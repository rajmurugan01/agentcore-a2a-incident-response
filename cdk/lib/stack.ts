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
    const monitoringRuntime = new agentcore.CfnRuntime(this, 'MonitoringRuntime', {
      agentRuntimeName: 'monitoring_agent_a2a',
      description: 'Strands CloudWatch monitoring/incident-finding agent, A2A server',
      roleArn: monitoringRole.roleArn,
      protocolConfiguration: 'A2A',
      agentRuntimeArtifact: { containerConfiguration: { containerUri: monitoringImage.imageUri } },
      authorizerConfiguration: {
        customJwtAuthorizer: { discoveryUrl, allowedClients: [monitoringClient.userPoolClientId, remediationClient.userPoolClientId] },
      },
      // Required by the service even though the CFN type marks it optional -- the first real
      // deploy attempt failed with "NetworkConfiguration is required" until this was added.
      networkConfiguration: { networkMode: 'PUBLIC' },
      environmentVariables: { AWS_REGION: this.region },
    });
    if (monitoringDefaultPolicy) monitoringRuntime.node.addDependency(monitoringDefaultPolicy);

    const remediationRuntime = new agentcore.CfnRuntime(this, 'RemediationRuntime', {
      agentRuntimeName: 'remediation_agent_a2a',
      description: 'LangGraph runbook-lookup remediation agent, A2A server',
      roleArn: remediationRole.roleArn,
      protocolConfiguration: 'A2A',
      agentRuntimeArtifact: { containerConfiguration: { containerUri: remediationImage.imageUri } },
      authorizerConfiguration: {
        customJwtAuthorizer: { discoveryUrl, allowedClients: [monitoringClient.userPoolClientId, remediationClient.userPoolClientId] },
      },
      networkConfiguration: { networkMode: 'PUBLIC' },
      environmentVariables: { AWS_REGION: this.region },
    });
    if (remediationDefaultPolicy) remediationRuntime.node.addDependency(remediationDefaultPolicy);

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
  }
}
