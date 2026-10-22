# Spec: AgentCore A2A cross-framework incident-response demo
Status: draft (pending approval -- do not build Tier 1 or Tier 2 until Raj signs off)
Date: 2026-10-19, revised 2026-10-22   Domain: Professional (public companion repo for rajmurugan.com)
Repo: agentcore-a2a-incident-response

## Problem / why now
Grounding material for the next rajmurugan.com post on what A2A actually changes for multi-agent systems -- needs real measured findings, not researched theory, per the site's Factual/Authenticity DoD gates.

## Evidence-review finding (2026-10-22) -- why this spec is reopened
The shipped build (criteria below, all originally checked off 2026-10-21) measures A2A
transport mechanics for real, but does NOT demonstrate an incident being detected or any
autonomous action, and the canonical post's "setup" section implied otherwise. Checked
directly against `demo.py` and `docs/transcript.txt`:
- `demo.py`'s `TEST_FINDING` is a hardcoded string (`"CPUUtilization alarm on service
  checkout-api..."`) fed to all three comms patterns. `checkout-api` is not a real service
  in the account.
- The monitoring agent's real detection path, `find_incidents_direct()` (a genuine Strands
  `Agent` wired to real, read-only boto3 CloudWatch tools in `cloudwatch_tools.py`), deployed
  successfully and reached `READY`, but `demo.py`'s `pattern_a_direct()` only asserts it
  exists and then calls `remediation.remediate_direct(TEST_FINDING)` directly -- the real
  detection path is never invoked.
- `docs/transcript.txt` has no wall-clock timestamps, only per-call elapsed seconds, so no
  genuine step-by-step timeline exists to build a hero diagram from.
- The remediation agent (`classify_finding` -> `recommend`) only ever returns a text
  recommendation; there is no tool-calling to any AWS mutation API and no ticketing
  integration, so "what stops it from being destructive" has no real answer to give --
  there is no actuation path to stop.
Decision (Raj, 2026-10-22): fix the demo so the incident story is real before any post
rewrite. Scope below, two tiers.

## Tier 1 acceptance criteria (required, build this first)
- [ ] `make incident` creates a real, tagged test CloudWatch metric (via `put_metric_data`)
      and an alarm on it, and pushes the metric above the alarm's threshold so it actually
      enters `ALARM` state.
- [ ] All three comms patterns (direct call / native invoke / full A2A) start from
      `find_incidents_direct()`'s REAL output (the monitoring agent actually calling
      `list_alarms_in_alarm_state()` and finding the real test alarm), not `TEST_FINDING`.
      `TEST_FINDING` is kept only as an explicitly labelled control run, so the latency/cost
      comparison in the post stays like-for-like against the original numbers.
- [ ] The transcript logs real wall-clock timestamps for each step of the real-incident run:
      alarm enters ALARM, monitoring agent detects it, agent-card fetch, OAuth token fetch,
      `message/send`, `classify_finding`, `recommend`. This is the data the hero diagram
      storyboard gets built from -- no step, no diagram claim.
- [ ] 5 runs per pattern, same as the current build.
- [ ] `make destroy` removes the test metric and alarm too (not just the AgentCore/Cognito
      stack), and teardown is independently verified (describe-alarms / list metrics show
      nothing left), same discipline as the existing "Teardown" section.

## Tier 2 (proposed only -- do NOT build without separate approval)
One safe, reversible action behind human approval: the remediation agent PROPOSES an action
(e.g. setting an SSM Parameter Store feature-flag value, or resetting the test alarm), a
human approves it, and only then does it execute -- through an AgentCore Gateway with a
Cedar policy scoped to tagged test resources only. Show one denied call (wrong resource tag)
and one allowed call, same pattern as the sibling `agentcore-acts-as-the-user` repo's Cedar
demo. Pitch: "two frameworks detect and propose; a human approves; policy enforces" --
deliberately short of full autonomy.

**Rough estimate, not yet verified against a live build:**
- Cost: AgentCore Gateway $5/million invocations, Policy $25/million authorisations
  (published list price, confirmed against aws.amazon.com/bedrock/agentcore/pricing/
  2026-10-21 in this session) -- a handful of test calls is well under $1 total, consistent
  with every other build in this repo. SSM Parameter Store read/write is free-tier eligible.
- Time/risk: comparable order of magnitude to Tier 1's own build (new real AWS primitives,
  expect real gotchas), but lower unknown-risk than Tier 1 was, since Raj's
  `agentcore-acts-as-the-user` repo already built Gateway + Cedar once and documented its
  real sharp edges (a role-vs-resource creation-order dependency, a Cedar `like` match
  collision) -- those are known traps to check for up front this time, not blind territory.

## Non-goals
- Not the AWS sample 1:1 (no Google ADK host, no JIRA, no Tavily).
- Not a production reference architecture.
- Not the blog post itself.
- Unattended remediation of real resources -- out of scope, not being built, not proposed.
- No new paid third-party API signups, Tier 1 or Tier 2.

## Build-session note (2026-10-20)
Code (CDK stack, both agents, demo harness, Makefile, README) was written and committed
in this session. Deploy/demo/destroy were NOT run: the `personal` AWS SSO profile's
session token had expired and refreshing it requires an interactive browser login that
could not be completed headlessly. No acceptance criterion above was checked off --
nothing had been deployed or measured. Blocked status, not shipped.

## Deploy-session note (2026-10-21)
Resumed with a fresh SSO session. Deployed, measured, and tore down for real; see
README.md "Status", "Sharp edges I hit", and "Cost" for the full account. Seven real bugs
were found and fixed along the way (CDK/IAM/CloudFormation ordering issues, an A2A
port convention, a boto3-vs-OAuth limitation, a Cognito endpoint mismatch, an EOL model,
and a cross-Region inference-profile IAM gap) -- none hypothetical, all confirmed against
the live stack. All five acceptance criteria now pass; see the checked boxes above for
exactly what was verified and any caveat. AWS resources were destroyed afterwards and
teardown was independently verified (not just the CLI exit code) -- see README
"Teardown".
