#!/usr/bin/env python3
"""Measures the three ways the monitoring agent's finding can reach the remediation
agent, for real, against the stack deployed by `make deploy`:

  (a) DIRECT CALL       -- import both agents' functions in one process. No AgentCore hop.
  (b) NATIVE INVOKE      -- boto3 bedrock-agentcore `InvokeAgentRuntime`, raw JSON payload,
                             IAM/JWT-authenticated, no agent-card discovery, no JSON-RPC envelope.
  (c) FULL A2A PROTOCOL  -- `GetAgentCard` discovery, a Cognito client-credentials OAuth token,
                             then a JSON-RPC 2.0 `message/send` envelope through InvokeAgentRuntime.

Both (b) and (c) go through the SAME AgentCore data-plane operation (InvokeAgentRuntime /
GetAgentCard against the deployed Runtime's ARN) -- AgentCore does not expose a separate
non-A2A "runtime URL" to curl directly when a custom JWT authorizer is attached. What
differs between (b) and (c) is the payload shape and whether discovery/OAuth happens
first, not the transport. That is itself one of this repo's findings; see README.

Run with: make demo  (wraps `python3 demo/demo.py | tee docs/transcript.txt`)
"""
import base64
import json
import os
import statistics
import sys
import time
import urllib.request

import boto3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agents", "monitoring_agent"))
sys.path.insert(0, os.path.join(ROOT, "agents", "remediation_agent"))

OUT_PATH = os.path.join(ROOT, "cdk", "cdk.outputs.json")
RUNS_PER_PATTERN = 5
TEST_FINDING = (
    "CPUUtilization alarm on service checkout-api has been in ALARM state for 9 minutes; "
    "average CPU is 94%% over the last 15 minutes, no corresponding deploy in that window."
)

# Published list prices, cross-checked 2026-10-20. Labelled explicitly -- see README cost
# section for how measured counts are combined with these rates.
PRICE_RUNTIME_VCPU_HOUR = 0.0895       # AgentCore Runtime v1, per vCPU-hour
PRICE_RUNTIME_GB_HOUR = 0.00945        # AgentCore Runtime v1, per GB-hour (128MB minimum)
PRICE_HAIKU_INPUT_PER_1K = 0.0008      # Claude 3.5 Haiku on Bedrock, us-east-1, on-demand
PRICE_HAIKU_OUTPUT_PER_1K = 0.004


def load_outputs():
    with open(OUT_PATH) as f:
        data = json.load(f)
    return data[next(iter(data))]


def get_client_credentials_token(domain_url, client_id, client_secret, scope):
    """Cognito client_credentials grant -- the OAuth token pattern (c) needs, same as
    the AWS sample's monitoring_agent/utils.py get_access_token()."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = f"grant_type=client_credentials&scope={scope}".encode()
    req = urllib.request.Request(
        f"{domain_url}/oauth2/token", data=body, method="POST",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["access_token"]


def timed(fn, n=RUNS_PER_PATTERN):
    samples = []
    last_result = None
    for i in range(n):
        start = time.perf_counter()
        last_result = fn()
        samples.append(time.perf_counter() - start)
        print(f"    run {i + 1}/{n}: {samples[-1]:.3f}s")
    return samples, last_result


def report(label, samples):
    print(f"  {label}: min={min(samples):.3f}s median={statistics.median(samples):.3f}s max={max(samples):.3f}s "
          f"(n={len(samples)})")


# ── (a) DIRECT CALL ──────────────────────────────────────────────────────────────────
def pattern_a_direct():
    from agent import find_incidents_direct  # monitoring_agent/agent.py
    sys.path.insert(0, os.path.join(ROOT, "agents", "remediation_agent"))
    import importlib
    remediation = importlib.import_module("agent")  # remediation_agent/agent.py, same name clash avoided via sys.path order

    def run():
        finding = TEST_FINDING  # in a real direct-call integration the monitoring call's
        # own output would feed this; held constant here so all three patterns remediate
        # the identical finding and are cost/latency comparable.
        return remediation.remediate_direct(finding)

    return timed(run)


# ── (b) NATIVE AGENTCORE INVOKE (no A2A layer) ──────────────────────────────────────
def pattern_b_native_invoke(client, remediation_runtime_arn):
    def run():
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=remediation_runtime_arn,
            qualifier="DEFAULT",
            contentType="application/json",
            accept="application/json",
            payload=json.dumps({"finding": TEST_FINDING}).encode(),
        )
        return resp["response"].read()

    return timed(run)


# ── (c) FULL A2A PROTOCOL ────────────────────────────────────────────────────────────
def pattern_c_full_a2a(client, remediation_runtime_arn, token):
    def discover_and_send():
        # Discovery: a real, distinct AgentCore data-plane operation for A2A agent-card
        # retrieval -- GetAgentCard -- not a bare HTTPS fetch of .well-known/agent-card.json.
        card = client.get_agent_card(agentRuntimeArn=remediation_runtime_arn, qualifier="DEFAULT")
        envelope = {
            "jsonrpc": "2.0", "id": "1", "method": "message/send",
            "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": TEST_FINDING}]}},
        }
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=remediation_runtime_arn,
            qualifier="DEFAULT",
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(envelope).encode(),
        )
        return card["agentCard"], resp["response"].read()

    return timed(discover_and_send)


def main():
    outputs = load_outputs()
    region = outputs["Region"]
    remediation_arn = outputs["RemediationRuntimeArn"]

    print("=" * 70)
    print("AgentCore A2A incident-response: three comms patterns, measured")
    print("=" * 70)

    print("\n== (a) DIRECT CALL -- no AgentCore hop ==")
    a_samples, a_result = pattern_a_direct()
    report("direct", a_samples)
    print(f"  sample output: {str(a_result)[:160]}")

    client = boto3.client("bedrock-agentcore", region_name=region)

    print("\n== (b) NATIVE AGENTCORE INVOKE -- InvokeAgentRuntime, raw payload, no A2A layer ==")
    b_samples, b_result = pattern_b_native_invoke(client, remediation_arn)
    report("native invoke", b_samples)
    print(f"  sample output: {b_result[:160]}")

    print("\n== (c) FULL A2A PROTOCOL -- GetAgentCard + OAuth + JSON-RPC message/send ==")
    token = get_client_credentials_token(
        outputs["DiscoveryUrl"].replace("/.well-known/openid-configuration", ""),
        outputs["MonitoringClientId"], os.environ["MONITORING_CLIENT_SECRET"], "incident-response/remediation.invoke",
    )
    c_samples, c_result = pattern_c_full_a2a(client, remediation_arn, token)
    report("full A2A", c_samples)
    print(f"  sample agent-card + response: {str(c_result)[:160]}")

    print("\n== Cost: measured call counts x published list prices (see demo.py constants) ==")
    print(f"  Runs this session: {len(a_samples)} direct, {len(b_samples)} native-invoke, {len(c_samples)} full-A2A")
    print("  AgentCore Runtime compute: billed per second of actual vCPU+memory consumption,")
    print(f"  1-second minimum, at ${PRICE_RUNTIME_VCPU_HOUR}/vCPU-hour + ${PRICE_RUNTIME_GB_HOUR}/GB-hour (v1 list price).")
    print("  Model cost per call depends on actual measured token counts, not estimated here --")
    print("  pull them from this run's CloudWatch GetMetricData / Bedrock invocation logs and")
    print(f"  multiply by ${PRICE_HAIKU_INPUT_PER_1K}/1K input + ${PRICE_HAIKU_OUTPUT_PER_1K}/1K output tokens (list price).")

    print("\nDone.")


if __name__ == "__main__":
    main()
