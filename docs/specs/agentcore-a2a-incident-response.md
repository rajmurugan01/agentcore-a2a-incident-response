# Spec: AgentCore A2A cross-framework incident-response demo
Status: Tier 1 shipped and verified live 2026-10-22 (see "Tier 1 fix session note" below).
Tier 2 remains proposed only -- not approved, not built.
Date: 2026-10-19, revised 2026-10-22   Domain: Professional (public companion repo for rajmurugan.com)
Repo: agentcore-a2a-incident-response

## Problem / why now
**The claim this spec exists to test (rewritten 2026-10-22, approval condition 1):** two
agents built in different frameworks detect a real CloudWatch incident and hand it off
agent-to-agent over A2A on AgentCore -- not a script calling two agents in turn. Measured:
detection rate, hand-off latency, cost.

This is the grounding material for the next rajmurugan.com post; needs real measured
findings, not researched theory, per the site's Factual/Authenticity DoD gates.

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

- [x] `make incident` creates a tagged test metric (`put_metric_data`) and alarm, and drives it
      into `ALARM`. Verified 2026-10-22: `scripts/incident.py` created
      `a2a-incident-demo-checkout-api-test-errors` (namespace `A2AIncidentDemo`, metric
      `SyntheticErrorRate`, 10s high-resolution period) and drove it to `ALARM` for real in
      ~25s. Real gotcha hit and fixed in-session: the Makefile's region pin used `?=`, which is
      a no-op when the calling shell already exports `AWS_REGION` (mine did, to
      `ap-southeast-2`) -- the alarm was first created in the wrong region entirely. Fixed to
      an unconditional `:=` and re-ran correctly in `us-east-1`.
- [x] Detection is real: the monitoring agent calls `find_incidents_direct()`; each run's
      finding must contain the real alarm name and a metric value matching
      `describe-alarms`/`get-metric-statistics`. Misses count as failed runs and are reported,
      never retried away. Detection success rate is published. Verified: 5/5 pattern-(c)-real
      runs detected the real alarm and metric value for real (100%); see
      `docs/transcript.txt`.
- [x] Agent-to-agent hand-off: in pattern (c) the monitoring agent itself (running on
      AgentCore Runtime) fetches the remediation agent's card and sends `message/send` with its
      own OAuth token. `demo.py` only triggers the run. Patterns (a) and (b) stay as transport
      baselines (unchanged, still client-driven, since they exist to measure transport cost,
      not the hand-off). Verified live 2026-10-22, both container logs correlated in
      `docs/transcript.txt`'s hero timeline: `monitoring_agent/agent.py`'s
      `handoff_to_remediation_agent` tool fetches its own token, does `GET
      /.well-known/agent-card.json` and `POST /` (`message/send`) against the remediation
      runtime, and `demo.py`'s pattern-(c)-real path never calls the remediation runtime at
      all -- confirmed by reading the actual code path, not just the transcript.
- [x] `TEST_FINDING` is kept only as a labelled control run for like-for-like comparison with
      the 2026-10-21 numbers. `pattern_c_control_test_finding()` in `demo/demo.py`, explicitly
      labelled "CONTROL" in the transcript; median 18.831s vs the 2026-10-21 run's 19.116s --
      consistent, as expected for an unchanged transport path.
- [x] Timestamps per step, with sources: alarm `StateUpdatedTimestamp` (AWS, not the laptop
      clock), detection, card fetch, token, `message/send`, `classify`, `recommend` (UTC,
      container clock; skew recorded). Each run splits detection time vs hand-off time.
      Verified: `docs/incident.json` carries the real `StateUpdatedTimestamp` from
      `describe-alarms`; every container-side STEP log line carries its own
      `datetime.now(timezone.utc)`, pulled from CloudWatch Logs and shown in the hero
      timeline; the gap between the laptop's send time and the container's first STEP
      (+1.357s on the published hero run) is reported as-is, labelled as unseparated
      latency+skew, not claimed as pure clock skew.
