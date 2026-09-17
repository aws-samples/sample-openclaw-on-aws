"""Networking stack: VPC, subnets, security groups for AgentCore Instances."""

from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    CfnOutput,
)
from constructs import Construct


class NetworkingStack(Stack):
    """Creates a VPC with egress-capable private subnets for AgentCore Runtime
    Instances.

    For other AgentCore Runtime compute types (e.g. microVMs), AgentCore's
    own networking layer provides the container's internet access, so a
    NAT Gateway in your VPC is unnecessary there. That is NOT true for
    Instances compute: the capacity provider launches real EC2 instances
    directly into the subnets you supply (see
    ``capacity_provider_stack.py`` / ``vpcConfiguration``), and those EC2
    instances get exactly the network path their subnet gives them — nothing
    more. Isolated subnets with no NAT/IGW route give them zero egress, so
    the instance cannot reach ECR (to pull the container image), Bedrock
    (inference), SSM, or ClawHub, and the first invocation fails.

    This stack provides:
    - VPC with one NAT Gateway and PRIVATE_WITH_EGRESS subnets (plus the
      CDK-managed public subnet the NAT Gateway requires)
    - S3 Gateway Endpoint (free) for workspace backup sync
    - Security group allowing outbound traffic
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # VPC with a NAT Gateway and egress-capable private subnets.
        # Instances compute launches EC2 directly into these subnets, so they
        # need a real route to the internet (ECR, Bedrock, SSM, ClawHub) —
        # AgentCore does not provide that path for this compute type. One NAT
        # Gateway is enough for a sample; it also creates the public subnet
        # tier it depends on. S3 Gateway Endpoint stays alongside it to keep
        # backup-sync traffic off the NAT (S3 is free either way, but this
        # also avoids NAT data-processing charges for that traffic).
        self.vpc = ec2.Vpc(
            self,
            "OpenClawVpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
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

        # Private (egress-capable) subnets for the capacity provider — this
        # is where the AgentCore-managed EC2 instances actually run.
        self.private_subnets = self.vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
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
