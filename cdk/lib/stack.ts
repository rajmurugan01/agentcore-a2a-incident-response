import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';

const RESOURCE_SERVER_ID = 'incident-response';

export class A2AIncidentResponseStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── Identity: one Cognito user pool, one resource server, two M2M clients ──
    // (one per agent, mirroring the AWS sample's per-agent Cognito client pattern, not
    // the "acts as the user" human-identity pattern from the other reference repo).
    const pool = new cognito.UserPool(this, 'Pool', {
      selfSignUpEnabled: false,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const domain = pool.addDomain('Domain', {
      cognitoDomain: { domainPrefix: `a2a-incident-${this.account}` },
    });
    const resourceServer = pool.addResourceServer('ResourceServer', {
      identifier: RESOURCE_SERVER_ID,
      scopes: [
        { scopeName: 'monitoring.invoke', scopeDescription: 'Invoke the monitoring agent' },
        { scopeName: 'remediation.invoke', scopeDescription: 'Invoke the remediation agent' },
        // Tier 2 (2026-10-22): a THIRD scope, for the human-approved execution step only.
        // Deliberately not granted to either agent client below -- the remediation agent
        // PROPOSES an action (agents/remediation_agent/agent.py's
        // propose_remediation_action()) but has no credential able to execute one.
        { scopeName: 'tier2.execute', scopeDescription: 'Execute an approved Tier 2 remediation action' },
      ],
    });
    const monitoringScope = cognito.OAuthScope.resourceServer(resourceServer, { scopeName: 'monitoring.invoke', scopeDescription: 'Invoke the monitoring agent' });
    const remediationScope = cognito.OAuthScope.resourceServer(resourceServer, { scopeName: 'remediation.invoke', scopeDescription: 'Invoke the remediation agent' });
    const tier2ExecuteScope = cognito.OAuthScope.resourceServer(resourceServer, { scopeName: 'tier2.execute', scopeDescription: 'Execute an approved Tier 2 remediation action' });

    // Each agent gets its OWN client-credentials client, scoped only to invoke the
    // OTHER agent. A credential that could invoke itself is a code smell, not a feature.
    const monitoringClient = pool.addClient('MonitoringClient', {
      generateSecret: true,
      oAuth: { flows: { clientCredentials: true }, scopes: [remediationScope] },
    });
    const remediationClient = pool.addClient('RemediationClient', {
      generateSecret: true,
      oAuth: { flows: { clientCredentials: true }, scopes: [monitoringScope] },
    });
    // Tier 2's execution-step credential -- a THIRD, separate identity from both agents.
    // Neither agent client above is scoped to tier2.execute, so neither agent could call
    // the Tier 2 Gateway even if it tried; only whatever runs the human-approved
    // execution step (scripts/tier2_demo.py) ever holds this credential.
    const tier2ExecutorClient = pool.addClient('Tier2ExecutorClient', {
      generateSecret: true,
      oAuth: { flows: { clientCredentials: true }, scopes: [tier2ExecuteScope] },
    });
    const discoveryUrl = `https://cognito-idp.${this.region}.amazonaws.com/${pool.userPoolId}/.well-known/openid-configuration`;

    // ── Container images: CDK builds + pushes each agent's Dockerfile as an asset,
    // so `cdk deploy` alone is enough -- no separate manual `docker push` step. AgentCore
    // Runtime requires linux/arm64 images (Graviton-only execution environment).
    const monitoringImage = new ecrAssets.DockerImageAsset(this, 'MonitoringImage', {
      directory: path.join(__dirname, '../../agents/monitoring_agent'),
      platform: ecrAssets.Platform.LINUX_ARM64,
    });
    const remediationImage = new ecrAssets.DockerImageAsset(this, 'RemediationImage', {
      directory: path.join(__dirname, '../../agents/remediation_agent'),
      platform: ecrAssets.Platform.LINUX_ARM64,
    });

    // ── Execution roles ──────────────────────────────────────────────────────────
    const trustedByRuntime = new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
      conditions: {
        StringEquals: { 'aws:SourceAccount': this.account },
        ArnLike: { 'aws:SourceArn': `arn:aws:bedrock-agentcore:${this.region}:${this.account}:*` },
      },
    });

    const monitoringRole = new iam.Role(this, 'MonitoringRole', { assumedBy: trustedByRuntime });
    const remediationRole = new iam.Role(this, 'RemediationRole', { assumedBy: trustedByRuntime });

    for (const role of [monitoringRole, remediationRole]) {
      // Baseline every AgentCore Runtime execution role needs: its own logs/traces,
      // pulling its own image, and invoking the model it reasons with.
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams'],
        resources: ['*'],
      }));
      // Claude Haiku 4.5 (see agents/*/agent.py for why it replaced 3.5 Haiku, which hit
      // EOL) has no in-Region us-east-1 inference profile -- only the cross-Region "us."
      // geo profile, which is a DIFFERENT IAM resource type (inference-profile, not
      // foundation-model). Granting only foundation-model/* -- which is all the original
      // model needed -- gets AccessDenied on "arn:...:inference-profile/us.anthropic...".
      // Caught live: the remediation agent's own JSON-RPC response surfaced the
      // AccessDeniedException text verbatim. Both resource types are needed: the
      // inference-profile ARN you call, and the underlying foundation-model ARNs it
      // routes to.
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: ['arn:aws:bedrock:*::foundation-model/*', 'arn:aws:bedrock:*:*:inference-profile/*'],
      }));
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['bedrock-agentcore:GetWorkloadAccessToken', 'bedrock-agentcore:GetWorkloadAccessTokenForJWT'],
        resources: ['*'],
      }));
      // ecr:GetAuthorizationToken is an account-level action with no resource ARN of its own
      // (it's how the ECR "docker login" token is minted) -- repository.grantPull() only grants
      // the repo-scoped actions (BatchGetImage/GetDownloadUrlForLayer/BatchCheckLayerAvailability),
      // so without this separate statement AgentCore's own image pull gets AccessDenied even
      // though grantPull() looks sufficient. Hit this on the first real deploy attempt.
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['ecr:GetAuthorizationToken'],
        resources: ['*'],
      }));
    }
    monitoringImage.repository.grantPull(monitoringRole);
    remediationImage.repository.grantPull(remediationRole);

    // ── AgentCore Identity: outbound-auth credential provider (approval condition 2,
    // 2026-10-22) ──────────────────────────────────────────────────────────────────
    // The monitoring agent now makes the agent-to-agent hand-off itself (see
    // MonitoringCrossInvokePolicy below) and needs its OWN Cognito client-credentials
    // token to do it -- never a plaintext secret in its code, env, or container image.
    // Verified for real against this build's botocore (bedrock-agentcore-control
    // CreateOauth2CredentialProvider shape) and CDK (aws-bedrockagentcore L1) models
    // during this session, per docs/specs/agentcore-a2a-incident-response.md's condition
    // 2: a CfnOAuth2CredentialProvider with credentialProviderVendor "CognitoOauth2" (a
    // named enum value, confirmed in the live botocore model -- there is no separately
    // modelled CognitoOauth2ProviderConfigInput struct, so it pairs with the generic
    // includedOauth2ProviderConfig shape, which takes issuer/authorizationEndpoint/
    // tokenEndpoint explicitly) is the right fit. The client SECRET is supplied to this
    // ONE create call via UserPoolClient.userPoolClientSecret -- CDK's own
    // deploy-time-resolved SecretValue for the secret Cognito generates, never a literal
    // in this file -- and AgentCore Identity stores it in its own vault from that point
    // on; the running monitoring_agent container fetches a fresh access token at call
    // time via GetResourceOauth2Token (wrapped by the bedrock_agentcore SDK's
    // @requires_access_token(..., auth_flow="M2M") decorator in agent.py), and never
    // reads a secret from os.environ.
    //
    // Cognito's OAuth2 token endpoint lives on the hosted-UI domain, NOT the issuer URL
    // the discovery document advertises (the same gotcha CognitoDomainUrl below exists
    // to work around for demo.py) -- so issuer/authorizationEndpoint/tokenEndpoint are
    // given explicitly here instead of relying on discoveryUrl resolution.
    const cognitoIssuer = `https://cognito-idp.${this.region}.amazonaws.com/${pool.userPoolId}`;
    const monitoringToRemediationCredentialProvider = new agentcore.CfnOAuth2CredentialProvider(
      this, 'MonitoringToRemediationCredentialProvider', {
        name: 'monitoring-to-remediation-cognito',
        credentialProviderVendor: 'CognitoOauth2',
        oauth2ProviderConfigInput: {
          includedOauth2ProviderConfig: {
            clientId: monitoringClient.userPoolClientId,
            clientSecret: monitoringClient.userPoolClientSecret.unsafeUnwrap(),
            issuer: cognitoIssuer,
            authorizationEndpoint: `${domain.baseUrl()}/oauth2/authorize`,
            tokenEndpoint: `${domain.baseUrl()}/oauth2/token`,
          },
        },
      },
    );
    // The GetResourceOauth2Token IAM grant is added further down, AFTER monitoringRuntime
    // exists -- see the comment there for why the resource ARN it needs is NOT this
    // credential provider's own ARN (a real finding from this build, not an assumption).

    // Monitoring agent's actual job: read CloudWatch logs/metrics/alarms. Read-only --
    // it is a monitoring agent, not a remediation one.
    monitoringRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'cloudwatch:DescribeAlarms', 'cloudwatch:GetMetricData', 'cloudwatch:GetMetricStatistics',
        'cloudwatch:ListMetrics', 'logs:FilterLogEvents', 'logs:GetLogEvents', 'logs:DescribeLogGroups',
      ],
      resources: ['*'],
    }));

    // CDK batches every role.addToPolicy()/grantPull() call above into ONE auto-generated
    // "DefaultPolicy" IAM::Policy resource per role. CloudFormation has no implicit ordering
    // between that Policy resource and the CfnRuntime below -- both only depend on the Role
    // itself, not on each other -- so without an explicit dependency they get created in
    // parallel, and the AgentCore control plane's own ECR-access check (which runs synchronously
    // during CreateAgentRuntime) frequently loses that race: the Runtime create call reaches
    // AWS before the policy has attached, and fails with "Access denied ... ecr:GetAuthorizationToken"
    // even though the policy document is completely correct. Hit this for real -- three failed
    // deploy attempts with byte-identical IAM policies before tracing it to a race, not a
    // missing permission. Force the ordering explicitly.
    const monitoringDefaultPolicy = monitoringRole.node.findChild('DefaultPolicy');
    const remediationDefaultPolicy = remediationRole.node.findChild('DefaultPolicy');

    // ── AgentCore Runtimes, both in A2A-server protocol mode ────────────────────
    // Remediation created FIRST: the monitoring runtime's own environmentVariables now
    // need remediation's ARN (for the agent-to-agent hand-off tool in monitoring/agent.py),
    // so remediation's CfnRuntime construct has to exist before monitoring's prop object
    // is built, not just before monitoring's dependency/policy wiring below.
    const remediationRuntime = new agentcore.CfnRuntime(this, 'RemediationRuntime', {
      agentRuntimeName: 'remediation_agent_a2a',
      description: 'LangGraph runbook-lookup remediation agent, A2A server',
      roleArn: remediationRole.roleArn,
      protocolConfiguration: 'A2A',
      agentRuntimeArtifact: { containerConfiguration: { containerUri: remediationImage.imageUri } },
      authorizerConfiguration: {
        customJwtAuthorizer: { discoveryUrl, allowedClients: [monitoringClient.userPoolClientId, remediationClient.userPoolClientId] },
      },
      // Required by the service; the CFN/CDK type is non-optional too (checked directly
      // against aws-cdk-lib's generated types). First deploy attempt just missed it --
      // failed with "NetworkConfiguration is required" until this was added.
      networkConfiguration: { networkMode: 'PUBLIC' },
      environmentVariables: { AWS_REGION: this.region },
    });
    if (remediationDefaultPolicy) remediationRuntime.node.addDependency(remediationDefaultPolicy);

    const monitoringRuntime = new agentcore.CfnRuntime(this, 'MonitoringRuntime', {
      agentRuntimeName: 'monitoring_agent_a2a',
      description: 'Strands CloudWatch monitoring/incident-finding agent, A2A server',
      roleArn: monitoringRole.roleArn,
      protocolConfiguration: 'A2A',
      agentRuntimeArtifact: { containerConfiguration: { containerUri: monitoringImage.imageUri } },
      authorizerConfiguration: {
        customJwtAuthorizer: { discoveryUrl, allowedClients: [monitoringClient.userPoolClientId, remediationClient.userPoolClientId] },
      },
      networkConfiguration: { networkMode: 'PUBLIC' },
      environmentVariables: {
        AWS_REGION: this.region,
        // Agent-to-agent hand-off (Tier 1 criterion, 2026-10-22): the monitoring agent
        // fetches remediation's card and sends message/send itself. None of these three
        // values are secrets -- the credential provider NAME is just an identifier the
        // agent passes to AgentCore Identity's GetResourceOauth2Token at call time; the
        // actual Cognito client secret lives only in the credential provider's own vault
        // (see MonitoringToRemediationCredentialProvider above), never here.
        REMEDIATION_RUNTIME_ARN: remediationRuntime.attrAgentRuntimeArn,
        OAUTH2_CREDENTIAL_PROVIDER_NAME: monitoringToRemediationCredentialProvider.name,
        REMEDIATION_SCOPE: `${RESOURCE_SERVER_ID}/remediation.invoke`,
      },
    });
    if (monitoringDefaultPolicy) monitoringRuntime.node.addDependency(monitoringDefaultPolicy);

    // Real finding from this build, confirmed against FOUR successive live
    // AccessDeniedException messages (2026-10-22), each naming a DIFFERENT exact resource
    // ARN as the one denied (IAM's deny message only ever reports the first unauthorized
    // resource it hits in a multi-resource action, not every one the action touches):
    // the credential provider ARN, the workload identity ARN, the workload identity
    // DIRECTORY ARN, and finally the TOKEN VAULT ARN one level up from the credential
    // provider. A first pass closed this with two namespace wildcards
    // (token-vault/*, workload-identity-directory/*), which worked but is an
    // account-wide-within-service anti-pattern this repo's own point is to avoid --
    // flagged in review (2026-10-22) against AWS's own least-privilege guidance
    // (devguide "Scope down access to credential providers by workload identity"),
    // which gives the exact four-ARN shape used below. Scoped to this stack's own
    // resources only, not reconstructed by guessing the segment names: the workload
    // identity's exact name (AgentCore's auto-created default identity, confirmed
    // against the live AccessDeniedException text, not assumed from the devguide's
    // generic example) and the credential provider's exact ARN both come from this
    // stack's own resource attributes, not a hand-typed string -- the devguide's own
    // example literal ("oauth2-credential-provider", hyphenated) does NOT match this
    // resource's actual deployed `CredentialProviderArn` attribute ("oauth2credential
    // provider", no hyphen, confirmed from this session's real CfnOutput every time it
    // was printed) -- using the real attribute sidesteps that mismatch entirely rather
    // than risk a silently-wrong Resource that happens to still "match" because an
    // earlier wildcard was masking it.
    const monitoringWorkloadIdentityDirectoryArn = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`;
    const monitoringWorkloadIdentityArn = `${monitoringWorkloadIdentityDirectoryArn}/workload-identity/${cdk.Fn.select(1, cdk.Fn.split('runtime/', monitoringRuntime.attrAgentRuntimeArn))}`;
    const tokenVaultArn = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default`;
    new iam.Policy(this, 'MonitoringOutboundAuthPolicy', {
      roles: [monitoringRole],
      statements: [
        new iam.PolicyStatement({
          actions: ['bedrock-agentcore:GetResourceOauth2Token'],
          resources: [
            monitoringWorkloadIdentityDirectoryArn,
            monitoringWorkloadIdentityArn,
            tokenVaultArn,
            monitoringToRemediationCredentialProvider.attrCredentialProviderArn,
          ],
        }),
        // A FIFTH deny, past all four bedrock-agentcore ones above: AgentCore Identity
        // stores the credential provider's client secret as an actual AWS Secrets
        // Manager secret, and GetResourceOauth2Token reads it using the CALLING ROLE's
        // own IAM identity, not an AgentCore-internal service role -- so the monitoring
        // role ALSO needs secretsmanager:GetSecretValue on that secret directly.
        // Tried the credential provider's own attrClientSecretArn attribute first (it
        // exists on CfnOAuth2CredentialProvider and looked like the exact-ARN answer),
        // but it resolves to a structured value, not a bare ARN string -- passing it
        // into Resource produced "Syntax errors in policy" at deploy time (caught live,
        // not guessed). There is no AWS-documented exact-ARN form for this specific
        // secret (unlike the four bedrock-agentcore ARNs above, which the devguide
        // documents explicitly) -- a name-PREFIX wildcard, scoped to this credential
        // provider's own name only (never a service- or account-wide `*`), is the
        // narrowest thing that actually deploys. The real secret name was confirmed
        // live via the AccessDeniedException text:
        // "bedrock-agentcore-identity!default/oauth2/<provider-name>-<random-suffix>"
        // -- the random suffix is only assigned at creation time, which is why the
        // wildcard is needed at all here, not from not having looked.
        new iam.PolicyStatement({
          actions: ['secretsmanager:GetSecretValue'],
          resources: [
            `arn:aws:secretsmanager:${this.region}:${this.account}:secret:bedrock-agentcore-identity!default/oauth2/${monitoringToRemediationCredentialProvider.name}*`,
          ],
        }),
      ],
    });

    // ── Cross-runtime invoke, pattern (b): the InvokeAgentRuntime data-plane call is a
    // DISTINCT primitive from the A2A JSON-RPC envelope -- it exists regardless of
    // ProtocolConfiguration. Grant each role permission to invoke the OTHER runtime
    // directly, bypassing agent-card discovery and the A2A message/send wrapper.
    //
    // DELIBERATELY separate iam.Policy constructs, not role.addToPolicy(): that call merges
    // into the role's single auto-generated DefaultPolicy resource, which already carries the
    // baseline ecr:GetAuthorizationToken/logs/bedrock statements the runtime needs just to be
    // CREATED. Because these statements reference the OTHER runtime's attrAgentRuntimeArn,
    // merging them in makes the whole DefaultPolicy depend on both CfnRuntimes existing first --
    // a circular wait (runtime needs the policy's baseline perms to be created; policy can't be
    // created until the runtime exists) that CloudFormation resolves by creating the runtime
    // BEFORE the policy, so the runtime creation call always fails with ECR AccessDenied.
    // Hit this for real on this deploy (three failed attempts, same error, before tracing it here).
    new iam.Policy(this, 'MonitoringCrossInvokePolicy', {
      roles: [monitoringRole],
      statements: [new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeAgentRuntime'],
        resources: [remediationRuntime.attrAgentRuntimeArn, `${remediationRuntime.attrAgentRuntimeArn}/*`],
      })],
    });
    new iam.Policy(this, 'RemediationCrossInvokePolicy', {
      roles: [remediationRole],
      statements: [new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeAgentRuntime'],
        resources: [monitoringRuntime.attrAgentRuntimeArn, `${monitoringRuntime.attrAgentRuntimeArn}/*`],
      })],
    });

    // ── Tier 2 (approved by Raj 2026-10-22, "go tier 2"): one safe, reversible action
    // behind human approval -- an AgentCore Gateway with a Cedar policy (ENFORCE mode)
    // scoped to exactly one tagged test SSM parameter. "Two frameworks detect and
    // propose; a human approves; policy enforces" -- the remediation agent (see
    // propose_remediation_action() in agents/remediation_agent/agent.py) is never
    // granted any credential able to reach this Gateway at all; only
    // scripts/tier2_demo.py's separate Tier2ExecutorClient token can. See
    // docs/specs/agentcore-a2a-incident-response.md's Tier 2 acceptance criteria for
    // the four things this section exists to make true: a logged approval, a denied
    // no-approval attempt, a denied untagged-resource attempt, and a verified rollback.

    // The ONE resource Tier 2 is allowed to touch: a real, tagged test feature flag.
    const maintenanceFlagParam = new ssm.StringParameter(this, 'MaintenanceFlagParam', {
      parameterName: '/a2a-incident-demo/feature-flags/checkout-api-test-maintenance-mode',
      stringValue: 'false',
      description: 'TEST feature flag for agentcore-a2a-incident-response Tier 2 -- reversible, '
        + 'human-approved, Cedar-policy-enforced. Not a real production flag.',
    });
    maintenanceFlagParam.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    cdk.Tags.of(maintenanceFlagParam).add('Project', 'agentcore-a2a-incident-response');
    cdk.Tags.of(maintenanceFlagParam).add('Purpose', 'tier2-remediation-test');
    cdk.Tags.of(maintenanceFlagParam).add('CreatedBy', 'cdk/lib/stack.ts');

    // A second, deliberately UNTAGGED parameter -- a real SSM parameter outside Tier 2's
    // approved scope, used ONLY to prove the denied-untagged-resource acceptance
    // criterion. Cedar never queries live AWS resource tags (same limitation the
    // agentcore-acts-as-the-user sibling repo's README documents for its own
    // group-claim check: "Cedar only knows what the token says"/what's in context) --
    // the Tier2ApprovalPolicy statement below permits exactly ONE parameter_name
    // literal, and this parameter is deliberately not that one, so a call naming it is
    // denied by Cedar's own default-deny, the same mechanism the sibling repo's
    // "any other repo has no permit" case relies on.
    const untaggedFlagParam = new ssm.StringParameter(this, 'UntaggedFlagParam', {
      parameterName: '/a2a-incident-demo/feature-flags/untagged-example',
      stringValue: 'false',
      description: 'Deliberately UNTAGGED and outside Tier 2 approved scope -- exists only to '
        + 'prove the Cedar policy denies a call against it.',
    });
    untaggedFlagParam.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    // Gateway target: a Lambda that does the actual ssm:GetParameter/PutParameter.
    const setFeatureFlagFn = new lambda.Function(this, 'SetFeatureFlagFn', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'set_feature_flag.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambda')),
      timeout: cdk.Duration.seconds(10),
    });
    // Least privilege: the Lambda may touch ONLY the one tagged test parameter -- never
    // ssm:*/parameter/* -- even though this is a throwaway demo value. The whole point
    // of this repo is the narrow form, not the wildcard that happens to also work.
    maintenanceFlagParam.grantRead(setFeatureFlagFn);
    maintenanceFlagParam.grantWrite(setFeatureFlagFn);

    // Gateway execution role: same bedrock-agentcore service-principal trust condition
    // Tier 1's runtime roles use (trustedByRuntime, defined above).
    const tier2GatewayRole = new iam.Role(this, 'Tier2GatewayRole', { assumedBy: trustedByRuntime });
    setFeatureFlagFn.grantInvoke(tier2GatewayRole);

    const tier2Engine = new agentcore.CfnPolicyEngine(this, 'Tier2Engine', { name: 'tier2_remediation_engine' });

    // Policy-in-AgentCore permissions on the Gateway execution role -- the three actions
    // from AWS's own devguide
    // (docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html,
    // fetched and read in this session, 2026-10-22), applying the Tier 1 outbound-auth
    // lesson PROACTIVELY this time: scoped to THIS stack's own policy-engine/gateway IDs
    // from the start, never policy-engine/* or gateway/* -- the devguide's own
    // troubleshooting examples use those wildcards, but its explicit "production"
    // guidance is to replace them with the specific resource IDs, which is what the
    // ARNs below do (attrPolicyEngineId / attrGatewayIdentifier are this construct's own
    // live CFN attributes, not hand-typed strings). No GetWorkloadAccessToken statement
    // is added: that's only required for TEMPORAL policies (a policy session ID
    // supplied per-call), which this Gateway doesn't use.
    //
    // The devguide's own two-statement split puts AuthorizeAction/PartiallyAuthorizeActions
    // only on [policy-engine, gateway] ARNs, both assumed to already exist. TWO real,
    // live CREATE_FAILED errors caught this doesn't hold at CREATE time, each naming a
    // DIFFERENT resource as the one denied (the same "deny message only ever reports
    // the first unauthorized resource" texture Tier 1's outbound-auth fix hit):
    // attaching a PolicyEngineConfiguration to a brand-new Gateway runs a
    // "GenesisPolicyEngineCheck" that calls AuthorizeAction FIRST against the POLICY
    // ENGINE alone (deny #1: "...GenesisPolicyEngineCheck is not authorized to perform:
    // bedrock-agentcore:AuthorizeAction on resource: policy-engine/..."), then AGAIN
    // against the GATEWAY'S OWN ARN (deny #2, after granting the first: "...resource:
    // gateway/tier2-remediation-gateway-yh8rlwzygf...") -- even though the Gateway does
    // not exist yet at the moment this check runs. AWS::BedrockAgentCore::Gateway always
    // appends a random suffix to the given `name` (confirmed live from deny #2's exact
    // resource string: name "tier2-remediation-gateway" -> ID
    // "tier2-remediation-gateway-yh8rlwzygf"), so the exact Gateway ARN genuinely cannot
    // be known before CREATE -- there is no exact-ARN form available here, the same
    // class of bootstrapping gap Tier 1's Secrets Manager grant hit for the outbound-auth
    // credential provider's secret name. A name-PREFIX wildcard, scoped to THIS stack's
    // own gateway name only (never service- or account-wide), is the narrowest thing
    // that actually gets through Genesis; the exact-ARN grant on
    // Tier2GatewayAuthorizationPolicy below still exists for the real post-creation
    // runtime-invocation checks, so the wildcard's only job is bootstrapping CREATE.
    tier2GatewayRole.addToPolicy(new iam.PolicyStatement({
      sid: 'PolicyEngineConfiguration',
      actions: [
        'bedrock-agentcore:GetPolicyEngine',
        'bedrock-agentcore:AuthorizeAction',
        'bedrock-agentcore:PartiallyAuthorizeActions',
      ],
      resources: [
        `arn:aws:bedrock-agentcore:${this.region}:${this.account}:policy-engine/${tier2Engine.attrPolicyEngineId}`,
        `arn:aws:bedrock-agentcore:${this.region}:${this.account}:gateway/tier2-remediation-gateway*`,
      ],
    }));

    // Hyphens only, no underscores -- AWS::BedrockAgentCore::GatewayTarget's Name must
    // match ^([0-9a-zA-Z][-]?){1,100}$ (caught live by `cdk synth`'s own template
    // validation, 2026-10-22, not assumed).
    const TIER2_TARGET = 'tier2-remediation';
    const TIER2_TOOL = 'set_feature_flag';

    const tier2Gateway = new agentcore.CfnGateway(this, 'Tier2Gateway', {
      name: 'tier2-remediation-gateway',
      protocolType: 'MCP',
      roleArn: tier2GatewayRole.roleArn,
      authorizerType: 'CUSTOM_JWT',
      authorizerConfiguration: {
        // Only the Tier2ExecutorClient's tokens are even ACCEPTED at the Gateway's JWT
        // authorizer -- neither agent client can present a valid token here at all,
        // before Cedar is ever consulted. Defence in depth, not the only gate.
        customJwtAuthorizer: { discoveryUrl, allowedClients: [tier2ExecutorClient.userPoolClientId] },
      },
      policyEngineConfiguration: { arn: tier2Engine.attrPolicyEngineArn, mode: 'ENFORCE' },
    });

    // DELIBERATELY a separate iam.Policy, not tier2GatewayRole.addToPolicy(): that call
    // merges into the role's single auto-generated DefaultPolicy, which the Gateway
    // itself already depends on (via the explicit addDependency below, needed so
    // GetPolicyEngine permission exists before Gateway creation calls it). This
    // statement references the Gateway's OWN attrGatewayIdentifier, so folding it into
    // DefaultPolicy would make DefaultPolicy depend on the Gateway too -- a real
    // circular dependency CloudFormation validation caught live at `cdk synth`
    // ("Tier2GatewayRoleDefaultPolicy -> Tier2Gateway -> Tier2GatewayRoleDefaultPolicy"),
    // the same class of bug Tier 1's MonitoringCrossInvokePolicy comment documents for
    // the runtimes. A standalone Policy construct naturally depends on the Gateway
    // (it needs the Gateway's ARN to exist) without the Gateway depending back on it.
    new iam.Policy(this, 'Tier2GatewayAuthorizationPolicy', {
      roles: [tier2GatewayRole],
      statements: [new iam.PolicyStatement({
        sid: 'PolicyEngineAuthorization',
        actions: ['bedrock-agentcore:AuthorizeAction', 'bedrock-agentcore:PartiallyAuthorizeActions'],
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:policy-engine/${tier2Engine.attrPolicyEngineId}`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:gateway/${tier2Gateway.attrGatewayIdentifier}`,
        ],
      })],
    });

    const tier2Target = new agentcore.CfnGatewayTarget(this, 'Tier2Target', {
      name: TIER2_TARGET,
      gatewayIdentifier: tier2Gateway.attrGatewayIdentifier,
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: setFeatureFlagFn.functionArn,
            toolSchema: {
              inlinePayload: [{
                name: TIER2_TOOL,
                description: 'Set a feature-flag SSM parameter to a new value. Only ever reaches '
                  + 'this Lambda if the Cedar policy already authorised the call -- the policy, '
                  + 'not this description, is the enforcement boundary.',
                inputSchema: {
                  type: 'object',
                  properties: {
                    parameter_name: { type: 'string', description: 'Full SSM parameter name to set' },
                    new_value: { type: 'string', description: '"true" or "false"' },
                    approved: { type: 'string', description: 'Must be the literal string "true" -- checked by the Cedar policy before this tool is ever invoked' },
                    approver: { type: 'string', description: 'Informational only, never trusted for authorization' },
                    approved_at: { type: 'string', description: 'Informational only, never trusted for authorization' },
                  },
                  required: ['parameter_name', 'new_value', 'approved'],
                },
              }],
            },
          },
        },
      },
      credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
    });

    // Same CloudFormation-ordering fix Tier 1 needed for its Runtimes (see
    // MonitoringCrossInvokePolicy's comment above) and the agentcore-acts-as-the-user
    // sibling repo needed for its own Gateways: the role's DefaultPolicy is a SEPARATE
    // CloudFormation resource with no implicit ordering against the Gateway, so without
    // an explicit dependency CloudFormation can create the Gateway before the role's
    // permissions are attached, and Policy Engine attachment fails with
    // "Access denied ... GetPolicyEngine" even though the policy document is correct.
    const tier2GatewayDefaultPolicy = tier2GatewayRole.node.findChild('DefaultPolicy');
    if (tier2GatewayDefaultPolicy) tier2Gateway.node.addDependency(tier2GatewayDefaultPolicy);

    // The Cedar policy itself. A single statement covers BOTH required denial tests:
    // approved != "true" denies regardless of resource (the no-approval test), and any
    // parameter_name other than the one tagged resource denies regardless of approval
    // (the untagged-resource test) -- there is no permit for either case, so AgentCore's
    // default-deny applies, exactly like the sibling repo's "any other repo has no
    // permit" case.
    const tier2Policy = new agentcore.CfnPolicy(this, 'Tier2ApprovalPolicy', {
      name: 'tier2_feature_flag_requires_approval',
      policyEngineId: tier2Engine.attrPolicyEngineId,
      definition: {
        cedar: {
          statement: `permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"${TIER2_TARGET}___${TIER2_TOOL}",
  resource == AgentCore::Gateway::"${tier2Gateway.attrGatewayArn}"
) when {
  context.input.parameter_name == "${maintenanceFlagParam.parameterName}" &&
  context.input.approved == "true" &&
  (context.input.new_value == "true" || context.input.new_value == "false")
};`,
        },
      },
    });
    // Policy validation calls the Gateway (authorized as InvokeGateway on the deploying
    // principal, which already holds AdministratorAccess in this account) -- same
    // dependency the sibling repo's policies need on their guarded gateway/target.
    tier2Policy.addResourceDependency(tier2Gateway);
    tier2Policy.addResourceDependency(tier2Target);

    new cdk.CfnOutput(this, 'Region', { value: this.region });
    new cdk.CfnOutput(this, 'Account', { value: this.account });
    new cdk.CfnOutput(this, 'UserPoolId', { value: pool.userPoolId });
    new cdk.CfnOutput(this, 'DiscoveryUrl', { value: discoveryUrl });
    // The OAuth2 client-credentials token endpoint lives on Cognito's HOSTED-UI DOMAIN
    // (a2a-incident-<account>.auth.<region>.amazoncognito.com), NOT the issuer URL the
    // discovery document uses -- deriving it by string-stripping DiscoveryUrl (as an earlier
    // version of demo.py did) silently points /oauth2/token at the wrong host. Output it
    // explicitly instead of making demo.py reconstruct it.
    new cdk.CfnOutput(this, 'CognitoDomainUrl', { value: domain.baseUrl() });
    new cdk.CfnOutput(this, 'MonitoringClientId', { value: monitoringClient.userPoolClientId });
    new cdk.CfnOutput(this, 'RemediationClientId', { value: remediationClient.userPoolClientId });
    new cdk.CfnOutput(this, 'MonitoringRuntimeArn', { value: monitoringRuntime.attrAgentRuntimeArn });
    new cdk.CfnOutput(this, 'RemediationRuntimeArn', { value: remediationRuntime.attrAgentRuntimeArn });
    new cdk.CfnOutput(this, 'OAuth2CredentialProviderArn', { value: monitoringToRemediationCredentialProvider.attrCredentialProviderArn });

    // Tier 2 outputs.
    new cdk.CfnOutput(this, 'Tier2ExecutorClientId', { value: tier2ExecutorClient.userPoolClientId });
    new cdk.CfnOutput(this, 'Tier2GatewayUrl', { value: tier2Gateway.attrGatewayUrl });
    new cdk.CfnOutput(this, 'Tier2GatewayArn', { value: tier2Gateway.attrGatewayArn });
    new cdk.CfnOutput(this, 'Tier2PolicyEngineArn', { value: tier2Engine.attrPolicyEngineArn });
    new cdk.CfnOutput(this, 'MaintenanceFlagParameterName', { value: maintenanceFlagParam.parameterName });
    new cdk.CfnOutput(this, 'UntaggedFlagParameterName', { value: untaggedFlagParam.parameterName });
  }
}
