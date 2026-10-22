# Spec: AgentCore A2A cross-framework incident-response demo
Status: Tier 1 shipped and verified live 2026-10-22 (see "Tier 1 fix session note" below).
Tier 2 shipped and verified live 2026-10-22 (see "Tier 2 build session note" below).
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

## Tier 2 (approved by Raj 2026-10-22, "go tier 2" -- shipped and verified live 2026-10-22)
One safe, reversible action behind human approval: the remediation agent PROPOSES an action
(e.g. setting an SSM Parameter Store feature-flag value, or resetting the test alarm), a
human approves it, and only then does it execute -- through an AgentCore Gateway with a
Cedar policy scoped to tagged test resources only. Show one denied call (wrong resource tag)
and one allowed call, same pattern as the sibling `agentcore-acts-as-the-user` repo's Cedar
demo. Pitch: "two frameworks detect and propose; a human approves; policy enforces" --
deliberately short of full autonomy.

**Built: SSM Parameter Store feature flag** (`/a2a-incident-demo/feature-flags/
checkout-api-test-maintenance-mode`), not an alarm reset -- cleaner to scope a Cedar policy
to one specific, genuinely-tagged resource name than to a shared alarm also used by Tier 1.
The remediation agent's `error_rate` runbook match now carries a `proposed_action` (see
`runbooks.json`); `agents/remediation_agent/agent.py`'s `propose_remediation_action()`
returns it as a structured proposal and has NO AWS credential able to execute one -- only
`scripts/tier2_demo.py`'s separate `Tier2ExecutorClient` Cognito credential can even reach
the Tier 2 Gateway's JWT authorizer at all.

**Acceptance criteria, all verified live 2026-10-22 against the real deployed stack**
(`make deploy` -> `make incident` -> `TIER2_APPROVAL=y make tier2-demo`; full transcript in
`docs/tier2_transcript.txt`, approval log in `docs/tier2_approval_log.jsonl`):

- [x] Approval is a logged y/n with a timestamp and an identified approver, not an implicit
      step. Verified: `docs/tier2_approval_log.jsonl` has one line per approval decision,
      e.g. `{"ts": "2026-10-22T08:48:32.466648+00:00", "approver_arn":
      "arn:aws:sts::<redacted-account-id>:assumed-role/AWSReservedSSO_AdministratorAccess_.../raj",
      "decision": "y", "approved": true, "source":
      "env_var_non_interactive(TIER2_APPROVAL)", ...}`. The approver identity is never
      hardcoded -- it's read live from `aws sts get-caller-identity` at approval time.
      `scripts/tier2_demo.py`'s `record_approval()` prompts interactively (`input()`) when a
      real TTY is attached; this build session ran non-interactively (no human physically at
      a live prompt during an automated session), so it fell back to the `TIER2_APPROVAL`
      env var -- the log line records that fact (`source` field) rather than claiming an
      interactive approval that didn't happen. Raj's own prior "go tier 2" authorised
      building and exercising this flow in this session; the per-action log is the
      mechanism, not a substitute for it.
- [x] One test shows the agent attempting the action WITHOUT approval being denied by code
      or policy, not merely discouraged in a prompt. Verified: calling the Tier 2 Gateway's
      `tier2-remediation___set_feature_flag` tool with `approved: "false"` against the
      correct, tagged parameter returned `{"code": -32002, "message": "Tool Execution
      Denied: Tool call not allowed due to policy enforcement [No policy applies to the
      request (denied by default).]"}` -- a real Cedar `AuthorizeAction` default-deny, not
      an application-level check. The SSM parameter's value was independently re-read via a
      direct `ssm:GetParameter` call before and after and confirmed unchanged
      (`'false' -> 'false'`).
- [x] One test shows a call against an untagged resource being denied by the Cedar policy.
      Verified: a second, real, deliberately UNTAGGED SSM parameter
      (`/a2a-incident-demo/feature-flags/untagged-example`) was created by the same stack;
      calling the tool with `approved: "true"` but `parameter_name` set to that parameter
      got the identical Cedar default-deny response. Independently confirmed via
      `aws ssm list-tags-for-resource` that this parameter carries only the automatic
      CloudFormation stack tags (no `Project`/`Purpose` business tags), vs. the approved
      parameter's genuine `Project=agentcore-a2a-incident-response` /
      `Purpose=tier2-remediation-test` tags -- "untagged" is a real, checked property of the
      resource, not just a string the Cedar policy happens to reject.
