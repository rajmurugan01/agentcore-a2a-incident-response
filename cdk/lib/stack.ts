import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as iam from 'aws-cdk-lib/aws-iam';
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
      ],
    });
    const monitoringScope = cognito.OAuthScope.resourceServer(resourceServer, { scopeName: 'monitoring.invoke', scopeDescription: 'Invoke the monitoring agent' });
    const remediationScope = cognito.OAuthScope.resourceServer(resourceServer, { scopeName: 'remediation.invoke', scopeDescription: 'Invoke the remediation agent' });

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
  }
}
