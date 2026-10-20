# Spec: AgentCore A2A cross-framework incident-response demo
Status: building
Date: 2026-10-19   Domain: Professional (public companion repo for rajmurugan.com)
Repo: agentcore-a2a-incident-response

## Problem / why now
Grounding material for the next rajmurugan.com post on what A2A actually changes for multi-agent systems -- needs real measured findings, not researched theory, per the site's Factual/Authenticity DoD gates.

## Acceptance criteria
- [ ] Two agents, two different frameworks (Strands + LangGraph), deployed as real AgentCore Runtime A2A servers in Raj's AWS account, each with a working agent-card endpoint.
- [ ] All three comms patterns (direct call / AgentCore-native invoke / A2A) demonstrated with real measured latency and cost numbers, logged in docs/transcript.txt.
- [ ] At least one genuine sharp-edge gotcha documented with its real fix.
- [ ] make deploy / make demo / make destroy all run clean end to end.
- [ ] No new paid third-party API signups.

## Non-goals
- Not the AWS sample 1:1 (no Google ADK host, no JIRA, no Tavily).
- Not a production reference architecture.
- Not the blog post itself.

## Build-session note (2026-10-20)
Code (CDK stack, both agents, demo harness, Makefile, README) was written and committed
in this session. Deploy/demo/destroy were NOT run: the `personal` AWS SSO profile's
session token had expired and refreshing it requires an interactive browser login that
could not be completed headlessly. See README.md "Status" for exact resume steps. No
acceptance criterion above is checked off yet -- none of them can honestly be, since
nothing has been deployed or measured. This is a blocked status, not a shipped one.