- [x] The rollback (resetting the SSM flag back to its prior state) is itself verified, not
      just invoked. Verified: after the approved forward action (`'false' -> 'true'`,
      independently re-read via `ssm:GetParameter`), a SECOND logged approval authorised the
      rollback call (`new_value: "false"`), which also went through the same Gateway +
      Cedar path (not a direct SSM call bypassing the policy) and was independently
      re-verified via a THIRD direct `ssm:GetParameter` call to confirm it landed back at
      `'false'`. Cross-checked against the Lambda's own CloudWatch Logs
      (`/aws/lambda/AgentcoreA2AIncidentRespo-SetFeatureFlagFn...`), pulled directly from AWS
      in this session: two real `tier2_execution` log lines, `previous_value: "false" ->
      confirmed_value: "true"` then `previous_value: "true" -> confirmed_value: "false"`,
      each carrying the real approver ARN through from the approval record.

**Cedar policy** (`Tier2ApprovalPolicy` in `cdk/lib/stack.ts`) is a single statement that
covers both required denial tests: it permits only when `context.input.parameter_name`
equals the one tagged parameter's exact name AND `context.input.approved == "true"` AND
`context.input.new_value` is `"true"` or `"false"` -- any other parameter name, or
`approved` anything other than `"true"`, falls through to AgentCore's own default-deny, the
same mechanism the `agentcore-acts-as-the-user` sibling repo's "any other repo has no
permit" case relies on.

**IAM, least privilege (checked against AWS's own devguide before writing, per the Tier 1
outbound-auth lesson, not retrofitted after a review flag):**
- `SetFeatureFlagFn`'s Lambda execution role: `ssm:GetParameter`/`ssm:PutParameter` scoped
  to the ONE tagged parameter's exact ARN only (`maintenanceFlagParam.grantRead/grantWrite`)
  -- never `ssm:*`/`parameter/*`, even though this is a throwaway demo value.
- `Tier2GatewayRole`'s Policy-in-AgentCore permissions: the exact three actions
  (`GetPolicyEngine`, `AuthorizeAction`, `PartiallyAuthorizeActions`) and the exact
  two-ARN-type shape from
  `docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html` (fetched
  and read live in this session, 2026-10-22), scoped to this stack's own
  `policy-engine/<id>` (exact, via `attrPolicyEngineId`) and, for the post-creation runtime
  checks, `gateway/<id>` (exact, via `attrGatewayIdentifier`) -- never `policy-engine/*` or
  `gateway/*`, which the devguide's own troubleshooting examples use but its "production"
  guidance says to replace.
- **One real wildcard, and why no exact form was available:** attaching a
  `policyEngineConfiguration` to a brand-new Gateway runs AWS's own
  "GenesisPolicyEngineCheck" DURING `CREATE`, which calls `AuthorizeAction` against the
  Gateway's own ARN before the Gateway exists -- caught live as a real `CREATE_FAILED`
  (`"...GenesisPolicyEngineCheck is not authorized to perform:
  bedrock-agentcore:AuthorizeAction on resource:
  gateway/tier2-remediation-gateway-yh8rlwzygf..."`). `AWS::BedrockAgentCore::Gateway`
  always appends a random suffix to the given `name` (confirmed from that exact deny
  message), so the Gateway's real ARN genuinely cannot be known before `CREATE` --
  the same class of bootstrapping gap Tier 1's Secrets Manager grant hit for the outbound-auth
  credential provider's secret name. The fix is a name-PREFIX wildcard
  (`gateway/tier2-remediation-gateway*`), scoped to THIS stack's own gateway name only,
  added to the pre-creation policy purely to get through Genesis; the exact-ARN grant
  (`Tier2GatewayAuthorizationPolicy`, added after the Gateway exists) still covers the real
  post-creation runtime-invocation checks.
- `Tier2GatewayRole` is also a SEPARATE `iam.Policy` construct (not merged into the role's
  auto-generated `DefaultPolicy`) for the post-creation gateway-ARN grant -- folding it into
  `DefaultPolicy` would make the role's policy depend on the Gateway (for the ARN) while the
  Gateway also depends on the role's policy (for `GetPolicyEngine` before `CREATE`), a real
  circular dependency CloudFormation's own template validation caught live at `cdk synth`
  (`"Tier2GatewayRoleDefaultPolicy -> Tier2Gateway -> Tier2GatewayRoleDefaultPolicy"`) before
  any deploy attempt, the same class of bug Tier 1's `MonitoringCrossInvokePolicy` exists to
  avoid for the Runtimes.

