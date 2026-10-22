#!/usr/bin/env python3
"""`make incident` -- creates a real, tagged test CloudWatch metric + alarm and drives it
into ALARM for real (Tier 1 criterion 1). Run with the same AWS_PROFILE as the rest of
this repo; needs cloudwatch:PutMetricData/PutMetricAlarm/DescribeAlarms on whatever
identity is running it (the `personal` SSO admin role already has this).

Design notes (so the next session doesn't have to re-derive these):
- Uses a high-resolution (StorageResolution=1) custom metric so the alarm's Period can be
  10 seconds instead of the 60s minimum for standard-resolution metrics -- gets to a real
  ALARM state transition in low-single-digit minutes instead of needing several 60s
  evaluation windows.
- The metric/dimension/alarm names are explicitly test-labelled (not a real "checkout-api"
  service in this account) and the alarm resource itself is TAGGED at creation
  (cloudwatch alarms support resource tags; custom metrics do not -- there is no
  "tag a metric" primitive in CloudWatch, only alarms/dashboards).
- Polls describe_alarms for the REAL StateUpdatedTimestamp (AWS's own clock, not this
  script's) once ALARM is reached -- this is the source of truth Tier 1 criterion 5 asks
  for, not this script's local "when did I observe it" time (also recorded, separately
  labelled, for comparison).
- Never silently retries past a failure to reach ALARM: if the timeout elapses first, this
  exits non-zero and prints exactly what state the alarm was actually in.
- Writes docs/incident.json -- the ground-truth record demo.py and the final report use
  to check the monitoring agent's findings against reality, and that `make destroy`'s
  teardown step reads to know what to tear down.
"""
import datetime as dt
import json
import os
import sys
import time

import boto3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "incident.json")
REGION = os.environ.get("AWS_REGION", "us-east-1")

NAMESPACE = "A2AIncidentDemo"
METRIC_NAME = "SyntheticErrorRate"
DIMENSION_NAME = "Service"
DIMENSION_VALUE = "checkout-api-test"
ALARM_NAME = "a2a-incident-demo-checkout-api-test-errors"
THRESHOLD = 80.0
TEST_VALUE = 97.0
PERIOD_SECONDS = 10
PUSH_INTERVAL_SECONDS = 5
POLL_INTERVAL_SECONDS = 10
TIMEOUT_SECONDS = 300  # 5 minutes -- generous; a 10s-period high-res alarm usually trips in 1-2 min.


def main() -> int:
    cw = boto3.client("cloudwatch", region_name=REGION)
    sts = boto3.client("sts", region_name=REGION)
    account = sts.get_caller_identity()["Account"]

    print(f"Creating/updating alarm {ALARM_NAME!r} on {NAMESPACE}/{METRIC_NAME} "
          f"({DIMENSION_NAME}={DIMENSION_VALUE}) in {REGION}, account {account}.")

    cw.put_metric_alarm(
        AlarmName=ALARM_NAME,
        AlarmDescription=(
            "TEST alarm for agentcore-a2a-incident-response Tier 1 (2026-10-22). "
            "Synthetic metric, not a real production service -- tagged and torn down by "
            "`make incident` / `make destroy` in that repo."
        ),
        Namespace=NAMESPACE,
        MetricName=METRIC_NAME,
        Dimensions=[{"Name": DIMENSION_NAME, "Value": DIMENSION_VALUE}],
        Statistic="Average",
        Period=PERIOD_SECONDS,
        EvaluationPeriods=1,
        Threshold=THRESHOLD,
        ComparisonOperator="GreaterThanThreshold",
        # "ignore" (not "notBreaching"): once in ALARM, a 10s-period high-res metric would
        # otherwise flip back toward OK within ~10-20s of this script's last push (missing
        # data treated as not-breaching). "ignore" holds the alarm at its last evaluated
        # state when data is missing, so it stays in ALARM for the whole `make demo` run
        # (several minutes, multiple detection calls) without needing a background pusher
        # process running for the full demo duration.
        TreatMissingData="ignore",
        Tags=[
            {"Key": "Project", "Value": "agentcore-a2a-incident-response"},
            {"Key": "Purpose", "Value": "tier1-incident-test"},
            {"Key": "CreatedBy", "Value": "scripts/incident.py"},
        ],
    )

    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_push = 0.0
    final_state = None
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now - last_push >= PUSH_INTERVAL_SECONDS:
            cw.put_metric_data(
                Namespace=NAMESPACE,
                MetricData=[{
                    "MetricName": METRIC_NAME,
                    "Dimensions": [{"Name": DIMENSION_NAME, "Value": DIMENSION_VALUE}],
                    "Value": TEST_VALUE,
                    "Unit": "Percent",
                    "StorageResolution": 1,
                    "Timestamp": dt.datetime.now(dt.timezone.utc),
                }],
            )
            last_push = now
            print(f"  pushed datapoint value={TEST_VALUE} at {dt.datetime.now(dt.timezone.utc).isoformat()}")

        resp = cw.describe_alarms(AlarmNames=[ALARM_NAME])
        alarms = resp.get("MetricAlarms", [])
        if alarms:
            final_state = alarms[0]
            state = final_state["StateValue"]
            print(f"  alarm state: {state} ({final_state.get('StateReason', '')[:100]})")
            if state == "ALARM":
                break
        time.sleep(POLL_INTERVAL_SECONDS)
    else:
        pass

    observed_at = dt.datetime.now(dt.timezone.utc).isoformat()

    if not final_state or final_state["StateValue"] != "ALARM":
        print(
            f"\nFAILED: alarm did not reach ALARM state within {TIMEOUT_SECONDS}s. "
            f"Last known state: {final_state.get('StateValue') if final_state else 'unknown'}. "
            "Not retrying silently -- fix the metric/alarm config and re-run `make incident`.",
            file=sys.stderr,
        )
        return 1

    record = {
        "alarm_name": ALARM_NAME,
        "namespace": NAMESPACE,
        "metric_name": METRIC_NAME,
        "dimension_name": DIMENSION_NAME,
        "dimension_value": DIMENSION_VALUE,
        "threshold": THRESHOLD,
        "pushed_test_value": TEST_VALUE,
        "region": REGION,
        "account": account,
        # Ground truth, AWS's own clock -- this is the timestamp Tier 1 criterion 5 asks
        # for ("the alarm's actual StateUpdatedTimestamp from AWS, not the laptop clock").
        "state_updated_timestamp_aws": final_state["StateUpdatedTimestamp"].isoformat(),
        "state_reason_aws": final_state.get("StateReason"),
        # This script's own laptop-clock observation of when it NOTICED the ALARM state --
        # kept separate and explicitly labelled so the two are never conflated.
        "observed_by_incident_script_at_laptop_clock": observed_at,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(record, f, indent=2)

    print(f"\nALARM reached ALARM state for real. AWS StateUpdatedTimestamp: "
          f"{record['state_updated_timestamp_aws']}")
    print(f"Ground-truth record written to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