- [x] Input/output tokens per model call logged for real; pinned model IDs recorded in the
      transcript (Claude 3.5 Haiku's EOL was already one gotcha from skipping this). Verified:
      both agents log a `model_usage` STEP line with real `inputTokens`/`outputTokens` (Strands
      `event_loop_metrics.accumulated_usage` via an `AfterInvocationEvent` hook; LangChain's
      `response.usage_metadata`) and the pinned model id
      `us.anthropic.claude-haiku-4-5-20251001-v1:0`, visible in `docs/transcript.txt`'s hero
      timeline.
- [x] 5 runs per pattern; all runs published in the transcript. Hero timeline uses the
      pre-declared MEDIAN pattern-(c) run, not the best one. Verified: all 20 runs (5 each,
      patterns a/b/c-control/c-real) are in `docs/transcript.txt`; the hero timeline is
      `real-1` at 25.270s, the sorted-median of [23.741, 25.270, 23.929, 26.418, 25.398] --
      selected by `sorted(...)[len//2]` in code before any run's content was read, not picked
      after the fact.
- [x] Spend cap: stop and report if spend exceeds ~$5, rather than running unbounded. Verified:
      `demo.py` checks real CloudWatch `AWS/Bedrock` usage against the $5 cap before and after
      each run; final session total was ~$0.245 (below the first build's bar, but this build
      also made many more model-invoking calls while debugging IAM -- see session note).
- [x] `make destroy` also removes the metric and alarm; teardown independently verified
      (`describe-alarms` / metric list empty), same discipline as the existing "Teardown"
      section. Verified 2026-10-22: `scripts/teardown_incident.py` deleted the alarm and
      `describe-alarms` confirmed it gone; `list-metrics` still shows the metric's metadata
      (CloudWatch has no delete-metric API at all -- confirmed, not assumed -- so this is
      expected AWS behaviour, documented rather than hidden, same honesty standard as the
      existing ECR-leftover note in README). Stack, both AgentCore runtimes, the Cognito pool,
      and the OAuth2 credential provider were all independently confirmed gone after
      `cdk destroy`.
- [x] **Approval condition 2 (2026-10-22):** the monitoring agent obtains the OAuth credential
      it now needs (to call the remediation agent itself, per the agent-to-agent criterion
      above) through AgentCore Identity's outbound auth / credential provider mechanism, with
      no client secret in code, environment variables, or the container image. **Outbound auth
      fit and worked, verified live** -- see the Build-session note below for the real,
      previously-UNVERIFIED mechanics this required (a `CognitoOauth2`-vendor
      `CfnOAuth2CredentialProvider`, `UserPoolClient.userPoolClientSecret` resolved by CDK's
      own custom resource rather than any literal, and the `@requires_access_token(...,
      auth_flow="M2M")` SDK decorator) and the real IAM surface it turned out to need (five
      iterations, not one guess) before the live call succeeded with zero secrets in
      `monitoring_agent`'s env or image. No fallback to Secrets Manager was needed. The
      first working IAM fix used two namespace wildcards; caught in review as an
      anti-pattern this fix exists to avoid and replaced with the exact four-ARN policy
      AWS's own devguide documents for this, re-verified live -- see the "Tier 1 fix
      session note" below for the full account.

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

## Tier 1 fix session note (2026-10-22)
Fixed the fake-incident problem this spec was reopened for. Full account in README.md
"Status"/"Sharp edges"/"Cost"; summary here:

- **Real incident, real detection, real agent-to-agent hand-off, all verified live.**
  `make incident` drives a real tagged CloudWatch alarm into `ALARM`; the monitoring
  agent's own `find_incidents_direct()` tool loop found it for real in all 5 pattern-(c)
  runs (100% detection rate, checked against `describe-alarms`/`get-metric-statistics`
  each time); the monitoring agent itself -- not `demo.py` -- fetched the remediation
  agent's card and sent `message/send` with its own OAuth token.
- **Approval condition 2 (outbound auth) worked, but needed five IAM iterations, not
  one -- and the first fix that deployed was itself a wildcard anti-pattern, caught and
  tightened in review.** `GetResourceOauth2Token`'s AccessDeniedException named a
  DIFFERENT exact resource ARN as the blocker on each of five live deploy-and-retry
  cycles, in this order: the credential provider ARN, the runtime's workload-identity
  ARN, the workload-identity-directory ARN, the token-vault ARN, and finally a
  Secrets-Manager `secretsmanager:GetSecretValue` on the actual
  AWS-Secrets-Manager-backed secret AgentCore Identity stores the Cognito client secret
  in (`bedrock-agentcore-identity!default/oauth2/<provider-name>-<suffix>`) -- a real,
  previously-undocumented-in-this-repo finding: the credential provider's secret isn't
  purely internal to AgentCore Identity, the CALLING ROLE needs direct Secrets Manager
  read access to it too. None of this was guessed; each fix was driven by reading the
  literal `AccessDeniedException` text out of CloudWatch Logs after adding a
  `token_fetch_FAILED` debug log line, not by re-reading AWS docs blind. The build's
  first working fix closed the four bedrock-agentcore denials with two namespace
  wildcards (`token-vault/*`, `workload-identity-directory/*`) -- flagged in review as an
  account-wide-within-service anti-pattern inconsistent with the whole point of this
  fix, and replaced with the exact four-ARN policy AWS's own devguide documents for
  this ("Scope down access to credential providers by workload identity"), using this
  stack's own real resource attributes (the auto-created workload identity's exact name,
  confirmed via the live AccessDeniedException text; the credential provider's exact
  `attrCredentialProviderArn`) rather than hand-typed strings. Re-verified live: a fresh
  deploy with the scoped policy, then two real pattern-(c) hand-off calls, both
  succeeded with zero AccessDeniedExceptions. The Secrets Manager grant stays a
  name-prefix wildcard (scoped to this one credential provider's name, not account- or
  service-wide) -- AWS's devguide gives no exact-ARN form for this secret, and the
  credential provider's own `attrClientSecretArn` attribute resolves to a structured
  value, not a bare ARN string (passing it into a policy `Resource` produced `"Syntax
  errors in policy"` at deploy time, caught live and reverted).
