"""Storage stack: S3 bucket for workspace backup.

Persistence model for Instances compute:
- EBS root volume (defined in capacity provider) is the live workspace
- S3 bucket provides background backup for disaster recovery
- S3 sync runs every 5 min from the container (non-blocking)
- Restore from S3 only when workspace is empty (session expired after 14 days — rare)

NOTE: S3 Files / EFS / sessionStorage are NOT supported with
capacityProviderConfiguration (Instances compute type). This stack provides
only the S3 bucket for backup purposes.
"""

from aws_cdk import (
    Stack,
    RemovalPolicy,
    Duration,
    aws_s3 as s3,
    aws_ssm as ssm,
    CfnOutput,
)
from constructs import Construct


class StorageStack(Stack):
    """Creates an S3 bucket for workspace backup sync.

    Architecture (Instances compute):
      EBS Root Volume (30GB gp3)     ← live workspace (zero cold start)
           │
           │  aws s3 sync (every 5 min, background)
           ▼
      S3 Bucket (versioned)          ← backup / disaster recovery
           │
           └─ Lifecycle rules: expire old versions after 30 days

    The bucket name is stored in SSM Parameter Store so the container
    can discover it at boot without hardcoding.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # S3 bucket — backup storage layer
        self.bucket = s3.Bucket(
            self,
            "WorkspaceBucket",
            bucket_name=None,  # Auto-generated
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireOldVersions",
                    noncurrent_version_expiration=Duration.days(30),
                    enabled=True,
                ),
                s3.LifecycleRule(
                    id="TransitionOldVersions",
                    noncurrent_versions_to_retain=3,
                    noncurrent_version_transitions=[
                        s3.NoncurrentVersionTransition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=Duration.days(7),
                        ),
                    ],
                    enabled=True,
                ),
            ],
        )

        # Store bucket name in SSM Parameter Store (free)
        # Container discovers this at boot to enable S3 backup sync
        ssm.StringParameter(
            self,
            "BackupBucketParam",
            parameter_name="/openclaw/backup-bucket",
            string_value=self.bucket.bucket_name,
            description="OpenClaw workspace backup S3 bucket name",
        )

        # Outputs
        CfnOutput(self, "BucketName", value=self.bucket.bucket_name)
        CfnOutput(self, "BucketArn", value=self.bucket.bucket_arn)
