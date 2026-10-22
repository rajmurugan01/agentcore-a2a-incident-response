"""Tier 2 Gateway target. Sets an SSM Parameter Store feature flag to a new value.

This Lambda is only ever reached if AgentCore Gateway's Cedar policy (ENFORCE mode,
see cdk/lib/stack.ts's Tier2ApprovalPolicy) already authorised the call -- the policy
checks `context.input.parameter_name` (must be the ONE tagged test parameter) and
`context.input.approved` (must be the literal string "true") BEFORE the Gateway ever
invokes this function. This handler does NOT re-check "approved" itself: that would
just be code pretending to be the enforcement boundary the acceptance criteria asked
for Cedar to actually provide. What this handler does do, as defence in depth, is only
ever touch the exact parameter name it's given -- it has no IAM permission to touch
anything else (see MaintenanceFlagParam.grantRead/grantWrite in stack.ts), so even a
Cedar misconfiguration can't turn this into an arbitrary-parameter-write primitive.

Returns both the previous and the freshly-confirmed value so the caller can verify a
rollback actually took effect, not just that the PutParameter call returned success.
"""
import datetime
import json

import boto3

ssm = boto3.client("ssm")


def handler(event, context):
    parameter_name = event["parameter_name"]
    new_value = event["new_value"]

    before = ssm.get_parameter(Name=parameter_name)["Parameter"]["Value"]
    ssm.put_parameter(Name=parameter_name, Value=new_value, Type="String", Overwrite=True)
    # Re-read rather than trust the put_parameter call's own (empty) response -- the
    # same "verify, don't just invoke" standard the rollback acceptance criterion asks
    # demo/tier2_demo.py to apply from the outside, applied here too from the inside.
    confirmed = ssm.get_parameter(Name=parameter_name)["Parameter"]["Value"]

    result = {
        "parameter_name": parameter_name,
        "previous_value": before,
        "requested_value": new_value,
        "confirmed_value": confirmed,
        "executed_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        # Informational only -- carried through from the approval record for the
        # transcript/audit trail, never re-checked for authorization here (Cedar already
        # did that before this function was invoked at all).
        "approver": event.get("approver", "unknown"),
        "approved_at": event.get("approved_at", "unknown"),
    }
    print(json.dumps({"tier2_execution": result}))
    return result
