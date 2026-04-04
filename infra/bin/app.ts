#!/usr/bin/env node
/**
 * AI Trading System — CDK Application
 *
 * Stacks:
 *   1. StorageStack — S3 data bucket + Secrets Manager (API keys)
 *   2. RuntimeStack — ECR + CodeBuild + AgentCore Runtime
 *
 * Deploy order: StorageStack first, then RuntimeStack.
 * Stacks communicate via SSM parameters.
 */
import 'source-map-support/register'
import * as cdk from 'aws-cdk-lib'
import { StorageStack } from '../lib/storage-stack'
import { RuntimeStack } from '../lib/runtime-stack'

const app = new cdk.App()

const projectName = app.node.tryGetContext('projectName') || 'swing-trading-agent'
const environment = app.node.tryGetContext('environment') || 'dev'

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION || 'us-west-2',
}

// 1. Storage — S3 + Secrets Manager
new StorageStack(app, `${projectName}-storage`, {
  projectName,
  environment,
  env,
})

// 2. Runtime — AgentCore Runtime (reads storage SSM params)
new RuntimeStack(app, `${projectName}-runtime`, {
  projectName,
  environment,
  env,
})

app.synth()
