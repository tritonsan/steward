"""AgentCore runs inference only; durable application state stays with the caller."""

import json
from uuid import uuid4

from steward.agents.planning import ResolutionPlanRecommendation
from steward.store import WorkflowArtifact


class AgentCoreResolutionPlanner:
    def __init__(self, *, settings, store, clock, client=None):
        import boto3

        self.settings, self.store, self.clock = settings, store, clock
        self.client = client or boto3.Session(
            profile_name=settings.aws_profile, region_name=settings.aws_region
        ).client("bedrock-agentcore")

    def plan(self, *, context_id, context):
        response = self.client.invoke_agent_runtime(
            agentRuntimeArn=self.settings.agentcore_runtime_arn,
            runtimeSessionId=str(uuid4()),
            payload=json.dumps({"context_id": context_id, "context": context}).encode(),
        )
        body = response["response"]
        try:
            payload = json.loads(body.read())
        finally:
            body.close()
        output = ResolutionPlanRecommendation.model_validate(payload["proposal"])
        if not set(output.source_ids) <= set(context["allowed_source_ids"]):
            raise ValueError("AgentCore returned unavailable evidence")
        for trace in payload.get("traces", []):
            self.store.put_configuration(
                WorkflowArtifact(
                    artifact_id=uuid4().hex,
                    kind="agent.trace.v1",
                    created_at=self.clock.now(),
                    source_ids=(context_id,),
                    payload=trace,
                )
            )
        return output
