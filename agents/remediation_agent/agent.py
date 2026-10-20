"""Remediation/ops agent -- LangGraph, deployed as an AgentCore Runtime A2A server.

Deviates from AWS's sample-fully-autonomous-incident-response (which uses the OpenAI
Agents SDK for this role) on purpose: LangGraph instead, re-using the framework from
the earlier rajmurugan.com LangGraph-vs-AgentCore post rather than signing up for a new
OpenAI API key. Does something genuinely agentic in response to a monitoring finding:
classifies it, looks up a static local runbook (runbooks.json -- no paid search API),
and asks the model to turn the runbook steps into a recommendation for THIS finding.

Strands ships strands.multiagent.a2a.server.A2AServer out of the box; LangGraph does
not have an equivalent, so this file wires the graph to the a2a-sdk's own
AgentExecutor/A2AStarletteApplication primitives directly.
"""
import json
import logging
import os
from typing import TypedDict

import uvicorn
from langchain_aws import ChatBedrock
from langgraph.graph import StateGraph, END

from a2a.server.apps import A2AStarletteApplication
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message

logging.basicConfig(format="[%(asctime)s] %(levelname)s %(name)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("remediation_agent")

REGION = os.environ.get("AWS_REGION", "us-east-1")
HOST = os.environ.get("HOST", "0.0.0.0")
# AgentCore Runtime's A2A ProtocolConfiguration proxies the container on port 9000 at the
# root path, not 8080 -- see monitoring_agent/agent.py for the full note.
PORT = int(os.environ.get("PORT", 9000))

with open(os.path.join(os.path.dirname(__file__), "runbooks.json")) as f:
    RUNBOOKS = json.load(f)

model = ChatBedrock(
    # See monitoring_agent/agent.py for why this isn't Claude 3.5 Haiku any more (EOL,
    # confirmed live against the deployed stack) and why the model id is prefixed "us.".
    model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    region_name=REGION,
    model_kwargs={"temperature": 0.1, "max_tokens": 1024},
)


class RemediationState(TypedDict):
    finding: str
    runbook_key: str
    recommendation: str


def classify_finding(state: RemediationState) -> RemediationState:
    """Match the finding's keywords against the local runbook knowledge source."""
    text = state["finding"].lower()
    for key, rb in RUNBOOKS.items():
        if key == "default":
            continue
        if any(term in text for term in rb["match"]):
            return {**state, "runbook_key": key}
    return {**state, "runbook_key": "default"}


def recommend(state: RemediationState) -> RemediationState:
    """Ask the model to turn the matched runbook's steps into a recommendation for this finding."""
    runbook = RUNBOOKS[state["runbook_key"]]
    prompt = (
        f"A monitoring agent reported this finding:\n{state['finding']}\n\n"
        f"Matched runbook: {runbook['title']}\nSteps:\n"
        + "\n".join(f"- {s}" for s in runbook["steps"])
        + "\n\nWrite a 3-5 sentence remediation recommendation for THIS specific finding, "
        "grounded in the matched runbook steps. Do not invent steps the runbook doesn't contain."
    )
    response = model.invoke(prompt)
    return {**state, "recommendation": response.content}


graph_builder = StateGraph(RemediationState)
graph_builder.add_node("classify", classify_finding)
graph_builder.add_node("recommend", recommend)
graph_builder.set_entry_point("classify")
graph_builder.add_edge("classify", "recommend")
graph_builder.add_edge("recommend", END)
remediation_graph = graph_builder.compile()


def remediate_direct(finding: str) -> str:
    """Bare in-process call used by demo/demo.py comms pattern (a): no A2A, no AgentCore hop."""
    result = remediation_graph.invoke({"finding": finding, "runbook_key": "", "recommendation": ""})
    return result["recommendation"]


class RemediationAgentExecutor(AgentExecutor):
    """Bridges the LangGraph graph into the a2a-sdk's AgentExecutor contract."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        finding = context.get_user_input()
        logger.info("Remediation agent received finding: %s", finding)
        recommendation = remediate_direct(finding)
        await event_queue.enqueue_event(new_agent_text_message(recommendation))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancellation is not supported for this short-lived agent.")


agent_card = AgentCard(
    name="remediation_agent",
    description="Classifies a monitoring finding and recommends a remediation from a local runbook knowledge source.",
    url=os.environ.get("AGENTCORE_RUNTIME_URL", f"http://127.0.0.1:{PORT}/"),
    version="1.0.0",
    default_input_modes=["text"],
    default_output_modes=["text"],
    capabilities=AgentCapabilities(streaming=False),
    skills=[
        AgentSkill(
            id="remediate_finding",
            name="Remediate an incident finding",
            description="Given a monitoring agent's incident finding, return a runbook-grounded remediation recommendation.",
            tags=["incident-response", "remediation"],
        )
    ],
)

request_handler = DefaultRequestHandler(
    agent_executor=RemediationAgentExecutor(),
    task_store=InMemoryTaskStore(),
)
a2a_app = A2AStarletteApplication(agent_card=agent_card, http_handler=request_handler)
app = a2a_app.build()

if __name__ == "__main__":
    logger.info("Starting remediation_agent A2A server on %s:%s", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT)
