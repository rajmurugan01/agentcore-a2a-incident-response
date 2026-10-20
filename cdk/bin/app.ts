import * as cdk from 'aws-cdk-lib';
import { A2AIncidentResponseStack } from '../lib/stack';

const app = new cdk.App();
new A2AIncidentResponseStack(app, 'AgentcoreA2AIncidentResponse', {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.DEMO_REGION ?? 'us-east-1' },
});
