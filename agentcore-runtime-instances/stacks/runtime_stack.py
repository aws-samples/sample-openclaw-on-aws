"""Runtime stack: AgentCore agent runtime with Instances compute type.

Creates the agent runtime resource that defines:
- What runs (container image from ECR)
- How it runs (Instances compute, linked to capacity provider via ARN)
- Execution role (Bedrock + ECR + Logs + CloudWatch + X-Ray)

Note: filesystemConfigurations (S3 Files, EFS, sessionStorage) is NOT supported
with capacityProviderConfiguration (Instances compute type). Storage is provided
by the EBS root volume defined in the capacity provider.

API notes (validated Aug 2026):
- capacityProviderConfiguration uses `capacityProviderArn` (not name or id)
- Agent runtime terminal status is "READY"
"""

from aws_cdk import (
    Stack,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    CfnOutput,
)
from constructs import Construct
import os


class RuntimeStack(Stack):
    """Creates the AgentCore agent runtime for OpenClaw.

    The runtime defines the container image and links to a capacity provider.
    Storage is handled by the EBS root volume (configured in the capacity provider),
    NOT by filesystemConfigurations (which is unsupported for Instances compute).

    Note: AgentCore Runtime is not yet a native CDK L2 construct. This stack
    builds and pushes the container image to ECR and documents the boto3 call
    to create the runtime.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        capacity_provider_name: str,
        s3_backup_bucket_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Build container image and push to ECR
        container_dir = os.path.join(os.path.dirname(__file__), "..", "container")
        self.image = ecr_assets.DockerImageAsset(
            self,
            "OpenClawImage",
            directory=os.path.abspath(container_dir),
            platform=ecr_assets.Platform.LINUX_ARM64,
        )

        # Execution role — this is what the agent code runs as
        # Needs: Bedrock invoke, ECR pull, Logs, CloudWatch, X-Ray, S3 backup
        self.execution_role = iam.Role(
            self,
            "ExecutionRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="Execution role for OpenClaw agent on AgentCore Runtime",
        )

        # Bedrock model invocation
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockInvoke",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=["*"],  # All Bedrock models
            )
        )

        # ECR pull permissions (required for container image retrieval)
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="ECRPull",
                actions=[
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=["*"],
            )
        )

        # CloudWatch Logs
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogs",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=["*"],
            )
        )

        # CloudWatch Metrics
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchMetrics",
                actions=[
                    "cloudwatch:PutMetricData",
                ],
                resources=["*"],
            )
        )

        # X-Ray tracing
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="XRay",
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],
            )
        )

        # S3 backup bucket access (for background workspace sync)
        # Scoped to the sessions/* prefix: container/main.py stores each
        # AgentCore session's backup under sessions/<sanitized-session-id>/
        # rather than one flat shared prefix, so a per-session S3 key
        # collision (and the cross-tenant data leak that implies) requires
        # both the sanitized-session-id allowlist in main.py to be bypassed
        # AND this IAM scoping to be bypassed. A single execution role is
        # shared by every session on this runtime, so this is
        # defense-in-depth, not a per-tenant IAM boundary -- true per-session
        # isolation would need a per-session role, which AgentCore Instances
        # compute does not support today.
        if s3_backup_bucket_arn and s3_backup_bucket_arn != "PLACEHOLDER_ARN":
            self.execution_role.add_to_policy(
                iam.PolicyStatement(
                    sid="S3BackupSyncObjects",
                    actions=[
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                    ],
                    resources=[
                        f"{s3_backup_bucket_arn}/sessions/*",
                    ],
                )
            )
            self.execution_role.add_to_policy(
                iam.PolicyStatement(
                    sid="S3BackupSyncList",
                    actions=[
                        "s3:ListBucket",
                    ],
                    resources=[
                        s3_backup_bucket_arn,
                    ],
                    conditions={
                        "StringLike": {
                            "s3:prefix": ["sessions/*"],
                        },
                    },
                )
            )

        # Secrets Manager access (for channel tokens)
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="SecretsManagerChannelTokens",
                actions=[
                    "secretsmanager:GetSecretValue",
                ],
                resources=[
                    f"arn:aws:secretsmanager:*:{Stack.of(self).account}:secret:openclaw/*",
                ],
            )
        )

        # SSM Parameter Store (for S3 backup bucket discovery)
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="SSMParameterRead",
                actions=[
                    "ssm:GetParameter",
                ],
                resources=[
                    f"arn:aws:ssm:*:{Stack.of(self).account}:parameter/openclaw/*",
                ],
            )
        )

        # Outputs
        CfnOutput(self, "ContainerImageUri", value=self.image.image_uri)
        CfnOutput(self, "ExecutionRoleArn", value=self.execution_role.role_arn)
        CfnOutput(
            self,
            "CreateRuntimeBoto3",
            value=(
                "client.create_agent_runtime("
                "agentRuntimeName='openclaw_agent', "
                "agentRuntimeArtifact={'containerConfiguration': "
                f"{{'containerUri': '{self.image.image_uri}'}}}}, "
                "roleArn='<ExecutionRoleArn>', "
                "capacityProviderConfiguration={'capacityProviderArn': '<CAPACITY_PROVIDER_ARN>'}"
                ")"
                "\n\n"
                "# NOTE: Do NOT pass filesystemConfigurations with Instances compute.\n"
                "# S3 Files, EFS, and sessionStorage are not supported with capacityProviderConfiguration.\n"
                "# Storage is provided by the EBS root volume in the capacity provider."
            ),
        )
