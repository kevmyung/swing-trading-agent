/**
 * RuntimeStack — ECR + CodeBuild + AgentCore Runtime.
 *
 * Pipeline: S3 source upload → CodeBuild (Docker build + push) → AgentCore Runtime.
 * Requires StorageStack to be deployed first (reads SSM parameters).
 */
import * as cdk        from 'aws-cdk-lib'
import * as ecr        from 'aws-cdk-lib/aws-ecr'
import * as iam        from 'aws-cdk-lib/aws-iam'
import * as ssm        from 'aws-cdk-lib/aws-ssm'
import * as s3         from 'aws-cdk-lib/aws-s3'
import * as s3deploy   from 'aws-cdk-lib/aws-s3-deployment'
import * as codebuild  from 'aws-cdk-lib/aws-codebuild'
import * as lambda     from 'aws-cdk-lib/aws-lambda'
import * as cr         from 'aws-cdk-lib/custom-resources'
import * as agentcore  from 'aws-cdk-lib/aws-bedrockagentcore'
import * as scheduler  from 'aws-cdk-lib/aws-scheduler'
import { Construct }   from 'constructs'
import * as path       from 'path'

export interface RuntimeStackProps extends cdk.StackProps {
  projectName: string
  environment: string
}

export class RuntimeStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: RuntimeStackProps) {
    super(scope, id, props)

    const { projectName, environment } = props

    // Unique tag per deployment — forces CloudFormation to update Runtime
    const buildTag = Date.now().toString()

    // ── Read SSM parameters from StorageStack ──────────────────────
    const polygonSecretArn = ssm.StringParameter.valueForStringParameter(
      this, `/${projectName}/${environment}/secrets/polygon-arn`)
    const alpacaSecretArn = ssm.StringParameter.valueForStringParameter(
      this, `/${projectName}/${environment}/secrets/alpaca-arn`)
    const dataBucketName = ssm.StringParameter.valueForStringParameter(
      this, `/${projectName}/${environment}/s3/data-bucket-name`)
    const dataBucketArn = ssm.StringParameter.valueForStringParameter(
      this, `/${projectName}/${environment}/s3/data-bucket-arn`)
    const sessionTableName = ssm.StringParameter.valueForStringParameter(
      this, `/${projectName}/${environment}/dynamodb/session-table-name`)
    const sessionTableArn = ssm.StringParameter.valueForStringParameter(
      this, `/${projectName}/${environment}/dynamodb/session-table-arn`)

    // ── ECR Repository ─────────────────────────────────────────────
    const repository = new ecr.Repository(this, 'Repository', {
      repositoryName: `${projectName}-trading-agent`,
      removalPolicy:  cdk.RemovalPolicy.DESTROY,
      emptyOnDelete:  true,
      imageScanOnPush: true,
      lifecycleRules: [{ maxImageCount: 10 }],
    })

    // ── CodeBuild Pipeline ─────────────────────────────────────────
    const sourceBucket = new s3.Bucket(this, 'SourceBucket', {
      bucketName:        `${projectName}-agent-sources-${this.account}-${this.region}`,
      removalPolicy:     cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules:    [{ expiration: cdk.Duration.days(7), id: 'DeleteOldSources' }],
    })

