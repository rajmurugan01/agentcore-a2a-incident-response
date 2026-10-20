"""Monitoring/incident agent -- Strands SDK, deployed as an AgentCore Runtime A2A server.

Mirrors the real pattern in AWS's sample-fully-autonomous-incident-response
monitoring_agent.py: a Strands Agent wrapped by strands.multiagent.a2a.server.A2AServer,
mounted into a FastAPI app, served over uvicorn. Deviates from the sample by calling
CloudWatch directly with boto3 tools (cloudwatch_tools.py) instead of through an
AgentCore Gateway + MCP -- the Gateway/Memory/Identity primitives are out of scope here.
"""
import logging
import os

import uvicorn
from fastapi import FastAPI
from strands import Agent
from strands.models import BedrockModel
from strands.multiagent.a2a.server import A2AServer

from cloudwatch_tools import MONITORING_TOOLS

logging.basicConfig(format="[%(asctime)s] %(levelname)s %(name)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("monitoring_agent")

REGION = os.environ.get("AWS_REGION", "us-east-1")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8080))
RUNTIME_URL = os.environ.get("AGENTCORE_RUNTIME_URL", f"http://127.0.0.1:{PORT}/")

SYSTEM_PROMPT = (
    "You are a monitoring agent for a small AWS workload. You have read-only tools for "
    "CloudWatch alarms, metrics and logs. When asked to check for incidents, use the "
    "tools to gather real signal, then produce a short, structured finding: what is "
    "wrong (or confirm nothing is wrong), the evidence, and a one-line hand-off suitable "
    "for a remediation agent to act on. Never invent data a tool did not return."
)

model = BedrockModel(
    model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-3-5-haiku-20241022-v1:0"),
    region_name=REGION,
    temperature=0.1,
    max_tokens=1024,
)

monitoring_agent = Agent(
    name="monitoring_agent",
    description="Reads CloudWatch alarms, metrics and logs and reports incident findings.",
    system_prompt=SYSTEM_PROMPT,
    model=model,
    tools=MONITORING_TOOLS,
)

a2a_server = A2AServer(
    agent=monitoring_agent,
    host=HOST,
    port=PORT,
    http_url=RUNTIME_URL,
    serve_at_root=True,
    version="1.0.0",
)

app: FastAPI = a2a_server.to_fastapi_app()


@app.get("/ping")
def ping():
    return {"status": "healthy", "agent": "monitoring_agent"}


# Also exposed as a bare, non-A2A function for comms pattern (a) in demo/demo.py:
# a direct in-process call to the same tool/model logic the A2A layer wraps, with
# no AgentCore Runtime hop at all.
def find_incidents_direct(prompt: str = "Check for any current incidents.") -> str:
    result = monitoring_agent(prompt)
    return str(result)


if __name__ == "__main__":
    logger.info("Starting monitoring_agent A2A server on %s:%s", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT)
