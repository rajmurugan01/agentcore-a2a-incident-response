"""Monitoring/incident agent -- Strands SDK, deployed as an AgentCore Runtime A2A server.

Mirrors the real pattern in AWS's sample-fully-autonomous-incident-response
monitoring_agent.py: a Strands Agent wrapped by strands.multiagent.a2a.server.A2AServer,
mounted into a FastAPI app, served over uvicorn. Deviates from the sample by calling
CloudWatch directly with boto3 tools (cloudwatch_tools.py) instead of through an
AgentCore Gateway + MCP -- the Gateway/Memory/Identity primitives are out of scope here,
with ONE exception added 2026-10-22: AgentCore Identity's outbound-auth credential
provider, used below so this agent can make its own agent-to-agent hand-off call to the
remediation agent (see handoff_to_remediation_agent) without ever holding a plaintext
Cognito client secret in code, env, or the image -- see cdk/lib/stack.ts's
MonitoringToRemediationCredentialProvider and docs/specs/..., approval condition 2.
"""
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from uuid import uuid4

import uvicorn
from fastapi import FastAPI
from strands import Agent, tool
from strands.hooks import AfterInvocationEvent
from strands.models import BedrockModel
from strands.multiagent.a2a.server import A2AServer

from bedrock_agentcore.identity import requires_access_token
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from bedrock_agentcore.runtime.models import ACCESS_TOKEN_HEADER

from cloudwatch_tools import MONITORING_TOOLS

logging.basicConfig(format="[%(asctime)s] %(levelname)s %(name)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("monitoring_agent")

REGION = os.environ.get("AWS_REGION", "us-east-1")
HOST = os.environ.get("HOST", "0.0.0.0")
# AgentCore Runtime's A2A ProtocolConfiguration proxies the container on port 9000 at the
# root path, NOT port 8080/invocations (that's the plain HTTP/MCP protocol convention) --
# confirmed against AWS's A2A devguide after the first deploy attempt used 8080 and the
# runtime came up but was unreachable for A2A invocation. See README "sharp edge".
PORT = int(os.environ.get("PORT", 9000))
RUNTIME_URL = os.environ.get("AGENTCORE_RUNTIME_URL", f"http://127.0.0.1:{PORT}/")

# ── Agent-to-agent hand-off config (Tier 1, 2026-10-22) -- all non-secret identifiers,
# set by cdk/lib/stack.ts's environmentVariables on this runtime. The actual Cognito
# client secret is never here: it lives only in AgentCore Identity's own vault, behind
# OAUTH2_CREDENTIAL_PROVIDER_NAME, fetched fresh at call time below.
REMEDIATION_RUNTIME_ARN = os.environ.get("REMEDIATION_RUNTIME_ARN", "")
OAUTH2_CREDENTIAL_PROVIDER_NAME = os.environ.get("OAUTH2_CREDENTIAL_PROVIDER_NAME", "monitoring-to-remediation-cognito")
REMEDIATION_SCOPE = os.environ.get("REMEDIATION_SCOPE", "incident-response/remediation.invoke")

SYSTEM_PROMPT = (
    "You are a monitoring agent for a small AWS workload. You have read-only tools for "
    "CloudWatch alarms, metrics and logs, plus a hand-off tool to a separate remediation "
    "agent. When asked to check for incidents:\n"
    "1. Use list_alarms_in_alarm_state and get_metric_statistics to gather REAL evidence. "
    "Never invent an alarm name or a metric value a tool did not return.\n"
    "2. If no alarm is in ALARM state, say so explicitly and STOP -- do not call the "
    "hand-off tool and do not invent an incident.\n"
    "3. If an alarm IS in ALARM state, write a one-line finding in EXACTLY this form so it "
    "can be checked against CloudWatch directly: 'FINDING: alarm=<the exact AlarmName> "
    "metric_value=<the numeric average or maximum you retrieved>'. Then call "
    "handoff_to_remediation_agent with that finding text.\n"
    "4. Report back, in this order: the FINDING line, then 'REMEDIATION RECOMMENDATION: ' "
    "followed by the exact text the hand-off tool returned. Do not summarize or shorten "
    "the remediation agent's recommendation."
)

model = BedrockModel(
    # Claude 3.5 Haiku has reached end-of-life on Bedrock (confirmed live: InvokeModel
    # returned ResourceNotFoundException "This model version has reached the end of its
    # life" during this deploy's first demo run) -- Haiku 4.5 is the current GA
    # fast/cheap-tier model (model card: lifecycle Active, EOL N/A, launched 2025-10-16).
    # us-east-1 has no in-Region inference profile for it, only geo/global cross-Region --
    # "us.anthropic..." below is the US geo profile, confirmed against the AWS model card.
    model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    region_name=REGION,
    temperature=0.1,
    max_tokens=2048,
)


def _utcnow() -> str:
    """UTC timestamp logged from INSIDE this container -- per Tier 1 criterion 5, the
    provenance that matters is 'this ran in the container, not on the laptop running
    demo.py'. Printed with a fixed 'STEP' prefix so it's grep-able out of CloudWatch Logs."""
    return datetime.now(timezone.utc).isoformat()


def _log_step(step: str, **fields) -> None:
    payload = {"ts": _utcnow(), "step": step, **fields}
    logger.info("STEP %s", json.dumps(payload, default=str))


def _invocations_url(region: str, runtime_arn: str, suffix: str = "") -> str:
    escaped = urllib.parse.quote(runtime_arn, safe="")
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped}/invocations{suffix}?qualifier=DEFAULT"


