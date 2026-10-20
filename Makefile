# Region is pinned: the shell's AWS_REGION can silently override AWS_DEFAULT_REGION for CLI calls.
export AWS_REGION ?= us-east-1
export AWS_DEFAULT_REGION ?= us-east-1
export CDK_DEFAULT_ACCOUNT ?= $(shell aws sts get-caller-identity --query Account --output text)

install:
	cd cdk && npm install
	python3 -m pip install --quiet boto3

deploy: install
	cd cdk && npx cdk deploy --require-approval never --outputs-file cdk.outputs.json

# MONITORING_CLIENT_SECRET feeds demo.py's OAuth client-credentials call for pattern (c).
# Cognito never returns it via a CloudFormation output (avoids a secret in plaintext
# stack outputs); pull it live via DescribeUserPoolClient instead.
demo:
	@USER_POOL_ID=$$(python3 -c "import json;print(json.load(open('cdk/cdk.outputs.json'))[next(iter(json.load(open('cdk/cdk.outputs.json'))))]['UserPoolId'])"); \
	CLIENT_ID=$$(python3 -c "import json;print(json.load(open('cdk/cdk.outputs.json'))[next(iter(json.load(open('cdk/cdk.outputs.json'))))]['MonitoringClientId'])"); \
	SECRET=$$(aws cognito-idp describe-user-pool-client --user-pool-id $$USER_POOL_ID --client-id $$CLIENT_ID --query 'UserPoolClient.ClientSecret' --output text); \
	MONITORING_CLIENT_SECRET=$$SECRET python3 demo/demo.py | tee docs/transcript.txt

destroy:
	cd cdk && npx cdk destroy --force

.PHONY: install deploy demo destroy
