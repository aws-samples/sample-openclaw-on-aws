"""Capacity Provider stack: defines the EC2 infrastructure for AgentCore Instances.

A capacity provider is a reusable template that specifies:
- Operating system (Linux ARM64)
- Allowed instance types (c7g.large)
- Networking (VPC, subnets, security group)
- Storage (gp3 EBS root volume, 30GB)
- IAM roles (infrastructure operator role)

AgentCore uses this to provision and manage EC2 instances on your behalf.

API notes (validated Aug 2026):
- CreateCapacityProvider uses computeConfiguration.ec2Configuration
- Name regex: ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ (no hyphens!)
- permissionsConfiguration uses capacityProviderOperatorRoleArn
- Terminal status is "READY" (not "ACTIVE")
"""

from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    CfnOutput,
)
from constructs import Construct


class CapacityProviderStack(Stack):
    """Creates an AgentCore capacity provider for OpenClaw instances.

    Note: As of Aug 2026, AgentCore capacity providers are not yet available
    as native CDK L2 constructs. This stack creates the prerequisite IAM roles
    and documents the boto3 call to create the capacity provider.

    The capacity provider is created via:
      boto3 client('bedrock-agentcore-control').create_capacity_provider(...)
    See scripts/deploy.sh for the full command.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.IVpc,
        subnets: ec2.SelectedSubnets,
        security_group: ec2.ISecurityGroup,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Infrastructure operator role — AgentCore assumes this to provision/manage EC2
        # Requires broad permissions: ec2, autoscaling, events, iam, logs, cloudwatch, ssm
        self.infrastructure_role = iam.Role(
            self,
            "InfrastructureRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="Allows AgentCore to manage EC2 instances for capacity provider",
        )

        # Broad inline policy matching what AgentCore requires for infrastructure management
        self.infrastructure_role.add_to_policy(
            iam.PolicyStatement(
                sid="EC2Full",
                actions=["ec2:*"],
                resources=["*"],
            )
        )
        self.infrastructure_role.add_to_policy(
            iam.PolicyStatement(
                sid="AutoScaling",
                actions=["autoscaling:*"],
                resources=["*"],
            )
        )
        self.infrastructure_role.add_to_policy(
            iam.PolicyStatement(
                sid="EventBridge",
                actions=["events:*"],
                resources=["*"],
            )
        )
        self.infrastructure_role.add_to_policy(
            iam.PolicyStatement(
                sid="IAMPermissions",
                actions=[
                    "iam:PassRole",
                    "iam:CreateServiceLinkedRole",
                    "iam:GetRole",
                    "iam:GetInstanceProfile",
                ],
                resources=["*"],
            )
        )
        self.infrastructure_role.add_to_policy(
            iam.PolicyStatement(
                sid="Logging",
                actions=["logs:*"],
                resources=["*"],
            )
        )
        self.infrastructure_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatch",
                actions=["cloudwatch:*"],
                resources=["*"],
            )
        )
        self.infrastructure_role.add_to_policy(
            iam.PolicyStatement(
                sid="SSM",
                actions=["ssm:*"],
                resources=["*"],
            )
        )

        # Instance profile role — attached to the EC2 instance itself
        # Used for system log collection; does NOT grant agent code permissions
        self.instance_profile_role = iam.Role(
            self,
            "InstanceProfileRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="Instance profile for AgentCore managed instances (system logs)",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchAgentServerPolicy"
                ),
            ],
        )

        self.instance_profile = iam.CfnInstanceProfile(
            self,
            "InstanceProfile",
            roles=[self.instance_profile_role.role_name],
        )

        # Store configuration for the deploy script
        # Note: Name regex is ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ — no hyphens allowed!
        self.capacity_provider_name = "openclaw_capacity_provider"

        # Outputs needed for capacity provider creation
        CfnOutput(
            self,
            "InfrastructureRoleArn",
            value=self.infrastructure_role.role_arn,
        )
        CfnOutput(
            self,
            "InstanceProfileArn",
            value=self.instance_profile.attr_arn,
        )
        CfnOutput(self, "VpcId", value=vpc.vpc_id)
        CfnOutput(
            self,
            "SubnetIds",
            value=",".join([s.subnet_id for s in subnets.subnets]),
        )
        CfnOutput(
            self,
            "SecurityGroupId",
            value=security_group.security_group_id,
        )
        CfnOutput(
            self,
            "CapacityProviderName",
            value=self.capacity_provider_name,
        )

        # Output showing the actual boto3 call structure
        CfnOutput(
            self,
            "CreateCapacityProviderBoto3",
            value=(
                "client.create_capacity_provider("
                f"name='{self.capacity_provider_name}', "
                "computeConfiguration={{'ec2Configuration': {{"
                "'launchTemplateSource': {{'launchParameters': {{"
                "'instanceTypes': ['c7g.large'], "
                "'imageId': '<AMI_ID>'  # AgentCore provides this"
                "}}}}, "
                "'vpcConfiguration': {{"
                f"'subnetIds': [<SubnetIds>], "
                f"'securityGroupIds': ['{security_group.security_group_id}']"
                "}}, "
                "'rootVolume': {{'volumeType': 'gp3', 'sizeInGb': 30}}"
                "}}}}, "
                "permissionsConfiguration={{"
                f"'capacityProviderOperatorRoleArn': '{self.infrastructure_role.role_arn}'"
                "}})"
            ),
        )
