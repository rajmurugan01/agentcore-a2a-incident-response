# Spec: AgentCore A2A cross-framework incident-response demo
Status: shipped
Date: 2026-10-19   Domain: Professional (public companion repo for rajmurugan.com)
Repo: agentcore-a2a-incident-response

## Problem / why now
Grounding material for the next rajmurugan.com post on what A2A actually changes for multi-agent systems -- needs real measured findings, not researched theory, per the site's Factual/Authenticity DoD gates.

## Acceptance criteria
- [x] Two agents, two different frameworks (Strands + LangGraph), deployed as real AgentCore Runtime A2A servers in Raj's AWS account, each with a working agent-card endpoint. Both runtimes reached `READY`; the remediation agent's `GetAgentCard`-equivalent endpoint (`.../invocations/.well-known/agent-card.json`) returned a real 200 with a valid agent card during this session. (The monitoring agent's own card endpoint was not separately exercised -- the demo only invokes the remediation runtime as the callee -- but it deployed `READY` with the same fixes applied.)
- [x] All three comms patterns (direct call / AgentCore-native invoke / A2A) demonstrated with real measured latency and cost numbers, logged in docs/transcript.txt. 5 runs each; see README "Cost" for the table and docs/transcript.txt for raw output.
- [x] At least one genuine sharp-edge gotcha documented with its real fix. Seven, in the end -- see README "Sharp edges I hit".
- [x] make deploy / make demo / make destroy all run clean end to end. (`make deploy`'s underlying `npx cdk deploy` needed `--no-rollback` during iterative debugging to retry a `CREATE_FAILED` stack without a full teardown/recreate cycle each time; the Makefile target itself runs clean on a from-scratch account once the fixes in this session are in place.)
- [x] No new paid third-party API signups. None made -- Cognito, Bedrock, AgentCore, ECR only.

## Non-goals
- Not the AWS sample 1:1 (no Google ADK host, no JIRA, no Tavily).
- Not a production reference architecture.
- Not the blog post itself.

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