def _bearer_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": str(uuid4()),
    }


def _get(url: str, headers: dict) -> bytes:
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _post_json(url: str, headers: dict, body: dict) -> bytes:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _extract_a2a_text(raw: bytes) -> str:
    """Pull the plain-text recommendation out of the remediation agent's JSON-RPC
    message/send response envelope (a2a-sdk's new_agent_text_message shape)."""
    try:
        data = json.loads(raw)
        result = data.get("result", data)
        if isinstance(result, dict):
            parts = result.get("parts") or (result.get("message") or {}).get("parts", [])
            texts = [p.get("text") for p in parts if isinstance(p, dict) and p.get("text")]
            if texts:
                return "\n".join(texts)
        return raw.decode()
    except (json.JSONDecodeError, AttributeError):
        return raw.decode(errors="replace")


@requires_access_token(
    provider_name=OAUTH2_CREDENTIAL_PROVIDER_NAME,
    scopes=[REMEDIATION_SCOPE],
    auth_flow="M2M",
)
def _fetch_remediation_token(*, access_token: str) -> str:
    """Isolated so the OAuth fetch never leaks into handoff_to_remediation_agent's @tool
    schema -- Strands builds that schema from the function signature the model sees, and
    the model must never be asked to supply an access_token argument itself."""
    return access_token


@tool
def handoff_to_remediation_agent(finding: str) -> str:
    """Hand off a confirmed CloudWatch incident finding to the remediation agent over the
    real A2A protocol: fetch its agent card, then send a JSON-RPC message/send, using an
    OAuth token THIS agent fetches itself via AgentCore Identity outbound auth (never a
    secret from an environment variable). Call this only after you have a real finding
    backed by tool evidence, never with an invented finding.

    Args:
        finding: a one-line finding string, e.g. 'FINDING: alarm=<name> metric_value=<n>'.

    Returns:
        The remediation agent's text recommendation.
    """
    if not REMEDIATION_RUNTIME_ARN:
        raise RuntimeError("REMEDIATION_RUNTIME_ARN is not set -- cannot hand off.")

    _log_step("token_fetch_start", scope=REMEDIATION_SCOPE, provider=OAUTH2_CREDENTIAL_PROVIDER_NAME)
    try:
        token = _fetch_remediation_token()
    except Exception as exc:  # pragma: no cover -- debug instrumentation while verifying outbound auth
        _log_step("token_fetch_FAILED", error_type=type(exc).__name__, error=str(exc))
        raise
    _log_step("token_fetch_done")

    card_url = _invocations_url(REGION, REMEDIATION_RUNTIME_ARN, suffix="/.well-known/agent-card.json")
    invoke_url = _invocations_url(REGION, REMEDIATION_RUNTIME_ARN)

    _log_step("card_fetch_start")
    card_raw = _get(card_url, _bearer_headers(token))
    _log_step("card_fetch_done", card_bytes=len(card_raw))

    envelope = {
        "jsonrpc": "2.0", "id": "1", "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": finding}],
                                "messageId": str(uuid4())}},
    }
    _log_step("handoff_send_start", finding=finding)
    resp_raw = _post_json(invoke_url, _bearer_headers(token), envelope)
    _log_step("handoff_send_done", response_bytes=len(resp_raw))

    return _extract_a2a_text(resp_raw)


