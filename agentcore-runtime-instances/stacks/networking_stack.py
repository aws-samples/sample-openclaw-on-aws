"""Networking stack: VPC, subnets, security groups for AgentCore Instances."""

from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    CfnOutput,
)
from constructs import Construct


class NetworkingStack(Stack):
    """Creates a VPC with private subnets for AgentCore Runtime Instances.

    AgentCore Instances require VPC networking. The container's internet
    access (for Telegram, Discord, Bedrock API) is managed by AgentCore's
    networking layer — not by a NAT Gateway in your VPC.

    This stack provides:
    - VPC with private subnets (no NAT Gateway — $0 idle cost)
    - S3 Gateway Endpoint (free) for workspace backup sync
    - Security group allowing outbound traffic
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # VPC with isolated subnets (no NAT Gateway needed)
        # AgentCore manages container networking for internet access.
        # S3 Gateway Endpoint provides free access for backup sync.
        self.vpc = ec2.Vpc(
            self,
            "OpenClawVpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
        )

        # S3 Gateway Endpoint (free) — required for workspace backup sync
        self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        # Security group for AgentCore instances
        self.agent_security_group = ec2.SecurityGroup(
            self,
            "AgentSecurityGroup",
            vpc=self.vpc,
            description="Security group for OpenClaw AgentCore instances",
            allow_all_outbound=True,
        )

        # Private subnets for capacity provider
        self.private_subnets = self.vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
        )

        # Outputs
        CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        CfnOutput(
            self,
            "PrivateSubnetIds",
            value=",".join(
                [s.subnet_id for s in self.private_subnets.subnets]
            ),
        )
        CfnOutput(
            self,
            "SecurityGroupId",
            value=self.agent_security_group.security_group_id,
        )