**Cost, measured not estimated for the actual Tier 2 test run:** zero Bedrock model
invocations (`propose_remediation_action()` only calls the local, non-LLM
`classify_finding()` -- confirmed by pulling `AWS/Bedrock` `InputTokenCount`/
`OutputTokenCount` CloudWatch metrics for the session window: both `0`). The only billed
calls are 4 AgentCore Gateway invocations (2 denied, 2 allowed) and 4 Policy authorisations,
at published list rates ($5/million invocations, $25/million authorisations,
aws.amazon.com/bedrock/agentcore/pricing/) -- a few hundredths of a cent, well under the
spec's "well under $1" estimate and the repo's $5 spend cap. SSM Parameter Store and the
two Lambda invocations are free-tier eligible.

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

## Tier 2 build session note (2026-10-22)
Built, deployed, and verified live in the same session as the approval. Read the sibling
`agentcore-acts-as-the-user` repo's `cdk/lib/stack.ts` and README first, per the task brief
-- its Gateway-role-ordering fix and Cedar `like`-match note didn't recur here directly, but
its overall shape (Gateway + PolicyEngine + Lambda target + Cedar policy, deployed via the
same account's admin role) carried over directly and saved real time.

- **One genuinely new sharp edge, not in the sibling repo:** `AWS::BedrockAgentCore::Gateway`
  runs a "GenesisPolicyEngineCheck" authorization call DURING its own `CREATE`, against
  BOTH the policy engine's ARN and the Gateway's own (not-yet-existent) ARN, in two separate
  live `CREATE_FAILED` deny messages, each naming a different resource -- the same "deny
  message only reports the first blocker" texture Tier 1's outbound-auth fix hit. The second
  deny is the interesting one: it proved AWS always appends a random suffix to the Gateway's
  given `name`, so the exact post-creation ARN cannot be computed or known ahead of `CREATE`
  -- a genuine, AWS-caused bootstrapping gap, not a scoping oversight. See the Tier 2
  acceptance criteria above for the exact wildcard used and why it's the narrowest workable
  form, checked against AWS's own devguide first (`policy-permissions.html`, fetched and
  read live in this session before writing any Tier 2 IAM, per the Tier 1 lesson applied
  proactively this time).
- **A circular dependency CloudFormation's own template validation caught before any deploy
  attempt**, at `cdk synth`: folding the Gateway-ARN-referencing authorization statement
  into the Gateway role's auto-generated `DefaultPolicy` (which the Gateway itself already
  depends on, for `GetPolicyEngine` before `CREATE`) created
  `Tier2GatewayRoleDefaultPolicy -> Tier2Gateway -> Tier2GatewayRoleDefaultPolicy`. Fixed the
  same way Tier 1's `MonitoringCrossInvokePolicy` fixes the equivalent bug for the
  Runtimes: a separate `iam.Policy` construct, not `role.addToPolicy()`.
- **A naming-pattern rejection, also caught at `cdk synth` before any deploy attempt:**
  `AWS::BedrockAgentCore::GatewayTarget`'s `Name` must match `^([0-9a-zA-Z][-]?){1,100}$` --
  no underscores. The target name `tier2_remediation` was renamed to `tier2-remediation`;
  the tool name (`set_feature_flag`, underscored) is a different, unconstrained field and
  was left as-is.
- **All four required acceptance criteria passed on the first full `make tier2-demo` run**
  after the IAM/naming fixes above -- no retries needed, no criterion weakened. Full
  transcript: `docs/tier2_transcript.txt`. Approval log: `docs/tier2_approval_log.jsonl`.
- **Chose the SSM feature-flag action over an alarm reset**, per the spec's "your call which
  is cleaner to build and demo": a dedicated, genuinely-tagged test parameter gives the
  Cedar policy one clean resource identity to scope to, and a second, genuinely-untagged
  parameter for the denial test, without touching the Tier 1 alarm Tier 1's own `make
  incident`/`make destroy` already own the lifecycle of.
- **Approval is real and logged, but this session's own approvals were supplied
  non-interactively** (`TIER2_APPROVAL=y`), honestly recorded as such in
  `docs/tier2_approval_log.jsonl`'s `source` field -- no human was physically at a live
  terminal prompt during this automated build/verification session. `record_approval()`'s
  `input()` path is real and will prompt a human who runs `make tier2-demo` from an actual
  terminal; Raj's own "go tier 2" is what authorised building and exercising this flow in
  this session, not a substitute for the per-action log itself.
