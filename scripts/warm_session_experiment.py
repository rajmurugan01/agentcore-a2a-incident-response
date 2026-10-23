#!/usr/bin/env python3
"""One-off diagnostic (2026-10-23): isolates AgentCore Runtime session/microVM cold-start
cost from A2A's own wire-protocol cost (JSON-RPC serialization, HTTP round trips) --
written in response to a technical reviewer's pushback on the blog post's claim that
"~15 of 25.3 seconds (about 60%) is A2A protocol mechanics".

Reviewer's hypothesis: AgentCore Runtime uses per-session microVM isolation, so a brand
new `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` on every call (which is EXACTLY what
agent.py's `_bearer_headers` and demo.py's own `_bearer_headers` both do -- confirmed by
reading both files before writing this script) forces a real cold start on every single
HTTP call, and that cold start, not A2A's own wire mechanics, is most of what the post
blamed on "the A2A protocol".

Test: for each of the two steps the post's "~15s" claim blames most
(GET /.well-known/agent-card.json, and POST message/send), run it twice back-to-back on
the SAME session ID -- the first call (not measured) exists only to put that session ID
into a warm state; the second call (measured) is "warm". A fresh session ID used for a
single call, never reused, is "cold". 5 cold + 5 warm reps per step, same discipline as
demo.py's RUNS_PER_PATTERN=5.

Both steps are run directly against the REMEDIATION runtime (same geometry as demo.py's
pattern (c) CONTROL -- this script fetches the card and sends message/send itself),
deliberately NOT through the monitoring agent's own LLM reasoning loop (pattern (c) REAL):
routing through the monitoring agent's Bedrock-model tool-calling loop would add
model-latency variance that has nothing to do with session warmth and would make cold vs
warm impossible to read cleanly. This script needs the MONITORING client's token (scoped
to invoke remediation), same scope demo.py's pattern (b)/(c-control) use.

Run with: make warm-session-experiment (wraps
`python3 scripts/warm_session_experiment.py | tee docs/warm_session_transcript.txt`)
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
from datetime import datetime, timezone
from uuid import uuid4

import boto3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "cdk", "cdk.outputs.json")
REGION = os.environ.get("AWS_REGION", "us-east-1")
REPS = 5
SPEND_CAP_USD = 5.0

TEST_FINDING = (
    "CPUUtilization alarm on service checkout-api has been in ALARM state for 9 minutes; "
    "average CPU is 94%% over the last 15 minutes, no corresponding deploy in that window."
)

PRICE_HAIKU_INPUT_PER_1K = 0.001
PRICE_HAIKU_OUTPUT_PER_1K = 0.005


def load_outputs():
    with open(OUT_PATH) as f:
        data = json.load(f)
    return data[next(iter(data))]


def get_client_credentials_token(domain_url, client_id, client_secret, scope):
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = f"grant_type=client_credentials&scope={scope}".encode()
    req = urllib.request.Request(
        f"{domain_url}/oauth2/token", data=body, method="POST",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["access_token"]


def _invocations_url(region, runtime_arn, suffix=""):
    escaped = urllib.parse.quote(runtime_arn, safe="")
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped}/invocations{suffix}?qualifier=DEFAULT"


def _headers(token, session_id):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }


def _get(url, headers):
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _post_json(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _send_envelope(finding_text):
    return {
        "jsonrpc": "2.0", "id": "1", "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": finding_text}],
                                "messageId": str(uuid4())}},
    }


def card_fetch(card_url, token, session_id):
    start = time.perf_counter()
    raw = _get(card_url, _headers(token, session_id))
    elapsed = time.perf_counter() - start
    return elapsed, len(raw)


def message_send(invoke_url, token, session_id):
    start = time.perf_counter()
    raw = _post_json(invoke_url, _headers(token, session_id), _send_envelope(TEST_FINDING))
    elapsed = time.perf_counter() - start
    return elapsed, len(raw)


def run_cold(fn, n=REPS):
    """A brand-new, never-before-used session ID for every single rep, consumed once."""
    samples, bytes_seen = [], []
    for i in range(n):
        session_id = str(uuid4())
        elapsed, nbytes = fn(session_id)
        samples.append(elapsed)
        bytes_seen.append(nbytes)
        print(f"    cold rep {i + 1}/{n}: session={session_id} {elapsed:.3f}s ({nbytes} bytes)")
    return samples, bytes_seen


def run_warm(fn, n=REPS):
    """A fresh session ID per rep, but warmed by one throwaway call on that SAME session ID
    immediately before the measured call. Only the second call is timed."""
    samples, bytes_seen, warm_up_samples = [], [], []
    for i in range(n):
        session_id = str(uuid4())
        warm_elapsed, _ = fn(session_id)
        warm_up_samples.append(warm_elapsed)
        elapsed, nbytes = fn(session_id)
        samples.append(elapsed)
        bytes_seen.append(nbytes)
        print(f"    warm rep {i + 1}/{n}: session={session_id} warm-up={warm_elapsed:.3f}s "
              f"measured={elapsed:.3f}s ({nbytes} bytes)")
    return samples, bytes_seen, warm_up_samples


def report(label, samples):
    print(f"  {label}: min={min(samples):.3f}s median={statistics.median(samples):.3f}s "
          f"max={max(samples):.3f}s (n={len(samples)})")
    return {"min": min(samples), "median": statistics.median(samples), "max": max(samples), "n": len(samples),
            "samples": samples}


def estimate_spend_so_far_usd(cw_client, since_hours=6):
    from datetime import timedelta
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=since_hours)
    total_in = total_out = 0
    for metric_name, bucket in (("InputTokenCount", "in"), ("OutputTokenCount", "out")):
        try:
            resp = cw_client.get_metric_statistics(
                Namespace="AWS/Bedrock", MetricName=metric_name,
                StartTime=start, EndTime=end, Period=3600, Statistics=["Sum"],
            )
        except Exception:
            continue
        total = sum(p["Sum"] for p in resp.get("Datapoints", []))
        if bucket == "in":
            total_in = total
        else:
            total_out = total
    cost = (total_in / 1000) * PRICE_HAIKU_INPUT_PER_1K + (total_out / 1000) * PRICE_HAIKU_OUTPUT_PER_1K
    return cost, total_in, total_out


def main():
    outputs = load_outputs()
    region = outputs["Region"]
    remediation_arn = outputs["RemediationRuntimeArn"]
    cw = boto3.client("cloudwatch", region_name=region)

    print("=" * 78)
    print("Warm vs cold AgentCore Runtime session: isolating cold-start from A2A wire cost")
    print("=" * 78)
    print(f"\nStarted (UTC): {datetime.now(timezone.utc).isoformat()}")
    print(f"Remediation runtime ARN: {remediation_arn}")

    spend, tok_in, tok_out = estimate_spend_so_far_usd(cw)
    print(f"\nSpend check before running: ~${spend:.4f} so far this session "
          f"(in={tok_in:.0f} tok, out={tok_out:.0f} tok, list-price-derived). Cap: ${SPEND_CAP_USD}.")
    if spend >= SPEND_CAP_USD:
        print(f"FATAL: spend cap (${SPEND_CAP_USD}) reached or exceeded -- stopping.", file=sys.stderr)
        sys.exit(1)

    monitoring_token = get_client_credentials_token(
        outputs["CognitoDomainUrl"], outputs["MonitoringClientId"],
        os.environ["MONITORING_CLIENT_SECRET"], "incident-response/remediation.invoke",
    )

    card_url = _invocations_url(region, remediation_arn, suffix="/.well-known/agent-card.json")
    invoke_url = _invocations_url(region, remediation_arn)

    results = {}

    print("\n== STEP: GET /.well-known/agent-card.json -- COLD (brand-new session id each rep) ==")
    cold_card_samples, cold_card_bytes = run_cold(lambda sid: card_fetch(card_url, monitoring_token, sid))
    results["card_cold"] = report("card-fetch COLD", cold_card_samples)

    print("\n== STEP: GET /.well-known/agent-card.json -- WARM (session id warmed by one throwaway call first) ==")
    warm_card_samples, warm_card_bytes, warm_card_warmups = run_warm(lambda sid: card_fetch(card_url, monitoring_token, sid))
    results["card_warm"] = report("card-fetch WARM", warm_card_samples)
    results["card_warm_warmup"] = report("card-fetch WARM-UP CALL ITSELF", warm_card_warmups)

    print("\n== STEP: POST message/send -- COLD (brand-new session id each rep) ==")
    cold_send_samples, cold_send_bytes = run_cold(lambda sid: message_send(invoke_url, monitoring_token, sid))
    results["send_cold"] = report("message/send COLD", cold_send_samples)

    print("\n== STEP: POST message/send -- WARM (session id warmed by one throwaway call first) ==")
    warm_send_samples, warm_send_bytes, warm_send_warmups = run_warm(lambda sid: message_send(invoke_url, monitoring_token, sid))
    results["send_warm"] = report("message/send WARM", warm_send_samples)
    results["send_warm_warmup"] = report("message/send WARM-UP CALL ITSELF", warm_send_warmups)

    print("\n== Delta summary (median warm - median cold; negative = warm faster) ==")
    card_delta = results["card_warm"]["median"] - results["card_cold"]["median"]
    send_delta = results["send_warm"]["median"] - results["send_cold"]["median"]
    print(f"  card-fetch:   cold median {results['card_cold']['median']:.3f}s -> "
          f"warm median {results['card_warm']['median']:.3f}s  (delta {card_delta:+.3f}s)")
    print(f"  message/send: cold median {results['send_cold']['median']:.3f}s -> "
          f"warm median {results['send_warm']['median']:.3f}s  (delta {send_delta:+.3f}s)")

    print("\n== Idle-gap probe: reuse a session id again after a delay, see if it's still warm ==")
    # Build on one more session: warm it, wait, then measure again on the same id, to get
    # SOME signal on idle-timeout behaviour without burning a dedicated multi-minute test
    # per the spend/time budget for this one-off. Not a proper sweep -- single data point,
    # reported as exactly that.
    probe_session = str(uuid4())
    probe_warm_elapsed, _ = card_fetch(card_url, monitoring_token, probe_session)
    print(f"  probe session {probe_session}: warm-up call {probe_warm_elapsed:.3f}s")
    delay_s = 90
    print(f"  sleeping {delay_s}s before reusing the SAME session id...")
    time.sleep(delay_s)
    probe_after_delay_elapsed, _ = card_fetch(card_url, monitoring_token, probe_session)
    print(f"  same session id, after {delay_s}s idle: {probe_after_delay_elapsed:.3f}s "
          f"(compare to warm median {results['card_warm']['median']:.3f}s and cold median "
          f"{results['card_cold']['median']:.3f}s)")

    print("\n== Spend after this run ==")
    spend_after, tok_in_after, tok_out_after = estimate_spend_so_far_usd(cw)
    print(f"  ~${spend_after:.4f} (in={tok_in_after:.0f} tok, out={tok_out_after:.0f} tok)")
    if spend_after >= SPEND_CAP_USD:
        print(f"  WARNING: spend cap (${SPEND_CAP_USD}) reached or exceeded during this run.", file=sys.stderr)

    print(f"\nFinished (UTC): {datetime.now(timezone.utc).isoformat()}")
    print("\nDone.")


if __name__ == "__main__":
    main()
