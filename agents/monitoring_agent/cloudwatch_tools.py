"""Local CloudWatch tools for the monitoring agent, modelled on the tool patterns in
AWS's sample-fully-autonomous-incident-response monitoring_agent (lambda_function.py /
config.yaml), but called in-process with boto3 rather than fronted by an AgentCore
Gateway -- the Gateway/Memory/Identity primitives are out of scope for this repo
(see README "Not in scope"); only Runtime + A2A protocol are exercised.
"""
import os
import boto3
from strands import tool

REGION = os.environ.get("AWS_REGION", "us-east-1")
_cw = boto3.client("cloudwatch", region_name=REGION)
_logs = boto3.client("logs", region_name=REGION)


@tool
def list_alarms_in_alarm_state() -> str:
    """List every CloudWatch alarm currently in ALARM state, with its reason."""
    resp = _cw.describe_alarms(StateValue="ALARM")
    alarms = resp.get("MetricAlarms", []) + resp.get("CompositeAlarms", [])
    if not alarms:
        return "No alarms are currently in ALARM state."
    lines = []
    for a in alarms:
        lines.append(f"- {a['AlarmName']}: {a.get('StateReason', 'no reason recorded')}")
    return "\n".join(lines)


@tool
def get_metric_statistics(namespace: str, metric_name: str, dimension_name: str,
                           dimension_value: str, minutes: int = 15) -> str:
    """Get recent CloudWatch metric statistics (Average, Maximum) for one metric/dimension pair."""
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    resp = _cw.get_metric_statistics(
        Namespace=namespace, MetricName=metric_name,
        Dimensions=[{"Name": dimension_name, "Value": dimension_value}],
        StartTime=start, EndTime=end, Period=60, Statistics=["Average", "Maximum"],
    )
    points = sorted(resp.get("Datapoints", []), key=lambda d: d["Timestamp"])
    if not points:
        return f"No datapoints for {namespace}/{metric_name} in the last {minutes} minutes."
    latest = points[-1]
    return f"{namespace}/{metric_name} latest: avg={latest['Average']:.2f} max={latest['Maximum']:.2f} at {latest['Timestamp'].isoformat()}"


@tool
def filter_log_events(log_group_name: str, filter_pattern: str = "", minutes: int = 15, limit: int = 20) -> str:
    """Search a CloudWatch Logs log group for recent events matching a filter pattern."""
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    try:
        resp = _logs.filter_log_events(
            logGroupName=log_group_name,
            startTime=int(start.timestamp() * 1000),
            endTime=int(end.timestamp() * 1000),
            filterPattern=filter_pattern,
            limit=limit,
        )
    except _logs.exceptions.ResourceNotFoundException:
        return f"Log group {log_group_name} does not exist."
    events = resp.get("events", [])
    if not events:
        return f"No matching log events in {log_group_name} in the last {minutes} minutes."
    return "\n".join(e["message"].strip() for e in events[-limit:])


MONITORING_TOOLS = [list_alarms_in_alarm_state, get_metric_statistics, filter_log_events]
