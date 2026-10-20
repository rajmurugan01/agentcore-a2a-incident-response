#!/usr/bin/env python3
"""Measures the three ways the monitoring agent's finding can reach the remediation
agent, for real, against the stack deployed by `make deploy`:

  (a) DIRECT CALL       -- import both agents' functions in one process. No AgentCore hop.
  (b) NATIVE INVOKE      -- boto3 bedrock-agentcore `InvokeAgentRuntime`, raw JSON payload,
                             IAM/JWT-authenticated, no agent-card discovery, no JSON-RPC envelope.
  (c) FULL A2A PROTOCOL  -- `GetAgentCard` discovery, a Cognito client-credentials OAuth token,
                             then a JSON-RPC 2.0 `message/send` envelope through InvokeAgentRuntime.

Both (b) and (c) go through the SAME AgentCore data-plane operation (InvokeAgentRuntime /
GetAgentCard against the deployed Runtime's ARN) -- AgentCore DOES expose that operation
as a bare HTTPS endpoint you can curl directly (`POST .../runtimes/{arn}/invocations`),
but per AWS's own devguide, boto3 cannot be used to call it once a custom JWT authorizer
is attached: "If you plan on integrating your agent with OAuth, you can't use the AWS SDK
to call InvokeAgentRuntime. Instead, make an HTTPS request." A runtime supports EITHER IAM
SigV4 OR JWT inbound auth, never both, so once CustomJwtAuthorizer is configured (as it is
on both runtimes here, see cdk/lib/stack.ts), EVERY invocation -- not just the "full A2A"
one -- needs a raw HTTPS call with an `Authorization: Bearer <token>` header; the original
version of this script tried boto3's `invoke_agent_runtime`/`get_agent_card` for (b)/(c)
and was rewritten to raw `urllib` calls once this was caught during the build, before the
first live deploy. What differs between (b) and (c) is the payload shape and whether a
GetAgentCard-equivalent discovery GET happens first, not the transport, which is identical
for both. That is itself one of this repo's findings; see README.

Run with: make demo  (wraps `python3 demo/demo.py | tee docs/transcript.txt`)
"""
import base64
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

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
# Claude HAIKU 4.5 (3.5 Haiku hit end-of-life mid-build -- see agents/*/agent.py), on
# Bedrock, us-east-1, on-demand, via the "us." cross-Region inference profile. Rate
# cross-checked against third-party aggregators (e.g. typingmind's Bedrock cost
# reference), NOT independently confirmed against AWS's own pricing-page table, which
# renders its price grid client-side and didn't return the figure to an automated fetch
# during this build -- flagged, not fabricated; re-verify on https://aws.amazon.com/bedrock/pricing/
# before using this for a real budget.
PRICE_HAIKU_INPUT_PER_1K = 0.001       # $1.00 / 1M input tokens
PRICE_HAIKU_OUTPUT_PER_1K = 0.005      # $5.00 / 1M output tokens


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


def _load_module(name, path):
    """Both agents' entrypoint files are named agent.py -- `import agent` after pushing
    both directories onto sys.path resolves to whichever one sys.path happens to put
    first (remediation_agent, since it's inserted after monitoring_agent at module load
    time), silently shadowing the other. Caught this for real: pattern (a) crashed with
    ImportError because `from agent import find_incidents_direct` resolved to
    remediation_agent's agent.py, which has no such function. Load each by explicit file
    path under its own module name instead of relying on sys.path/import-name ordering."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── (a) DIRECT CALL ──────────────────────────────────────────────────────────────────
def pattern_a_direct():
    monitoring = _load_module("monitoring_agent_module", os.path.join(ROOT, "agents", "monitoring_agent", "agent.py"))
    remediation = _load_module("remediation_agent_module", os.path.join(ROOT, "agents", "remediation_agent", "agent.py"))
    assert monitoring.find_incidents_direct  # confirms the monitoring agent's direct-call path also loaded

    def run():
        finding = TEST_FINDING  # in a real direct-call integration the monitoring call's
        # own output would feed this; held constant here so all three patterns remediate
        # the identical finding and are cost/latency comparable.
        return remediation.remediate_direct(finding)

    return timed(run)


def _invocations_url(region, runtime_arn, suffix=""):
    escaped = urllib.parse.quote(runtime_arn, safe="")
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped}/invocations{suffix}?qualifier=DEFAULT"


def _bearer_headers(token):
    # 33-256 char session id required by the service; uuid4's 36-char hex-with-dashes form fits.
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": str(uuid4()),
    }


def _post_json(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _get(url, headers):
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


# ── (b) NATIVE AGENTCORE INVOKE (no A2A layer) ──────────────────────────────────────
def pattern_b_native_invoke(region, remediation_runtime_arn, token):
    # Raw JSON payload straight to InvokeAgentRuntime's HTTPS endpoint, no agent-card
    # discovery, no JSON-RPC envelope. Still needs the Bearer token: this runtime's
    # CustomJwtAuthorizer is the ONLY inbound auth mode it accepts (see module docstring),
    # so IAM/SigV4 (what boto3 would use) is rejected regardless of payload shape.
    url = _invocations_url(region, remediation_runtime_arn)

    def run():
        return _post_json(url, _bearer_headers(token), {"finding": TEST_FINDING})

    return timed(run)


# ── (c) FULL A2A PROTOCOL ────────────────────────────────────────────────────────────
def pattern_c_full_a2a(region, remediation_runtime_arn, token):
    card_url = _invocations_url(region, remediation_runtime_arn, suffix="/.well-known/agent-card.json")
    invoke_url = _invocations_url(region, remediation_runtime_arn)

    def discover_and_send():
        card = _get(card_url, _bearer_headers(token))
        envelope = {
            "jsonrpc": "2.0", "id": "1", "method": "message/send",
            "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": TEST_FINDING}],
                                    "messageId": str(uuid4())}},
        }
        resp = _post_json(invoke_url, _bearer_headers(token), envelope)
        return card, resp

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

    # Both (b) and (c) target a runtime whose ONLY inbound auth mode is the CustomJwtAuthorizer
    # (see module docstring) -- fetch the monitoring agent's client-credentials token once and
    # reuse it for both, exactly as the monitoring agent would when calling the remediation
    # agent for real.
    # NOTE: Cognito's OAuth2 token endpoint lives on the hosted-UI domain
    # (CognitoDomainUrl), not the issuer URL in DiscoveryUrl -- an earlier version of this
    # script derived it by string-stripping DiscoveryUrl, which silently 404s. See
    # cdk/lib/stack.ts's CognitoDomainUrl output and comment.
    token = get_client_credentials_token(
        outputs["CognitoDomainUrl"],
        outputs["MonitoringClientId"], os.environ["MONITORING_CLIENT_SECRET"], "incident-response/remediation.invoke",
    )

    print("\n== (b) NATIVE AGENTCORE INVOKE -- InvokeAgentRuntime, raw payload, no A2A layer ==")
    b_samples, b_result = pattern_b_native_invoke(region, remediation_arn, token)
    report("native invoke", b_samples)
    print(f"  sample output: {b_result[:160]}")

    print("\n== (c) FULL A2A PROTOCOL -- agent-card discovery + OAuth + JSON-RPC message/send ==")
    c_samples, c_result = pattern_c_full_a2a(region, remediation_arn, token)
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
