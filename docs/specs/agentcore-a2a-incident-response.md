# Spec: AgentCore A2A cross-framework incident-response demo
Status: draft (pending approval -- do not build Tier 1 or Tier 2 until Raj signs off)
Date: 2026-10-19, revised 2026-10-22   Domain: Professional (public companion repo for rajmurugan.com)
Repo: agentcore-a2a-incident-response

## Problem / why now
Grounding material for the next rajmurugan.com post on what A2A actually changes for multi-agent systems -- needs real measured findings, not researched theory, per the site's Factual/Authenticity DoD gates.

**The claim the post will make, which these criteria exist to test (added 2026-10-22):** two
agents, built in different frameworks, detect a real CloudWatch incident and hand it off to
each other over A2A on AgentCore -- not a script calling two agents in turn. Measured:
detection success rate, hand-off latency, cost.

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

## Tier 1 acceptance criteria (required)

**Review round 2 (2026-10-22): the round-1 draft above would still have shipped a demo that
doesn't tell the story.** Four must-fix problems, with the reasoning kept here since the next
build session needs the why, not just the checklist:

1. **The hand-off still wasn't agent to agent.** In every pattern, `demo.py` is the client: it
   calls the monitoring agent, then calls the remediation agent itself. Real detection alone
   doesn't fix this -- the post still couldn't honestly say "a Strands agent handed the
   incident to a LangGraph agent over A2A", because the hand-off is actually scripted from
   outside both agents. Fixed by requiring the monitoring agent itself to make the
   `message/send` call in pattern (c), using its own token. `demo.py` only triggers the run.
2. **Detection had no pass/fail test.** An LLM-driven agent can miss or hallucinate a finding.
   Fixed by requiring each run's finding to be checked against ground truth
   (`describe-alarms`/`get-metric-statistics`), with misses counted and reported, never
   silently retried.
3. **"The best run" was cherry-picking.** Pre-declare the median pattern-(c) run as the hero
   timeline before looking at results, and publish every run, not just the best one.
4. **Timestamp provenance was unspecified.** "Alarm enters ALARM" has to come from AWS's own
   `StateUpdatedTimestamp`, not the laptop clock running the script; in-runtime steps log UTC
   from the container; any clock skew between the two gets recorded, not silently absorbed.

Replacement criteria (supersede the round-1 list above):

- [ ] `make incident` creates a tagged test metric (`put_metric_data`) and alarm, and drives it
      into `ALARM`.
- [ ] Detection is real: the monitoring agent calls `find_incidents_direct()`; each run's
      finding must contain the real alarm name and a metric value matching
      `describe-alarms`/`get-metric-statistics`. Misses count as failed runs and are reported,
      never retried away. Detection success rate is published.
- [ ] Agent-to-agent hand-off: in pattern (c) the monitoring agent itself (running on
      AgentCore Runtime) fetches the remediation agent's card and sends `message/send` with its
      own OAuth token. `demo.py` only triggers the run. Patterns (a) and (b) stay as transport
      baselines (unchanged, still client-driven, since they exist to measure transport cost,
      not the hand-off).
- [ ] `TEST_FINDING` is kept only as a labelled control run for like-for-like comparison with
      the 2026-10-21 numbers.
- [ ] Timestamps per step, with sources: alarm `StateUpdatedTimestamp` (AWS, not the laptop
      clock), detection, card fetch, token, `message/send`, `classify`, `recommend` (UTC,
      container clock; skew recorded). Each run splits detection time vs hand-off time.
- [ ] Input/output tokens per model call logged for real; pinned model IDs recorded in the
      transcript (Claude 3.5 Haiku's EOL was already one gotcha from skipping this).
- [ ] 5 runs per pattern; all runs published in the transcript. Hero timeline uses the
      pre-declared MEDIAN pattern-(c) run, not the best one.
- [ ] Spend cap: stop and report if spend exceeds ~$5, rather than running unbounded.
- [ ] `make destroy` also removes the metric and alarm; teardown independently verified
      (`describe-alarms` / metric list empty), same discipline as the existing "Teardown"
      section.

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

**Specifics for when this is approved (added 2026-10-22, review round 2), so "approved" is
unambiguous when it happens:**
- Approval is a logged y/n with a timestamp and an identified approver, not an implicit step.
- One test shows the agent attempting the action WITHOUT approval being denied by code or
  policy -- not merely discouraged in a prompt. The prompt-only "do not take an automated
  remediation action" line already in `runbooks.json`'s `default` entry does not satisfy this;
  it is advisory text, not an enforced gate.
- One test shows a call against an untagged resource being denied by the Cedar policy.
- The rollback (resetting the SSM flag / alarm back to its prior state) is itself verified,
  not just invoked.

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