    const codeBuildRole = new iam.Role(this, 'CodeBuildRole', {
      assumedBy:   new iam.ServicePrincipal('codebuild.amazonaws.com'),
      description: 'Build role for trading agent container image pipeline',
    })
    codeBuildRole.addToPolicy(new iam.PolicyStatement({
      actions:   ['ecr:GetAuthorizationToken'],
      resources: ['*'],
    }))
    codeBuildRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'ecr:BatchCheckLayerAvailability', 'ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer',
        'ecr:PutImage', 'ecr:InitiateLayerUpload', 'ecr:UploadLayerPart', 'ecr:CompleteLayerUpload',
      ],
      resources: [repository.repositoryArn],
    }))
    codeBuildRole.addToPolicy(new iam.PolicyStatement({
      actions:   ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/aws/codebuild/${projectName}-*`],
    }))
    codeBuildRole.addToPolicy(new iam.PolicyStatement({
      actions:   ['s3:GetObject', 's3:ListBucket'],
      resources: [sourceBucket.bucketArn, `${sourceBucket.bucketArn}/*`],
    }))

    const buildProject = new codebuild.Project(this, 'BuildProject', {
      projectName: `${projectName}-agent-builder`,
      description: 'Builds container image for AgentCore trading runtime',
      role:        codeBuildRole,
      environment: {
        buildImage:  codebuild.LinuxBuildImage.AMAZON_LINUX_2_ARM_3,
        computeType: codebuild.ComputeType.SMALL,
        privileged:  true,
      },
      source: codebuild.Source.s3({ bucket: sourceBucket, path: 'agent-source/' }),
      buildSpec: codebuild.BuildSpec.fromObject({
        version: '0.2',
        phases: {
          pre_build: {
            commands: [
              'echo Logging in to Amazon ECR...',
              `aws ecr get-login-password --region ${this.region} | docker login --username AWS --password-stdin ${this.account}.dkr.ecr.${this.region}.amazonaws.com`,
            ],
          },
          build: {
            commands: [
              'echo Building Docker image for ARM64...',
              'docker build --platform linux/arm64 -t agent:latest .',
              `docker tag agent:latest ${repository.repositoryUri}:${buildTag}`,
              `docker tag agent:latest ${repository.repositoryUri}:latest`,
            ],
          },
          post_build: {
            commands: [
              'echo Pushing Docker image to ECR...',
              `docker push ${repository.repositoryUri}:${buildTag}`,
              `docker push ${repository.repositoryUri}:latest`,
              'echo Build completed successfully',
            ],
          },
        },
      }),
    })

    // Upload source code to S3 (exclude large/unnecessary files)
    const sourceUpload = new s3deploy.BucketDeployment(this, 'SourceUpload', {
      sources: [
        s3deploy.Source.asset(path.join(__dirname, '../..'), {
          exclude: [
            '__pycache__/**', '*.pyc', '.git/**', '.DS_Store', '*.log',
            '.cache/**', '.state/**', '.venv/**', 'venv/**',
            'state/logs/**', 'state/research/**',
            'backtest/fixtures/**', 'backtest/sessions/**',
            'infra/**', 'frontend/**', 'tests/**', 'data/**',
            '.pytest_cache/**', 'node_modules/**',
          ],
        }),
      ],
      destinationBucket:    sourceBucket,
      destinationKeyPrefix: 'agent-source/',
      prune:                true,
      retainOnDelete:       false,
    })

    // Trigger CodeBuild after source upload
    const buildTrigger = new cr.AwsCustomResource(this, 'TriggerCodeBuild', {
      onCreate: {
        service:            'CodeBuild',
        action:             'startBuild',
        parameters:         { projectName: buildProject.projectName },
        physicalResourceId: cr.PhysicalResourceId.of(`build-${Date.now()}`),
      },
      onUpdate: {
        service:            'CodeBuild',
        action:             'startBuild',
        parameters:         { projectName: buildProject.projectName },
        physicalResourceId: cr.PhysicalResourceId.of(`build-${Date.now()}`),
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions:   ['codebuild:StartBuild', 'codebuild:BatchGetBuilds'],
          resources: [buildProject.projectArn],
        }),
      ]),
      timeout: cdk.Duration.minutes(5),
    })
    buildTrigger.node.addDependency(sourceUpload)

    // Wait for CodeBuild to finish before creating Runtime
    const buildWaiterFn = new lambda.Function(this, 'BuildWaiterFunction', {
      runtime:    lambda.Runtime.NODEJS_22_X,
      handler:    'index.handler',
      timeout:    cdk.Duration.minutes(15),
      memorySize: 256,
      code: lambda.Code.fromInline(`
const { CodeBuildClient, BatchGetBuildsCommand } = require('@aws-sdk/client-codebuild');

exports.handler = async (event) => {
  if (event.RequestType === 'Delete') {
    return sendResponse(event, 'SUCCESS', {});
  }

  const buildId = event.ResourceProperties.BuildId;
  const client = new CodeBuildClient({});
  const deadline = Date.now() + 14 * 60 * 1000;

  while (Date.now() < deadline) {
    const { builds } = await client.send(new BatchGetBuildsCommand({ ids: [buildId] }));
    const status = builds[0].buildStatus;
    console.log('Build status:', status);
    if (status === 'SUCCEEDED') return sendResponse(event, 'SUCCESS', {});
    if (['FAILED','FAULT','TIMED_OUT','STOPPED'].includes(status))
      return sendResponse(event, 'FAILED', {}, 'Build ' + status);
    await new Promise(r => setTimeout(r, 30000));
  }
  return sendResponse(event, 'FAILED', {}, 'Build timed out after 14 minutes');
};

async function sendResponse(event, status, data, reason) {
  const body = JSON.stringify({
    Status: status,
    Reason: reason || 'See CloudWatch logs',
    PhysicalResourceId: event.PhysicalResourceId || event.RequestId,
    StackId: event.StackId,
    RequestId: event.RequestId,
    LogicalResourceId: event.LogicalResourceId,
    Data: data,
  });
  const https = require('https'), url = require('url');
  const p = url.parse(event.ResponseURL);
  return new Promise((res, rej) => {
    const req = https.request({ hostname: p.hostname, port: 443, path: p.path,
      method: 'PUT', headers: { 'Content-Type': '', 'Content-Length': body.length } },
      r => res(data));
    req.on('error', rej);
    req.write(body);
    req.end();
  });
}
      `),
    })
    buildWaiterFn.addToRolePolicy(new iam.PolicyStatement({
      actions:   ['codebuild:BatchGetBuilds'],
      resources: [buildProject.projectArn],
    }))

    const buildWaiter = new cdk.CustomResource(this, 'BuildWaiter', {
      serviceToken: buildWaiterFn.functionArn,
      properties:   { BuildId: buildTrigger.getResponseField('build.id') },
    })
    buildWaiter.node.addDependency(buildTrigger)

    // ── Execution Role ─────────────────────────────────────────────
    const executionRole = new iam.Role(this, 'ExecutionRole', {
      assumedBy:   new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for Trading Agent Runtime',
    })

    // ECR pull
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions:   ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'],
      resources: [repository.repositoryArn],
    }))
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions:   ['ecr:GetAuthorizationToken'],
      resources: ['*'],
    }))

    // CloudWatch Logs
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents',
        'logs:DescribeLogGroups', 'logs:DescribeLogStreams',
      ],
      resources: [
        `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
        `arn:aws:logs:${this.region}:${this.account}:log-group:*`,
      ],
    }))

    // X-Ray
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'xray:PutTraceSegments', 'xray:PutTelemetryRecords',
        'xray:GetSamplingRules', 'xray:GetSamplingTargets',
      ],
      resources: ['*'],
    }))

    // Bedrock Model Access
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream',
        'bedrock:Converse', 'bedrock:ConverseStream',
      ],
      resources: [
        'arn:aws:bedrock:*::foundation-model/*',
        `arn:aws:bedrock:${this.region}:${this.account}:*`,
      ],
    }))

    // S3 — full access to data bucket
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions:   ['s3:*'],
      resources: [dataBucketArn, `${dataBucketArn}/*`],
    }))

    // DynamoDB — session table
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
        'dynamodb:DeleteItem', 'dynamodb:Query', 'dynamodb:Scan',
        'dynamodb:BatchWriteItem',
      ],
      resources: [sessionTableArn],
    }))

    // Secrets Manager — read + write API keys
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions:   ['secretsmanager:GetSecretValue', 'secretsmanager:PutSecretValue'],
      resources: [polygonSecretArn, alpacaSecretArn],
    }))

    // SSM Parameter Store — read + write (scheduler config)
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions:   ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:PutParameter'],
      resources: [
        `arn:aws:ssm:${this.region}:${this.account}:parameter/${projectName}/*`,
      ],
    }))

    // EventBridge Scheduler — enable/disable rules from API
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions:   ['scheduler:GetSchedule', 'scheduler:UpdateSchedule'],
      resources: [
        `arn:aws:scheduler:${this.region}:${this.account}:schedule/${projectName}-trading/*`,
      ],
    }))
    // Need PassRole for UpdateSchedule (re-attaches target role)
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions:   ['iam:PassRole'],
      resources: [
        `arn:aws:iam::${this.account}:role/${projectName}-scheduler-role`,
      ],
    }))

    // ── AgentCore Runtime ──────────────────────────────────────────
    const runtimeName = projectName.replace(/-/g, '_') + '_trading_runtime'
    const runtime = new agentcore.CfnRuntime(this, 'Runtime', {
      agentRuntimeName: runtimeName,
      description:      `AI Trading System (deploy: ${buildTag})`,
      roleArn:          executionRole.roleArn,
      agentRuntimeArtifact: {
        containerConfiguration: {
          containerUri: `${repository.repositoryUri}:${buildTag}`,
        },
      },
      networkConfiguration: { networkMode: 'PUBLIC' },
      protocolConfiguration: 'HTTP',
      environmentVariables: {
        STORE_MODE:              'cloud',
        DATA_BUCKET:             dataBucketName,
        SESSION_TABLE:           sessionTableName,
        S3_BUCKET:               dataBucketName,
        AGENT_SESSION_STORAGE:   's3',
        POLYGON_SECRET_ARN:      polygonSecretArn,
        ALPACA_SECRET_ARN:       alpacaSecretArn,
        AWS_REGION:              this.region,
        LOG_LEVEL:               'INFO',
        DEPLOY_ID:               buildTag,
      },
    })

    runtime.node.addDependency(executionRole)
    runtime.node.addDependency(buildWaiter)

    // ── SSM Parameters ─────────────────────────────────────────────
    new ssm.StringParameter(this, 'RuntimeArnParam', {
      parameterName: `/${projectName}/${environment}/agentcore/runtime-arn`,
      stringValue:   runtime.attrAgentRuntimeArn,
      tier:          ssm.ParameterTier.STANDARD,
    })

    // ── Outputs ────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'RuntimeArn', { value: runtime.attrAgentRuntimeArn })
    new cdk.CfnOutput(this, 'RepositoryUri', { value: repository.repositoryUri })
    new cdk.CfnOutput(this, 'DeployId', { value: buildTag })

    // ══════════════════════════════════════════════════════════════
    // Trading Scheduler (EventBridge Scheduler → Lambda → AgentCore)
    //
    // Always deployed with rules DISABLED. The dashboard API dynamically
    // enables/disables rules when the user starts/stops trading from the UI.
    // Runtime config (session_id, mode) is stored in SSM Parameter and
    // read by Lambda on each invocation.
    // ══════════════════════════════════════════════════════════════

    // SSM parameter for dynamic scheduler config (written by API at runtime)
    const schedulerConfigParam = new ssm.StringParameter(this, 'SchedulerConfigParam', {
      parameterName: `/${projectName}/${environment}/scheduler/config`,
      stringValue: JSON.stringify({ enabled: false, mode: 'paper', session_id: '' }),
      tier: ssm.ParameterTier.STANDARD,
      description: 'Dynamic trading scheduler config (managed by dashboard API)',
    })

    // Lambda that invokes AgentCore for a given cycle
    const schedulerFn = new lambda.Function(this, 'SchedulerFn', {
      functionName: `${projectName}-trading-scheduler`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'scheduler')),
      timeout: cdk.Duration.seconds(30),
      memorySize: 128,
      environment: {
        AGENTCORE_RUNTIME_ARN: runtime.attrAgentRuntimeArn,
        SCHEDULER_CONFIG_PARAM: schedulerConfigParam.parameterName,
        ALPACA_SECRET_ARN: alpacaSecretArn,
      },
    })

    // Lambda permissions
    schedulerFn.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'bedrock-agentcore:InvokeAgentRuntime',
        'bedrock-agentcore:StartRuntimeSession',
      ],
      resources: [
        runtime.attrAgentRuntimeArn,
        `${runtime.attrAgentRuntimeArn}/*`,
      ],
    }))
    // Read Alpaca credentials for market calendar check
    schedulerFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue'],
      resources: [alpacaSecretArn],
    }))
    schedulerConfigParam.grantRead(schedulerFn)

    // IAM role for EventBridge Scheduler to invoke Lambda
    const schedulerRole = new iam.Role(this, 'SchedulerRole', {
      roleName: `${projectName}-scheduler-role`,
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
    })
    schedulerFn.grantInvoke(schedulerRole)

    // Schedule group
    const scheduleGroup = new scheduler.CfnScheduleGroup(this, 'TradingScheduleGroup', {
      name: `${projectName}-trading`,
    })

    // Three trading cycle schedules (Mon-Fri, America/New_York) — DISABLED by default
    const cycles: { name: string; cycle: string; cron: string }[] = [
      { name: 'Morning',   cycle: 'MORNING',   cron: 'cron(0 9 ? * MON-FRI *)'  },
      { name: 'Intraday',  cycle: 'INTRADAY',  cron: 'cron(30 10 ? * MON-FRI *)' },
      { name: 'EodSignal', cycle: 'EOD_SIGNAL', cron: 'cron(0 16 ? * MON-FRI *)' },
    ]

    for (const c of cycles) {
      new scheduler.CfnSchedule(this, `Schedule${c.name}`, {
        name: `${projectName}-${c.cycle.toLowerCase().replace('_', '-')}`,
        groupName: scheduleGroup.name,
        scheduleExpression: c.cron,
        scheduleExpressionTimezone: 'America/New_York',
        flexibleTimeWindow: { mode: 'OFF' },
        state: 'DISABLED',
        target: {
          arn: schedulerFn.functionArn,
          roleArn: schedulerRole.roleArn,
          input: JSON.stringify({ cycle: c.cycle }),
        },
      })
    }

    // SSM: schedule group name (for API to enable/disable rules)
    new ssm.StringParameter(this, 'ScheduleGroupParam', {
      parameterName: `/${projectName}/${environment}/scheduler/group-name`,
      stringValue: `${projectName}-trading`,
      tier: ssm.ParameterTier.STANDARD,
    })

    new cdk.CfnOutput(this, 'SchedulerLambdaArn', { value: schedulerFn.functionArn })
    new cdk.CfnOutput(this, 'ScheduleGroupName', { value: `${projectName}-trading` })
  }
}
