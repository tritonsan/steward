"""Bounded Strands coordinator. Tools see one frozen evidence snapshot, never services."""

from __future__ import annotations

import json
from threading import Lock
from time import perf_counter
from uuid import uuid4

from steward.agents.planning import RESOLUTION_PLANNING_SYSTEM_PROMPT, ResolutionPlanRecommendation
from steward.store import WorkflowArtifact

PROMPT_VERSION = "steward-coordinator-3"


class InferenceBudgetExceeded(RuntimeError):
    pass


class CallBudget:
    def __init__(self, max_tools=8, max_models=12):
        self.tools, self.models, self.names = 0, 0, []
        self.max_tools, self.max_models = max_tools, max_models
        self.lock = Lock()

    def register_hooks(self, registry, **_):
        from strands.hooks import BeforeModelCallEvent, BeforeToolCallEvent

        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(BeforeModelCallEvent, self.before_model)

    def before_tool(self, event):
        with self.lock:
            self.tools += 1
            if self.tools > self.max_tools:
                raise InferenceBudgetExceeded("tool call budget exhausted")
            self.names.append(event.tool_use["name"])

    def before_model(self, _event):
        with self.lock:
            self.models += 1
            if self.models > self.max_models:
                raise InferenceBudgetExceeded("model call budget exhausted")


class StrandsCoordinator:
    def __init__(self, *, settings, store, clock, agent_factory=None):
        self.settings, self.store, self.clock = settings, store, clock
        self.agent_factory = agent_factory

    def plan(self, *, context_id, context):
        import boto3
        from strands import Agent, tool
        from strands.models import BedrockModel

        frozen = json.loads(json.dumps(context))
        budget = CallBudget()
        started = perf_counter()
        result = None
        failure = None
        investigation_usage = {}
        specialist_usage = []

        def ask_specialist(specialist, question):
            response = specialist(json.dumps({"question": question, "evidence": frozen}))
            metrics = getattr(response, "metrics", None)
            with budget.lock:
                specialist_usage.append(dict(getattr(metrics, "accumulated_usage", {}) or {}))
            return str(response)

        def agent(prompt, tools=()):
            kwargs = dict(
                system_prompt=prompt, tools=list(tools), hooks=[budget], callback_handler=None
            )
            if self.agent_factory:
                return self.agent_factory(**kwargs)
            session = boto3.Session(
                profile_name=self.settings.aws_profile, region_name=self.settings.aws_region
            )
            return Agent(
                model=BedrockModel(
                    model_id=self.settings.bedrock_model_id,
                    boto_session=session,
                    temperature=0,
                    max_tokens=self.settings.planning_max_tokens,
                ),
                **kwargs,
            )

        @tool
        def case_evidence() -> dict:
            """Read this case's frozen messages and the allowed source identifiers."""
            return {
                **{k: frozen[k] for k in ("case", "messages", "allowed_source_ids")},
                "management_notes": frozen.get("management_notes", []),
            }

        @tool
        def community_experience() -> dict:
            """Read historical outcomes and vendor evidence in this evidence version."""
            return {
                "history": frozen.get("history", []),
                "vendor_options": frozen.get("vendor_options", []),
                "policy": frozen.get("policy", {}),
            }

        @tool
        def procurement_specialist(question: str) -> str:
            """Assess repair scope and recurrence with the procurement specialist."""
            specialist = agent(
                RESOLUTION_PLANNING_SYSTEM_PROMPT
                + "\nAssess repair scope, past failures and warranty uncertainty. Do not act."
            )
            return ask_specialist(specialist, question)

        @tool
        def meeting_specialist(question: str) -> str:
            """Review disagreements and open questions with the meeting specialist."""
            specialist = agent(
                RESOLUTION_PLANNING_SYSTEM_PROMPT
                + "\nDistinguish opinions from confirmed decisions. "
                "Identify unanswered questions. Do not act."
            )
            return ask_specialist(specialist, question)

        try:
            coordinator = agent(
                RESOLUTION_PLANNING_SYSTEM_PROMPT
                + "\nRead case evidence first. For a dispute, call meeting_specialist. "
                "For a repair with past failures, call procurement_specialist. "
                "Your snapshot is fixed. Cite only allowed source IDs. At most eight tools. "
                "Explain what new information would change your proposed path.",
                (case_evidence, community_experience, procurement_specialist, meeting_specialist),
            )
            investigation = coordinator(
                json.dumps(
                    {
                        "task": "Investigate with the available tools before recommending a path. "
                        "Call case_evidence first and the appropriate specialist. "
                        "Do not produce the final structured recommendation yet.",
                        "context_id": context_id,
                        "offered_resolution_paths": frozen["offered_resolution_paths"],
                        "case": frozen["case"],
                    }
                )
            )
            investigation_usage = dict(
                getattr(getattr(investigation, "metrics", None), "accumulated_usage", {}) or {}
            )
            if "case_evidence" not in budget.names:
                raise ValueError("Coordinator did not inspect the case evidence")
            result = coordinator(
                "Return the final recommendation using the evidence and specialist "
                "findings already read. Cite only allowed source IDs from case_evidence.",
                structured_output_model=ResolutionPlanRecommendation,
            )
            output = ResolutionPlanRecommendation.model_validate(result.structured_output)
            if not set(output.source_ids).issubset(frozen["allowed_source_ids"]):
                raise ValueError("coordinator cited unavailable evidence")
            return output
        except Exception as exc:
            failure = type(exc).__name__
            raise
        finally:
            metrics = getattr(result, "metrics", None)
            usage = getattr(metrics, "accumulated_usage", {}) if metrics else {}
            total_usage = dict(usage)
            for specialist in [investigation_usage, *specialist_usage]:
                for key, value in specialist.items():
                    if isinstance(value, (int, float)):
                        total_usage[key] = total_usage.get(key, 0) + value
            self.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=uuid4().hex,
                    kind="agent.trace.v1",
                    created_at=self.clock.now(),
                    source_ids=(context_id,),
                    payload={
                        "model": self.settings.bedrock_model_id,
                        "prompt_version": PROMPT_VERSION,
                        "context_id": context_id,
                        "expected_version": frozen.get("expected_version"),
                        "source_ids": frozen["allowed_source_ids"],
                        "tools": budget.names,
                        "model_calls": budget.models,
                        "latency_ms": round((perf_counter() - started) * 1000),
                        "usage": total_usage,
                        "coordinator_usage": dict(usage),
                        "investigation_usage": investigation_usage,
                        "specialist_usage": specialist_usage,
                        "error": failure,
                    },
                )
            )
