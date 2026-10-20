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
    pool.addDomain('Domain', {
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
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: ['arn:aws:bedrock:*::foundation-model/*'],
      }));
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['bedrock-agentcore:GetWorkloadAccessToken', 'bedrock-agentcore:GetWorkloadAccessTokenForJWT'],
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
      environmentVariables: { AWS_REGION: this.region },
    });

    const remediationRuntime = new agentcore.CfnRuntime(this, 'RemediationRuntime', {
      agentRuntimeName: 'remediation_agent_a2a',
      description: 'LangGraph runbook-lookup remediation agent, A2A server',
      roleArn: remediationRole.roleArn,
      protocolConfiguration: 'A2A',
      agentRuntimeArtifact: { containerConfiguration: { containerUri: remediationImage.imageUri } },
      authorizerConfiguration: {
        customJwtAuthorizer: { discoveryUrl, allowedClients: [monitoringClient.userPoolClientId, remediationClient.userPoolClientId] },
      },
      environmentVariables: { AWS_REGION: this.region },
    });

    // ── Cross-runtime invoke, pattern (b): the InvokeAgentRuntime data-plane call is a
    // DISTINCT primitive from the A2A JSON-RPC envelope -- it exists regardless of
    // ProtocolConfiguration. Grant each role permission to invoke the OTHER runtime
    // directly, bypassing agent-card discovery and the A2A message/send wrapper.
    monitoringRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: [remediationRuntime.attrAgentRuntimeArn, `${remediationRuntime.attrAgentRuntimeArn}/*`],
    }));
    remediationRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: [monitoringRuntime.attrAgentRuntimeArn, `${monitoringRuntime.attrAgentRuntimeArn}/*`],
    }));

    new cdk.CfnOutput(this, 'Region', { value: this.region });
    new cdk.CfnOutput(this, 'Account', { value: this.account });
    new cdk.CfnOutput(this, 'UserPoolId', { value: pool.userPoolId });
    new cdk.CfnOutput(this, 'DiscoveryUrl', { value: discoveryUrl });
    new cdk.CfnOutput(this, 'MonitoringClientId', { value: monitoringClient.userPoolClientId });
    new cdk.CfnOutput(this, 'RemediationClientId', { value: remediationClient.userPoolClientId });
    new cdk.CfnOutput(this, 'MonitoringRuntimeArn', { value: monitoringRuntime.attrAgentRuntimeArn });
    new cdk.CfnOutput(this, 'RemediationRuntimeArn', { value: remediationRuntime.attrAgentRuntimeArn });
  }
}
