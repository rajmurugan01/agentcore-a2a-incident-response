#!/usr/bin/env python3
"""`make tier2-demo` -- Tier 2: human-approved, Cedar-policy-enforced reversible
remediation (approved by Raj 2026-10-22, "go tier 2").

Pitch: two frameworks detect and propose; a human approves; policy enforces --
deliberately short of full autonomy. This script exercises the four required
acceptance criteria from docs/specs/agentcore-a2a-incident-response.md's Tier 2
section, against the real deployed Gateway + Cedar policy, not a mock:

  1. The remediation agent's REAL code (agents/remediation_agent/agent.py's
     propose_remediation_action(), imported in-process the same way demo.py's
     pattern (a) imports agent.py) PROPOSES a structured action from a real
     runbook match. It is never given any credential able to execute one --
     see cdk/lib/stack.ts: only Tier2ExecutorClient's token can even reach the
     Tier 2 Gateway's authorizer.
  2. TEST: calling the Gateway tool WITHOUT approval (approved="false") is denied
     by the Cedar policy itself, not by this script choosing not to call it.
  3. TEST: calling the Gateway tool with approved="true" but against a real,
     deliberately UNTAGGED SSM parameter (outside the one parameter_name the
     Cedar policy permits) is denied.
  4. A REAL approval gate: prompts interactively for y/n when a human is at the
     keyboard; logs the decision, a UTC timestamp, and the approver's actual IAM
     identity (`aws sts get-caller-identity`, never a hardcoded name) to
     docs/tier2_approval_log.jsonl. In a non-interactive/automated run (no TTY),
     falls back to the TIER2_APPROVAL env var and records that fact in the same
     log line -- this script never pretends a human typed something that didn't
     happen.
  5. Once approved, the SAME Gateway call now succeeds; the SSM parameter's new
     value is independently re-read via a direct ssm:GetParameter call (NOT the
     Lambda's own return value) to confirm it actually changed.
  6. ROLLBACK: resets the parameter back to its PRE-action value through the
     same approved-execution path, and independently re-verifies (again via a
     direct ssm:GetParameter call) that it actually landed back at that value --
     "rollback is itself verified, not just invoked."

Needs `make deploy` to have been run first (reads cdk/cdk.outputs.json).
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

import boto3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agents", "remediation_agent"))

OUT_PATH = os.path.join(ROOT, "cdk", "cdk.outputs.json")
INCIDENT_PATH = os.path.join(ROOT, "docs", "incident.json")
APPROVAL_LOG_PATH = os.path.join(ROOT, "docs", "tier2_approval_log.jsonl")

TARGET = "tier2-remediation"
TOOL = "set_feature_flag"
TOOL_NAME = f"{TARGET}___{TOOL}"


def load_outputs():
    with open(OUT_PATH) as f:
        data = json.load(f)
    return data[next(iter(data))]


def _load_module(name, path):
    """Same technique demo.py uses: load agent.py by explicit file path under its own
    module name, since both agents' entrypoint files happen to be named agent.py."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_client_credentials_token(domain_url, client_id, client_secret, scope):
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = f"grant_type=client_credentials&scope={scope}".encode()
    req = urllib.request.Request(
        f"{domain_url}/oauth2/token", data=body, method="POST",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["access_token"]


def call_gateway(gateway_url, token, arguments):
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": TOOL_NAME, "arguments": arguments},
    }
    req = urllib.request.Request(
        gateway_url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def verdict(status, body):
    if body.get("result", {}).get("isError"):
        return "DENIED", json.dumps(body["result"].get("content"))[:220]
    if "error" in body or status >= 400:
        return "DENIED", json.dumps(body.get("error") or body)[:220]
    return "ALLOWED", json.dumps(body.get("result", {}).get("content"))[:220]


def get_caller_identity_arn():
    return boto3.client("sts").get_caller_identity()["Arn"]


def record_approval(action_description, parameter_name, new_value):
    """The real approval gate (Tier 2 criterion: 'a logged y/n with a timestamp and an
    identified approver, not an implicit step'). Prompts interactively when a human is
    at the keyboard. This repo's own build/test sessions run non-interactively (no
    human physically present to type at a live prompt during an automated session) --
    in that case it falls back to the TIER2_APPROVAL env var and the log line records
    `source` as the non-interactive fallback, honestly, rather than claiming an
    interactive prompt that didn't happen. The approver identity itself is never
    hardcoded either way: it's read live from `aws sts get-caller-identity`, which is
    the real IAM principal running this script (Raj's own SSO role in this account)."""
    print(f"\n{'=' * 70}\nAPPROVAL REQUIRED\n{'=' * 70}")
    print(action_description)
    print(f"  parameter: {parameter_name} -> {new_value!r}")
    source = "interactive_prompt"
    try:
        if not sys.stdin.isatty():
            raise EOFError("stdin is not a TTY")
        decision = input("Approve this action? [y/n]: ").strip().lower()
    except EOFError:
        decision = os.environ.get("TIER2_APPROVAL", "n").strip().lower()
        source = "env_var_non_interactive(TIER2_APPROVAL)"
        print(f"  (non-interactive session -- no human TTY present; decision supplied via "
              f"{source}: {decision!r}. Run interactively to approve at a real prompt instead.)")
    approver_arn = get_caller_identity_arn()
    ts = datetime.now(timezone.utc).isoformat()
    record = {
        "ts": ts,
        "approver_arn": approver_arn,
        "action": action_description,
        "parameter_name": parameter_name,
        "new_value": new_value,
        "decision": decision,
        "approved": decision == "y",
        "source": source,
    }
    os.makedirs(os.path.dirname(APPROVAL_LOG_PATH), exist_ok=True)
    with open(APPROVAL_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"  LOGGED: approver={approver_arn} decision={decision!r} approved={record['approved']} "
          f"at {ts} -> {APPROVAL_LOG_PATH}")
    return record


def main():
    outputs = load_outputs()
    region = outputs["Region"]
    gateway_url = outputs["Tier2GatewayUrl"]
    maintenance_param = outputs["MaintenanceFlagParameterName"]
    untagged_param = outputs["UntaggedFlagParameterName"]
    user_pool_id = outputs["UserPoolId"]
    executor_client_id = outputs["Tier2ExecutorClientId"]

    cog = boto3.client("cognito-idp", region_name=region)
    executor_secret = cog.describe_user_pool_client(
        UserPoolId=user_pool_id, ClientId=executor_client_id,
    )["UserPoolClient"]["ClientSecret"]
    token = get_client_credentials_token(
        outputs["CognitoDomainUrl"], executor_client_id, executor_secret, "incident-response/tier2.execute",
    )
    ssm = boto3.client("ssm", region_name=region)

    print("=" * 70)
    print("Tier 2: human-approved, Cedar-policy-enforced reversible remediation")
    print("=" * 70)

    # ── Step 1: the remediation agent PROPOSES an action -- its real, deployed code,
    # imported in-process, not fabricated by this script. ─────────────────────────
    remediation = _load_module("remediation_agent_module", os.path.join(ROOT, "agents", "remediation_agent", "agent.py"))
    if os.path.exists(INCIDENT_PATH):
        incident = json.load(open(INCIDENT_PATH))
        finding = (
            f"Elevated error rate on {incident['dimension_value']}: {incident['metric_name']} reached "
            f"{incident['pushed_test_value']} (threshold {incident['threshold']}), alarm "
            f"{incident['alarm_name']} has been in ALARM since {incident['state_updated_timestamp_aws']}."
        )
    else:
        print("NOTE: docs/incident.json not found (run `make incident` first for a real finding) -- "
              "using a representative error-rate finding string instead.")
        finding = "Elevated 5xx error rate on checkout-api-test; error rate has been above threshold for several minutes."

    proposal = remediation.propose_remediation_action(finding)
    if proposal is None:
        print("FATAL: propose_remediation_action() returned no action for this finding -- "
              "check runbooks.json's error_rate entry has a proposed_action.", file=sys.stderr)
        sys.exit(1)
    print(f"\n== Remediation agent's PROPOSAL (matched runbook: {proposal['runbook_title']}) ==")
    print(f"  finding: {proposal['finding']}")
    print(json.dumps(proposal["proposed_action"], indent=2))

    results = []

    # ── TEST (required criterion): attempting the action WITHOUT approval is denied by
    # the Cedar policy itself, not by this script simply choosing not to call it. ──────
    print("\n== TEST 1: attempt the action WITHOUT approval ==")
    before1 = ssm.get_parameter(Name=maintenance_param)["Parameter"]["Value"]
    status, body = call_gateway(gateway_url, token, {
        "parameter_name": maintenance_param, "new_value": "true", "approved": "false",
    })
    v, detail = verdict(status, body)
    after1 = ssm.get_parameter(Name=maintenance_param)["Parameter"]["Value"]
    ok = v == "DENIED" and after1 == before1
    results.append(("no-approval attempt is denied (by Cedar, not by prompt text)", ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {v} -- {detail}")
    print(f"  parameter unchanged: {before1!r} -> {after1!r}")

    # ── TEST (required criterion): approved=true but against a real, deliberately
    # UNTAGGED resource is denied by the Cedar policy. ──────────────────────────────
    print("\n== TEST 2: approved=true, but against the UNTAGGED resource ==")
    before2 = ssm.get_parameter(Name=untagged_param)["Parameter"]["Value"]
    status, body = call_gateway(gateway_url, token, {
        "parameter_name": untagged_param, "new_value": "true", "approved": "true",
    })
    v, detail = verdict(status, body)
    after2 = ssm.get_parameter(Name=untagged_param)["Parameter"]["Value"]
    ok = v == "DENIED" and after2 == before2
    results.append(("untagged-resource attempt is denied by Cedar", ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {v} -- {detail}")
    print(f"  parameter unchanged: {before2!r} -> {after2!r}")

    # ── Real approval gate, logged. ─────────────────────────────────────────────────
    action_desc = f"Proposed by remediation agent: {proposal['proposed_action']['description']}"
    approval = record_approval(action_desc, maintenance_param, proposal["proposed_action"]["new_value"])

    # ── TEST (required criterion, implicitly): once approved, the SAME call now
    # succeeds, and the new value is independently re-read (not trusted from the
    # Lambda's own return payload). ─────────────────────────────────────────────────
    print("\n== TEST 3: approved call against the correct, tagged resource ==")
    prior_value = ssm.get_parameter(Name=maintenance_param)["Parameter"]["Value"]
    if approval["approved"]:
        status, body = call_gateway(gateway_url, token, {
            "parameter_name": maintenance_param,
            "new_value": proposal["proposed_action"]["new_value"],
            "approved": "true",
            "approver": approval["approver_arn"],
            "approved_at": approval["ts"],
        })
        v, detail = verdict(status, body)
    else:
        v, detail = "DENIED (not approved)", "operator declined approval -- action not attempted"
    executed_value = ssm.get_parameter(Name=maintenance_param)["Parameter"]["Value"]
    ok = approval["approved"] and v == "ALLOWED" and executed_value == proposal["proposed_action"]["new_value"]
    results.append(("approved execution succeeds, independently verified", ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {v} -- {detail}")
    print(f"  INDEPENDENTLY VERIFIED via a direct ssm:GetParameter call (not the Lambda's own "
          f"return value): {prior_value!r} -> {executed_value!r}")

    # ── Required criterion: the rollback is itself verified, not just invoked. ───────
    print("\n== ROLLBACK: reset the flag back to its PRIOR value ==")
    rollback_ok = False
    rollback_detail = "not attempted"
    if approval["approved"] and executed_value == proposal["proposed_action"]["new_value"]:
        rollback_desc = f"ROLLBACK of the above action: reset {maintenance_param} back to {prior_value!r}."
        rollback_approval = record_approval(rollback_desc, maintenance_param, prior_value)
        if rollback_approval["approved"]:
            status, body = call_gateway(gateway_url, token, {
                "parameter_name": maintenance_param,
                "new_value": prior_value,
                "approved": "true",
                "approver": rollback_approval["approver_arn"],
                "approved_at": rollback_approval["ts"],
            })
            v, rollback_detail = verdict(status, body)
            rolled_back_value = ssm.get_parameter(Name=maintenance_param)["Parameter"]["Value"]
            rollback_ok = v == "ALLOWED" and rolled_back_value == prior_value
            print(f"  [{'PASS' if rollback_ok else 'FAIL'}] {v} -- {rollback_detail}")
            print(f"  INDEPENDENTLY VERIFIED via a direct ssm:GetParameter call: now {rolled_back_value!r}, "
                  f"matches the pre-action value {prior_value!r}: {rolled_back_value == prior_value}")
        else:
            rollback_detail = "rollback not approved -- not executed"
            print(f"  {rollback_detail}.")
    else:
        print("  skipped -- the forward action did not succeed, nothing to roll back.")
    results.append(("rollback executes and is independently verified", rollback_ok, rollback_detail))

    print("\n" + "=" * 70)
    n_pass = sum(1 for _, ok, _ in results if ok)
    for label, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label} -- {detail}")
    print(f"\n{n_pass}/{len(results)} required Tier 2 behaviours verified.")
    sys.exit(0 if n_pass == len(results) else 1)


if __name__ == "__main__":
    main()
