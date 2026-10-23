# Region is pinned -- HARD, not with ?=. Make's `?=` only sets a var when it is NOT
# already present in the environment, but an operator's shell commonly already exports
# AWS_REGION (e.g. to their usual home region) -- in that case `?=` is a silent no-op and
# every AWS call in this Makefile and in scripts/incident.py/demo.py quietly runs against
# the WRONG region. Hit this for real in the 2026-10-22 session: `make incident` created
# the test alarm in ap-southeast-2 because the calling shell had AWS_REGION=ap-southeast-2
# set, while the stack itself (and every runtime's own AWS_REGION env var) is us-east-1 --
# the alarm was created in a region the monitoring agent's CloudWatch tools never look at.
# Caught immediately only because the script printed the region it used; nothing else
# would have caught it. Unconditional assignment closes this for good.
export AWS_REGION := us-east-1
export AWS_DEFAULT_REGION := us-east-1
export CDK_DEFAULT_ACCOUNT ?= $(shell aws sts get-caller-identity --query Account --output text)

# demo/demo.py's pattern (a) -- the direct-call baseline -- imports both agents' agent.py
# modules IN-PROCESS on the host running demo.py, not inside their containers. That needs
# the full strands/langgraph/a2a-sdk/bedrock-agentcore dependency set available to the host
# python, which plain `pip install boto3` never provided -- caught when the first `make demo`
# run threw ModuleNotFoundError on `import strands`. A venv keeps that off the system python
# (PEP 668 "externally-managed-environment" blocks a bare `pip install` on recent macOS
# Homebrew python anyway) and off python3.14 (the Homebrew default), which several of these
# packages don't yet support -- python3.12 (matching the Dockerfiles' base image) is required.
install:
	cd cdk && npm install
	python3.12 -m venv .venv
	.venv/bin/pip install --quiet --upgrade pip
	.venv/bin/pip install --quiet -r agents/monitoring_agent/requirements.txt -r agents/remediation_agent/requirements.txt

deploy: install
	cd cdk && npx cdk deploy --require-approval never --outputs-file cdk.outputs.json

# Tier 1 criterion 1: a REAL tagged test CloudWatch metric + alarm, driven into ALARM for
# real -- see scripts/incident.py for why TreatMissingData=ignore and a 10s high-res
# period were chosen. Writes docs/incident.json (ground truth for demo.py and the report).
incident:
	.venv/bin/python3 scripts/incident.py

# MONITORING_CLIENT_SECRET/REMEDIATION_CLIENT_SECRET feed demo.py's OAuth
# client-credentials calls. Cognito never returns either via a CloudFormation output
# (avoids a secret in plaintext stack outputs); pull both live via DescribeUserPoolClient
# instead. demo.py needs BOTH now: the monitoring client's secret for patterns (b) and the
# pattern-(c) control (demo.py calling the REMEDIATION runtime directly, as before), and
# the remediation client's secret for the real pattern (c) (demo.py now calls the
# MONITORING runtime instead, which only the remediation client is scoped to invoke --
# see cdk/lib/stack.ts's per-agent scoping).
demo:
	@USER_POOL_ID=$$(python3 -c "import json;print(json.load(open('cdk/cdk.outputs.json'))[next(iter(json.load(open('cdk/cdk.outputs.json'))))]['UserPoolId'])"); \
	MON_CLIENT_ID=$$(python3 -c "import json;print(json.load(open('cdk/cdk.outputs.json'))[next(iter(json.load(open('cdk/cdk.outputs.json'))))]['MonitoringClientId'])"); \
	REM_CLIENT_ID=$$(python3 -c "import json;print(json.load(open('cdk/cdk.outputs.json'))[next(iter(json.load(open('cdk/cdk.outputs.json'))))]['RemediationClientId'])"); \
	MON_SECRET=$$(aws cognito-idp describe-user-pool-client --user-pool-id $$USER_POOL_ID --client-id $$MON_CLIENT_ID --query 'UserPoolClient.ClientSecret' --output text); \
	REM_SECRET=$$(aws cognito-idp describe-user-pool-client --user-pool-id $$USER_POOL_ID --client-id $$REM_CLIENT_ID --query 'UserPoolClient.ClientSecret' --output text); \
	MONITORING_CLIENT_SECRET=$$MON_SECRET REMEDIATION_CLIENT_SECRET=$$REM_SECRET .venv/bin/python3 demo/demo.py | tee docs/transcript.txt

# Tier 2 (approved 2026-10-22): human-approved, Cedar-policy-enforced reversible
# remediation. Reads cdk/cdk.outputs.json directly (region/gateway URL/client IDs);
# fetches the Tier2ExecutorClient secret live via DescribeUserPoolClient, same
# never-in-a-plaintext-output discipline as `demo`. Run `make tier2-demo` with a real
# terminal attached to approve interactively; piped/non-interactive runs fall back to
# the TIER2_APPROVAL=y/n env var and log that fact (see scripts/tier2_demo.py).
tier2-demo:
	.venv/bin/python3 scripts/tier2_demo.py | tee docs/tier2_transcript.txt

# One-off (2026-10-23): isolates AgentCore Runtime session cold-start from A2A wire-
# protocol cost, in response to a technical reviewer's pushback on the blog post's "~15s
# of A2A protocol mechanics" claim. Only needs the MONITORING client's secret (same scope
# as demo.py's patterns (b)/(c-control): invoking the remediation runtime directly). See
# scripts/warm_session_experiment.py and docs/specs/agentcore-a2a-incident-response.md's
# "Warm-vs-cold session investigation" note for the full result.
warm-session-experiment:
	@USER_POOL_ID=$$(python3 -c "import json;print(json.load(open('cdk/cdk.outputs.json'))[next(iter(json.load(open('cdk/cdk.outputs.json'))))]['UserPoolId'])"); \
	MON_CLIENT_ID=$$(python3 -c "import json;print(json.load(open('cdk/cdk.outputs.json'))[next(iter(json.load(open('cdk/cdk.outputs.json'))))]['MonitoringClientId'])"); \
	MON_SECRET=$$(aws cognito-idp describe-user-pool-client --user-pool-id $$USER_POOL_ID --client-id $$MON_CLIENT_ID --query 'UserPoolClient.ClientSecret' --output text); \
	MONITORING_CLIENT_SECRET=$$MON_SECRET .venv/bin/python3 scripts/warm_session_experiment.py | tee docs/warm_session_transcript.txt

# Tier 1 criterion 9: also removes the test metric/alarm, not just the AgentCore/Cognito
# stack -- independently verified (describe-alarms / list-metrics), not just exit codes.
# Runs BEFORE cdk destroy: the incident alarm/metric are in this account regardless of
# whether the stack still exists, and describe-alarms works either way. Tier 2's own
# resources (Gateway, Policy Engine, Policy, both SSM parameters) are part of the same
# CDK stack, so `cdk destroy` below removes them too -- no separate teardown script.
destroy:
	.venv/bin/python3 scripts/teardown_incident.py
	cd cdk && npx cdk destroy --force

.PHONY: install deploy incident demo tier2-demo warm-session-experiment destroy
