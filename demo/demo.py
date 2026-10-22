#!/usr/bin/env python3
"""Measures the three ways a monitoring finding can reach the remediation agent, against
the stack deployed by `make deploy`, AND (2026-10-22 rebuild) proves the incident story is
real, not scripted. Four measured things, not three -- see
docs/specs/agentcore-a2a-incident-response.md's Tier 1 criteria for the full reasoning:

  (a) DIRECT CALL            -- UNCHANGED transport baseline. TEST_FINDING, in-process,
                                 no AgentCore hop, no detection. Exists to measure the
                                 floor, not the hand-off.
  (b) NATIVE INVOKE          -- UNCHANGED transport baseline. TEST_FINDING, raw JSON
                                 payload straight to the remediation runtime, no agent-card
                                 discovery, no JSON-RPC envelope, no detection.
  (c) FULL A2A -- CONTROL    -- TEST_FINDING, like-for-like with the 2026-10-21 numbers:
                                 THIS SCRIPT fetches remediation's card and sends
                                 message/send directly (the OLD, pre-fix method). Kept only
                                 for comparison -- this is explicitly NOT the real
                                 agent-to-agent hand-off; see (c) REAL below.
  (c) FULL A2A -- REAL       -- the actual fix. This script sends ONE message/send to the
                                 MONITORING runtime asking it to check for incidents and
                                 hand off any it confirms. The MONITORING AGENT ITSELF,
                                 running on its own AgentCore Runtime, then calls its real
                                 find_incidents_direct() CloudWatch tools, and if (and only
                                 if) it confirms a real alarm, fetches the remediation
                                 agent's card and sends message/send WITH ITS OWN OAuth
                                 token (obtained via AgentCore Identity outbound auth, see
                                 agents/monitoring_agent/agent.py). This script never talks
                                 to the remediation runtime at all in this path -- it only
                                 triggers the monitoring agent and reads back what comes
                                 out the other end. Each run's finding is checked against
                                 ground truth (docs/incident.json, describe-alarms,
                                 get-metric-statistics) -- a miss is counted as a FAILED
                                 run, never silently retried.

Run with: make demo  (wraps `python3 demo/demo.py | tee docs/transcript.txt`)
"""
import base64
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import boto3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agents", "monitoring_agent"))
sys.path.insert(0, os.path.join(ROOT, "agents", "remediation_agent"))

OUT_PATH = os.path.join(ROOT, "cdk", "cdk.outputs.json")
INCIDENT_PATH = os.path.join(ROOT, "docs", "incident.json")
RUNS_PER_PATTERN = 5
TEST_FINDING = (
    "CPUUtilization alarm on service checkout-api has been in ALARM state for 9 minutes; "
    "average CPU is 94%% over the last 15 minutes, no corresponding deploy in that window."
)
SPEND_CAP_USD = 5.0

# Published list prices, cross-checked 2026-10-20/21/22. Labelled explicitly -- see
# README cost section for how measured counts are combined with these rates.
PRICE_RUNTIME_VCPU_HOUR = 0.0895
PRICE_RUNTIME_GB_HOUR = 0.00945
PRICE_HAIKU_INPUT_PER_1K = 0.001
PRICE_HAIKU_OUTPUT_PER_1K = 0.005

REGION = os.environ.get("AWS_REGION", "us-east-1")


def load_outputs():
    with open(OUT_PATH) as f:
        data = json.load(f)
    return data[next(iter(data))]


def load_incident():
    if not os.path.exists(INCIDENT_PATH):
        print(f"FATAL: {INCIDENT_PATH} not found. Run `make incident` before `make demo` -- "
              "Tier 1 criterion 2 requires a REAL alarm to detect, not a hardcoded string.",
              file=sys.stderr)
        sys.exit(1)
    with open(INCIDENT_PATH) as f:
        return json.load(f)