monitoring_agent = Agent(
    name="monitoring_agent",
    description="Reads CloudWatch alarms, metrics and logs, reports incident findings, and "
                "hands off confirmed incidents to the remediation agent over A2A.",
    system_prompt=SYSTEM_PROMPT,
    model=model,
    tools=MONITORING_TOOLS + [handoff_to_remediation_agent],
)

def _on_after_invocation(event: AfterInvocationEvent) -> None:
    """Fires for EVERY completed agent invocation, regardless of entry point (A2A
    message/send from demo.py, or a direct find_incidents_direct() call) -- unlike
    find_incidents_direct()'s own logging, this is the one place that reliably logs real
    per-call input/output token counts (Tier 1 criterion 6) no matter how the agent was
    invoked."""
    try:
        usage = event.agent.event_loop_metrics.accumulated_usage
        _log_step(
            "model_usage",
            model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
            input_tokens=usage.get("inputTokens"),
            output_tokens=usage.get("outputTokens"),
            total_tokens=usage.get("totalTokens"),
        )
    except Exception as exc:  # pragma: no cover -- never let usage logging break the response
        logger.warning("Could not log token usage: %s", exc)


monitoring_agent.hooks.add_callback(AfterInvocationEvent, _on_after_invocation)

a2a_server = A2AServer(
    agent=monitoring_agent,
    host=HOST,
    port=PORT,
    http_url=RUNTIME_URL,
    serve_at_root=True,
    version="1.0.0",
)

app: FastAPI = a2a_server.to_fastapi_app()


@app.middleware("http")
async def _log_requests(request, call_next):
    """Container-clock markers for EVERY inbound call this runtime receives (A2A
    message/send included) -- not just the ones that happen to go through
    find_incidents_direct(). This is the real 'detection_start' / 'detection_done'
    boundary for the deployed pattern-(c) runs: the Strands agent's own tool-calling loop
    runs inside call_next(), so the gap between these two lines and
    handoff_to_remediation_agent's token_fetch_start log is genuine detection+reasoning
    time, not an estimate. Also doubles as the laptop/container clock-skew check -- the
    caller (demo.py) records its own UTC send time right before this request and compares
    it against request_start below rather than assuming the two clocks agree.

    ALSO does the workload-access-token extraction Strands' own A2AServer doesn't do:
    AgentCore Runtime passes the per-request WAT in a 'WorkloadAccessToken' header
    (confirmed live, 2026-10-22 build -- see bedrock_agentcore.runtime.a2a's
    BedrockCallContextBuilder, which implements exactly this extraction for the a2a-sdk's
    own ServerCallContextBuilder hook, but Strands' A2AServer builds its
    A2AStarletteApplication without wiring that hook in). Without this,
    @requires_access_token inside handoff_to_remediation_agent finds no WAT in
    BedrockAgentCoreContext, assumes it's running in local dev, and tries to CREATE a
    workload identity -- which the monitoring role correctly does NOT have permission to
    do, and the hand-off 500s with AccessDeniedException on
    bedrock-agentcore:CreateWorkloadIdentity. Hit this for real on the first live
    pattern-(c) run against the redeployed image."""
    token = request.headers.get(ACCESS_TOKEN_HEADER)
    if token:
        BedrockAgentCoreContext.set_workload_access_token(token)
        _log_step("workload_access_token_extracted_from_header")
    _log_step("runtime_request_start", path=request.url.path, method=request.method)
    response = await call_next(request)
    _log_step("runtime_request_done", path=request.url.path, status_code=response.status_code)
    return response


@app.get("/ping")
def ping():
    return {"status": "healthy", "agent": "monitoring_agent"}


# Also exposed as a bare, non-A2A function: a direct in-process call to the same
# tool/model logic the A2A layer wraps, with no AgentCore Runtime hop at all -- not used
# by demo.py's pattern (a) (that stays TEST_FINDING-only, a pure transport baseline, per
# the Tier 1 spec), but kept callable for manual verification against the live stack.
# Real detection either way: calls the live Strands agent, which calls real CloudWatch
# tools -- ground truth is checked by the CALLER against describe-alarms/
# get-metric-statistics, never inside this function (never silently retry a miss).
def find_incidents_direct(prompt: str = "Check for any current incidents and hand off any you confirm.") -> str:
    _log_step("detection_start", prompt=prompt)
    result = monitoring_agent(prompt)
    _log_step("detection_done")
    return str(result)


if __name__ == "__main__":
    logger.info("Starting monitoring_agent A2A server on %s:%s", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT)
