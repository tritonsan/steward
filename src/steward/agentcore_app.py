"""Stateless ARM64 AgentCore entrypoint. Never receives a database connection."""

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from steward.agents.coordinator import StrandsCoordinator
from steward.config import StewardSettings
from steward.domain.clock import SystemClock

app = BedrockAgentCoreApp()


class TraceCollector:
    def __init__(self):
        self.traces = []

    def put_configuration(self, artifact):
        self.traces.append(artifact.payload)


@app.entrypoint
def invoke(payload):
    traces = TraceCollector()
    output = StrandsCoordinator(settings=StewardSettings(), store=traces, clock=SystemClock()).plan(
        context_id=payload["context_id"], context=payload["context"]
    )
    return {"proposal": output.model_dump(mode="json"), "traces": traces.traces}


if __name__ == "__main__":
    app.run()