def get_client_credentials_token(domain_url, client_id, client_secret, scope):
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
    """Both agents' entrypoint files are named agent.py -- load each by explicit file path
    under its own module name instead of relying on sys.path/import-name ordering (see
    README 'Sharp edges I hit')."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── (a) DIRECT CALL -- UNCHANGED transport baseline ─────────────────────────────────
def pattern_a_direct():
    monitoring = _load_module("monitoring_agent_module", os.path.join(ROOT, "agents", "monitoring_agent", "agent.py"))
    remediation = _load_module("remediation_agent_module", os.path.join(ROOT, "agents", "remediation_agent", "agent.py"))
    assert monitoring.find_incidents_direct

    def run():
        finding = TEST_FINDING
        return remediation.remediate_direct(finding)

    return timed(run)


def _invocations_url(region, runtime_arn, suffix=""):
    escaped = urllib.parse.quote(runtime_arn, safe="")
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped}/invocations{suffix}?qualifier=DEFAULT"


def _bearer_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": str(uuid4()),
    }


def _post_json(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _get(url, headers):
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _extract_a2a_text(raw):
    try:
        data = json.loads(raw)
        result = data.get("result", data)
        if isinstance(result, dict):
            parts = result.get("parts") or (result.get("message") or {}).get("parts", [])
            texts = [p.get("text") for p in parts if isinstance(p, dict) and p.get("text")]
            if texts:
                return "\n".join(texts)
        return str(data)
    except (json.JSONDecodeError, AttributeError):
        return raw.decode(errors="replace")


# ── (b) NATIVE AGENTCORE INVOKE -- UNCHANGED transport baseline ────────────────────
def pattern_b_native_invoke(region, remediation_runtime_arn, token):
    url = _invocations_url(region, remediation_runtime_arn)

    def run():
        return _post_json(url, _bearer_headers(token), {"finding": TEST_FINDING})

    return timed(run)


# ── (c) FULL A2A -- CONTROL: TEST_FINDING, client-driven (the OLD, pre-fix method) ──
def pattern_c_control_test_finding(region, remediation_runtime_arn, token):
    """Labelled control run, kept ONLY for like-for-like comparison against the
    2026-10-21 numbers (19.116s median) -- this is deliberately the OLD implementation
    (this script fetches the card and sends message/send itself), NOT the real
    agent-to-agent hand-off. See module docstring and Tier 1 criterion 4."""
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


# ── (c) FULL A2A -- REAL: this script only triggers the MONITORING runtime ─────────
REAL_RUN_PROMPT_TEMPLATE = (
    "[run_id={run_id}] Check CloudWatch for any current incidents using your monitoring "
    "tools. If you find a real alarm in ALARM state, hand it off to the remediation agent "
    "via your handoff tool and report both your FINDING line and the remediation agent's "
    "recommendation, exactly as your instructions describe. If no alarm is in ALARM state, "
    "say so explicitly and do not call the hand-off tool."
)


def pattern_c_real_run(region, monitoring_runtime_arn, token, run_id):
    """One real run: send/send to the MONITORING runtime. Returns (laptop_send_utc,
    laptop_recv_utc, response_text)."""
    invoke_url = _invocations_url(region, monitoring_runtime_arn)
    envelope = {
        "jsonrpc": "2.0", "id": "1", "method": "message/send",
        "params": {"message": {"role": "user",
                                "parts": [{"kind": "text", "text": REAL_RUN_PROMPT_TEMPLATE.format(run_id=run_id)}],
                                "messageId": str(uuid4())}},
    }
    send_utc = datetime.now(timezone.utc)
    raw = _post_json(invoke_url, _bearer_headers(token), envelope)
    recv_utc = datetime.now(timezone.utc)
    return send_utc, recv_utc, _extract_a2a_text(raw)


def check_ground_truth(response_text, incident, cw_client):
    """Tier 1 criterion 2: each run's finding must contain the real alarm name AND a
    metric value matching describe-alarms/get-metric-statistics -- checked against AWS
    directly, right now, not against a cached assumption. Never retried on a miss -- this
    function only ever reports pass/fail for the run it was given."""
    alarm_name = incident["alarm_name"]
    has_alarm_name = alarm_name in response_text

    # Re-pull ground truth from AWS directly, right now, for THIS check -- not reused
    # across runs even though the synthetic metric is constant in this build.
    alarms = cw_client.describe_alarms(AlarmNames=[alarm_name]).get("MetricAlarms", [])
    alarm_in_state = bool(alarms) and alarms[0]["StateValue"] == "ALARM"

    # Window is generous (60 min), not a tight recent window: `make incident` only
    # pushes datapoints while driving the alarm into ALARM, then stops (TreatMissingData
    # =ignore is what keeps the ALARM state sticky afterward -- see scripts/incident.py).
    # A tight "last 5 minutes" window would miss that one-time datapoint entirely if
    # `make demo` runs more than a few minutes after `make incident`, and wrongly fail
    # every real run's ground-truth check despite correct detection.
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=60)
    stats = cw_client.get_metric_statistics(
        Namespace=incident["namespace"], MetricName=incident["metric_name"],
        Dimensions=[{"Name": incident["dimension_name"], "Value": incident["dimension_value"]}],
        StartTime=start, EndTime=end, Period=10, Statistics=["Average", "Maximum"],
    )
    points = stats.get("Datapoints", [])
    ground_truth_value = max((p["Maximum"] for p in points), default=None)

    numbers_in_text = [float(n) for n in re.findall(r"\d+\.?\d*", response_text)]
    value_matches = (
        ground_truth_value is not None
        and any(abs(n - ground_truth_value) <= 5.0 for n in numbers_in_text)
    )

    passed = has_alarm_name and alarm_in_state and value_matches
    return {
        "passed": passed,
        "has_alarm_name": has_alarm_name,
        "alarm_in_state": alarm_in_state,
        "ground_truth_value": ground_truth_value,
        "value_matches": value_matches,
    }


def fetch_container_log_events(logs_client, log_group_name, start_utc, end_utc, filter_pattern='"STEP" -"/ping"'):
    """Default filter excludes AgentCore's own health-check polling (GET /ping every ~2s)
    -- real, frequent, and irrelevant noise for the hero timeline otherwise."""
    try:
        resp = logs_client.filter_log_events(
            logGroupName=log_group_name,
            startTime=int(start_utc.timestamp() * 1000),
            endTime=int(end_utc.timestamp() * 1000),
            filterPattern=filter_pattern,
            limit=1000,
        )
    except logs_client.exceptions.ResourceNotFoundException:
        return []
    events = []
    for e in resp.get("events", []):
        msg = e["message"]
        m = re.search(r"STEP (\{.*\})", msg)
        if m:
            try:
                step = json.loads(m.group(1))
                step["_log_event_ts_ms"] = e["timestamp"]
                events.append(step)
            except json.JSONDecodeError:
                continue
    return events


def discover_runtime_log_groups(logs_client, monitoring_arn, remediation_arn):
    """AgentCore Runtime log groups are named from the runtime's resource id, not its
    friendly name -- discover them by listing /aws/bedrock-agentcore/runtimes/* and
    matching the id suffix out of each runtime's ARN, rather than hardcoding a guessed
    naming convention."""

    def runtime_id(arn):
        return arn.rsplit("/", 1)[-1]

    mon_id = runtime_id(monitoring_arn)
    rem_id = runtime_id(remediation_arn)
    mon_group, rem_group = None, None
    paginator = logs_client.get_paginator("describe_log_groups")
    for page in paginator.paginate(logGroupNamePrefix="/aws/bedrock-agentcore/runtimes/"):
        for lg in page.get("logGroups", []):
            name = lg["logGroupName"]
            if mon_id in name:
                mon_group = name
            elif rem_id in name:
                rem_group = name
    return mon_group, rem_group


def estimate_spend_so_far_usd(cw_client, since_hours=6):
    """Spend cap (Tier 1 criterion 8): pulls real AWS/Bedrock CloudWatch metrics for
    recent token usage (same technique as the 2026-10-21 session's Cost section) and
    combines with published list rates. Deliberately conservative window (several hours)
    so it also catches spend from earlier in this same session (deploy-time validation
    calls), not just this process's own calls."""
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
    incident = load_incident()
    region = outputs["Region"]
    remediation_arn = outputs["RemediationRuntimeArn"]
    monitoring_arn = outputs["MonitoringRuntimeArn"]

    cw = boto3.client("cloudwatch", region_name=region)
    logs_client = boto3.client("logs", region_name=region)

    print("=" * 70)
    print("AgentCore A2A incident-response: real detection + real hand-off, measured")
    print("=" * 70)
    print(f"\nGround truth (docs/incident.json): alarm={incident['alarm_name']!r} "
          f"AWS StateUpdatedTimestamp={incident['state_updated_timestamp_aws']}")

    spend, tok_in, tok_out = estimate_spend_so_far_usd(cw)
    print(f"\nSpend check before running: ~${spend:.4f} so far this session "
          f"(in={tok_in:.0f} tok, out={tok_out:.0f} tok, list-price-derived). Cap: ${SPEND_CAP_USD}.")
    if spend >= SPEND_CAP_USD:
        print(f"FATAL: spend cap (${SPEND_CAP_USD}) reached or exceeded -- stopping, not continuing unbounded.",
              file=sys.stderr)
        sys.exit(1)

    print("\n== (a) DIRECT CALL -- UNCHANGED transport baseline (TEST_FINDING) ==")
    a_samples, a_result = pattern_a_direct()
    report("direct", a_samples)
    print(f"  sample output: {str(a_result)[:160]}")

    # Monitoring's token: scoped to invoke REMEDIATION (patterns b and c-control).
    monitoring_token = get_client_credentials_token(
        outputs["CognitoDomainUrl"], outputs["MonitoringClientId"],
        os.environ["MONITORING_CLIENT_SECRET"], "incident-response/remediation.invoke",
    )
    # Remediation's token: scoped to invoke MONITORING (the real pattern-c hand-off
    # trigger). This script is the CLIENT here -- it only ever talks to the monitoring
    # runtime in this path; the monitoring agent itself gets ITS OWN separate token to
    # call remediation, via AgentCore Identity outbound auth (see agent.py), never this one.
    remediation_token = get_client_credentials_token(
        outputs["CognitoDomainUrl"], outputs["RemediationClientId"],
        os.environ["REMEDIATION_CLIENT_SECRET"], "incident-response/monitoring.invoke",
    )

    print("\n== (b) NATIVE AGENTCORE INVOKE -- UNCHANGED transport baseline (TEST_FINDING) ==")
    b_samples, b_result = pattern_b_native_invoke(region, remediation_arn, monitoring_token)
    report("native invoke", b_samples)
    print(f"  sample output: {b_result[:160]}")

    print("\n== (c) FULL A2A -- CONTROL: TEST_FINDING, client-driven (like-for-like with 2026-10-21) ==")
    c_control_samples, c_control_result = pattern_c_control_test_finding(region, remediation_arn, monitoring_token)
    report("full A2A (control)", c_control_samples)
    print(f"  sample agent-card + response: {str(c_control_result)[:160]}")

    print("\n== (c) FULL A2A -- REAL: monitoring agent detects + hands off itself ==")
    real_runs = []
    for i in range(RUNS_PER_PATTERN):
        run_id = f"real-{i}-{uuid4().hex[:8]}"
        perf_start = time.perf_counter()
        send_utc, recv_utc, text = pattern_c_real_run(region, monitoring_arn, remediation_token, run_id)
        elapsed = time.perf_counter() - perf_start
        gt = check_ground_truth(text, incident, cw)
        real_runs.append({
            "run_id": run_id, "elapsed_s": elapsed,
            "send_utc": send_utc.isoformat(), "recv_utc": recv_utc.isoformat(),
            "response_text": text, "ground_truth": gt,
        })
        verdict = "PASS" if gt["passed"] else "FAIL (miss -- NOT retried)"
        print(f"    run {i + 1}/{RUNS_PER_PATTERN}: {elapsed:.3f}s -- detection {verdict} "
              f"(alarm_name_present={gt['has_alarm_name']} alarm_in_ALARM={gt['alarm_in_state']} "
              f"value_matches={gt['value_matches']})")
        print(f"      response: {text[:200]}")

    real_samples = [r["elapsed_s"] for r in real_runs]
    report("full A2A (real)", real_samples)
    n_pass = sum(1 for r in real_runs if r["ground_truth"]["passed"])
    print(f"  DETECTION SUCCESS RATE: {n_pass}/{len(real_runs)} runs "
          f"({100 * n_pass / len(real_runs):.0f}%) -- published as-is, no retries on misses.")

    # ── Pre-declared rule (Tier 1 criterion 3): the HERO timeline is the MEDIAN
    # pattern-(c)-REAL run by total duration, decided by this sort -- not hand-picked
    # after reading the responses.
    sorted_runs = sorted(real_runs, key=lambda r: r["elapsed_s"])
    median_run = sorted_runs[len(sorted_runs) // 2]
    print(f"\n== HERO TIMELINE: pre-declared MEDIAN pattern-(c)-real run by total duration "
          f"({median_run['run_id']}, {median_run['elapsed_s']:.3f}s) ==")

    mon_log_group, rem_log_group = discover_runtime_log_groups(logs_client, monitoring_arn, remediation_arn)
    print(f"  monitoring log group: {mon_log_group}")
    print(f"  remediation log group: {rem_log_group}")

    # Forward-only, no pre-buffer: nothing legitimate for THIS run can exist before this
    # run's own send_utc, so a backward buffer only risks pulling in the PRECEDING run's
    # tail (these 5 runs execute back-to-back with no idle gap). Caught this for real on
    # the first `make demo` attempt: a -5s pre-buffer pulled in the previous run's
    # handoff_send_done/model_usage lines, which would have silently mislabelled another
    # run's events as this one's.
    raw_start = datetime.fromisoformat(median_run["send_utc"])
    raw_end = datetime.fromisoformat(median_run["recv_utc"]) + timedelta(seconds=15)
    # CloudWatch Logs delivery lag -- give it time before fetching.
    time.sleep(15)
    raw_mon_events = fetch_container_log_events(logs_client, mon_log_group, raw_start, raw_end) if mon_log_group else []

    # Anchor precisely on THIS run's own monitoring-side request lifecycle (the one
    # non-ping POST "/" runtime_request_start/_done pair) rather than trusting the
    # laptop-clock window alone -- this is what actually eliminates cross-run bleed,
    # not the forward-only change above (which only reduces the risk, doesn't remove it).
    mon_root_starts = [e for e in raw_mon_events if e["step"] == "runtime_request_start" and e.get("path") == "/"]
    mon_root_dones = [e for e in raw_mon_events if e["step"] == "runtime_request_done" and e.get("path") == "/"]
    if mon_root_starts and mon_root_dones:
        anchor_start = datetime.fromisoformat(min(mon_root_starts, key=lambda e: e["ts"])["ts"])
        anchor_end = datetime.fromisoformat(min(mon_root_dones, key=lambda e: e["ts"])["ts"]) + timedelta(seconds=1)
    else:
        anchor_start, anchor_end = raw_start, raw_end

    mon_events = [e for e in raw_mon_events if anchor_start <= datetime.fromisoformat(e["ts"]) <= anchor_end]
    rem_events = fetch_container_log_events(logs_client, rem_log_group, anchor_start, anchor_end) if rem_log_group else []
    all_events = sorted(mon_events + rem_events, key=lambda e: e["ts"])

    print(f"  laptop send (UTC):     {median_run['send_utc']}")
    if mon_events:
        first_container_ts = min(e["ts"] for e in mon_events)
        gap = (datetime.fromisoformat(first_container_ts) - datetime.fromisoformat(median_run["send_utc"])).total_seconds()
        print(f"  monitoring container's first STEP ts: {first_container_ts}  "
              f"(gap vs laptop send: {gap:+.3f}s -- includes real network/queueing latency "
              f"AND any clock skew, not separated; recorded as-is, not absorbed)")
    for e in all_events:
        src = "monitoring" if e in mon_events else "remediation"
        extra = {k: v for k, v in e.items() if k not in ("ts", "step", "_log_event_ts_ms")}
        print(f"    [{e['ts']}] ({src}) {e['step']} {extra if extra else ''}")
    print(f"  laptop recv (UTC):     {median_run['recv_utc']}")
    if not all_events:
        print("  (no STEP log lines retrieved for this window -- see log group names above; "
              "CloudWatch Logs delivery lag or log group discovery may need a re-check.)")

    print("\n== Cost: measured call counts x published list prices (see demo.py constants) ==")
    print(f"  Runs this invocation: {len(a_samples)} direct, {len(b_samples)} native-invoke, "
          f"{len(c_control_samples)} full-A2A-control, {len(real_samples)} full-A2A-real")
    print("  AgentCore Runtime compute: billed per second of actual vCPU+memory consumption,")
    print(f"  1-second minimum, at ${PRICE_RUNTIME_VCPU_HOUR}/vCPU-hour + ${PRICE_RUNTIME_GB_HOUR}/GB-hour (v1 list price).")
    spend_after, tok_in_after, tok_out_after = estimate_spend_so_far_usd(cw)
    print(f"  Spend estimate after this run (session window, list-price-derived): ~${spend_after:.4f} "
          f"(in={tok_in_after:.0f} tok, out={tok_out_after:.0f} tok)")
    if spend_after >= SPEND_CAP_USD:
        print(f"  WARNING: spend cap (${SPEND_CAP_USD}) reached or exceeded during this run.", file=sys.stderr)

    print("\nDone.")


if __name__ == "__main__":
    main()
