# Region is pinned: the shell's AWS_REGION can silently override AWS_DEFAULT_REGION for CLI calls.
export AWS_REGION ?= us-east-1
export AWS_DEFAULT_REGION ?= us-east-1
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

# MONITORING_CLIENT_SECRET feeds demo.py's OAuth client-credentials call for pattern (c).
# Cognito never returns it via a CloudFormation output (avoids a secret in plaintext
# stack outputs); pull it live via DescribeUserPoolClient instead.
demo:
	@USER_POOL_ID=$$(python3 -c "import json;print(json.load(open('cdk/cdk.outputs.json'))[next(iter(json.load(open('cdk/cdk.outputs.json'))))]['UserPoolId'])"); \
	CLIENT_ID=$$(python3 -c "import json;print(json.load(open('cdk/cdk.outputs.json'))[next(iter(json.load(open('cdk/cdk.outputs.json'))))]['MonitoringClientId'])"); \
	SECRET=$$(aws cognito-idp describe-user-pool-client --user-pool-id $$USER_POOL_ID --client-id $$CLIENT_ID --query 'UserPoolClient.ClientSecret' --output text); \
	MONITORING_CLIENT_SECRET=$$SECRET .venv/bin/python3 demo/demo.py | tee docs/transcript.txt

destroy:
	cd cdk && npx cdk destroy --force

.PHONY: install deploy demo destroy
