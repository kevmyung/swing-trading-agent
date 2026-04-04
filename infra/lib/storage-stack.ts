/**
 * StorageStack — S3 data bucket + DynamoDB session table + Secrets Manager.
 *
 * Hot path (DynamoDB):
 *   - Session metadata, progress, daily stats
 *   - Single table design: PK=session_id, SK=record_type
 *
 * Cold path (S3):
 *   - Portfolio state, snapshots, day logs, fixtures
 */
import * as cdk from 'aws-cdk-lib'
import * as s3 from 'aws-cdk-lib/aws-s3'
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb'
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager'
import * as ssm from 'aws-cdk-lib/aws-ssm'
import { Construct } from 'constructs'

export interface StorageStackProps extends cdk.StackProps {
  projectName: string
  environment: string
}

export class StorageStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: StorageStackProps) {
    super(scope, id, props)

    const { projectName, environment } = props

    // ── S3 Bucket (fixtures, sessions cold path, artifacts) ────────
    const dataBucket = new s3.Bucket(this, 'DataBucket', {
      bucketName: `${projectName}-data-${cdk.Aws.ACCOUNT_ID}-${cdk.Aws.REGION}`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      lifecycleRules: [
        { prefix: 'tmp/', expiration: cdk.Duration.days(7) },
      ],
    })

    // ── DynamoDB — Session metadata (hot path) ─────────────────────
    const sessionTable = new dynamodb.Table(this, 'SessionTable', {
      tableName: `${projectName}-sessions`,
      partitionKey: { name: 'session_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'record_type', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
    })

    // ── Secrets Manager — API Keys ─────────────────────────────────
    const polygonSecret = new secretsmanager.Secret(this, 'PolygonApiKey', {
      secretName: `/${projectName}/${environment}/polygon-api-key`,
      description: 'Polygon.io API key for market data',
    })

    const alpacaSecret = new secretsmanager.Secret(this, 'AlpacaApiKeys', {
      secretName: `/${projectName}/${environment}/alpaca-api-keys`,
      description: 'Alpaca API keys (JSON: {"paper":{"api_key":"...","secret_key":"..."},"live":{...}})',
    })

    // ── SSM Parameters (for RuntimeStack to read) ──────────────────
    new ssm.StringParameter(this, 'DataBucketNameParam', {
      parameterName: `/${projectName}/${environment}/s3/data-bucket-name`,
      stringValue: dataBucket.bucketName,
      tier: ssm.ParameterTier.STANDARD,
    })

    new ssm.StringParameter(this, 'DataBucketArnParam', {
      parameterName: `/${projectName}/${environment}/s3/data-bucket-arn`,
      stringValue: dataBucket.bucketArn,
      tier: ssm.ParameterTier.STANDARD,
    })

    new ssm.StringParameter(this, 'SessionTableNameParam', {
      parameterName: `/${projectName}/${environment}/dynamodb/session-table-name`,
      stringValue: sessionTable.tableName,
      tier: ssm.ParameterTier.STANDARD,
    })

    new ssm.StringParameter(this, 'SessionTableArnParam', {
      parameterName: `/${projectName}/${environment}/dynamodb/session-table-arn`,
      stringValue: sessionTable.tableArn,
      tier: ssm.ParameterTier.STANDARD,
    })

    new ssm.StringParameter(this, 'PolygonSecretArnParam', {
      parameterName: `/${projectName}/${environment}/secrets/polygon-arn`,
      stringValue: polygonSecret.secretArn,
      tier: ssm.ParameterTier.STANDARD,
    })

    new ssm.StringParameter(this, 'AlpacaSecretArnParam', {
      parameterName: `/${projectName}/${environment}/secrets/alpaca-arn`,
      stringValue: alpacaSecret.secretArn,
      tier: ssm.ParameterTier.STANDARD,
    })

    // ── Outputs ────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'DataBucketName', { value: dataBucket.bucketName })
    new cdk.CfnOutput(this, 'SessionTableName', { value: sessionTable.tableName })
    new cdk.CfnOutput(this, 'PolygonSecretArn', { value: polygonSecret.secretArn })
    new cdk.CfnOutput(this, 'AlpacaSecretArn', { value: alpacaSecret.secretArn })
  }
}