- **Two more real bugs, unrelated to outbound auth, found the same way:**
  (1) `monitoring_agent`'s container never actually had `a2a-sdk` installed -- a LATENT
  bug from the 2026-10-21 build, invisible until this session's new pattern-(c)-real path
  became the first thing to ever send a real A2A `message/send` to the monitoring
  runtime (the old build only ever drove the remediation runtime). (2) Strands'
  `A2AServer` builds its `A2AStarletteApplication` without wiring in
  `bedrock_agentcore.runtime.a2a`'s `BedrockCallContextBuilder`, so the Runtime's
  per-request `WorkloadAccessToken` header was never extracted into
  `BedrockAgentCoreContext` -- fixed with a small FastAPI middleware that does that
  extraction itself.
- **A Makefile region-pinning bug, caught immediately by its own safety net:** the
  region pin used `export AWS_REGION ?= us-east-1`, which is a no-op when the calling
  shell already exports `AWS_REGION` (mine did, to `ap-southeast-2`) -- `make incident`
  silently created the first test alarm in the wrong region. Caught because
  `scripts/incident.py` prints the region it's using; fixed to an unconditional `:=`.
- **No fallback to Secrets Manager was needed** -- the outbound-auth pattern worked once
  the full real IAM surface was granted. Spend for this entire session (deploy, five
  redeploys while fixing IAM, ~30 real model-invoking calls across two full `make demo`
  runs, destroy): ~$0.245, independently pulled from this session's real `AWS/Bedrock`
  CloudWatch usage metrics, well under the $5 cap.
