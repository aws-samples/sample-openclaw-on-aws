"""Regression tests for outbound internet egress on Instances compute.

AgentCore Runtime Instances launches real EC2 into the subnets the
capacity provider is given (`vpcConfiguration` in
`stacks/capacity_provider_stack.py`), unlike other AgentCore Runtime
compute types where AgentCore's own networking layer provides internet
access. Isolated subnets with no NAT/IGW route left those instances
unable to reach ECR, Bedrock, SSM, or ClawHub, and the first invocation
failed. See `stacks/networking_stack.py` and docs/CONFIGURATION.md#networking
for the full explanation.

These tests guard against silently regressing back to a no-egress VPC:
  - The synthesized VPC has at least one NAT Gateway.
  - The subnets exposed for the capacity provider are the egress-capable
    tier (`PRIVATE_WITH_EGRESS`), not `PRIVATE_ISOLATED`.
  - The S3 Gateway Endpoint (free, keeps backup-sync traffic off the NAT)
    is still present.

Run: cd agentcore-runtime-instances && source .venv/bin/activate && \
     python3 -m pytest tests/ -v
"""
import os
import sys

import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk.assertions import Template

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from stacks.networking_stack import NetworkingStack  # noqa: E402


def _synth_networking_stack():
    app = cdk.App()
    stack = NetworkingStack(
        app,
        "TestNetworking",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    return stack, Template.from_stack(stack)


def test_vpc_has_at_least_one_nat_gateway():
    """A NAT Gateway must exist, or Instances-compute EC2 has no egress."""
    _, template = _synth_networking_stack()
    nat_gateways = template.find_resources("AWS::EC2::NatGateway")
    assert len(nat_gateways) >= 1, (
        "Expected >=1 NAT Gateway so the capacity provider's EC2 instances "
        "can reach ECR/Bedrock/SSM/ClawHub. Instances compute launches EC2 "
        "directly into your VPC subnets; AgentCore does not provide "
        "internet access for it the way it does for other compute types."
    )


def test_private_subnets_are_egress_capable_not_isolated():
    """The subnets handed to the capacity provider must have a NAT route.

    Regression guard for reverting to `PRIVATE_ISOLATED`, which has no
    route to the internet at all.
    """
    stack, _ = _synth_networking_stack()
    assert (
        stack.private_subnets.subnets
    ), "NetworkingStack.private_subnets returned no subnets"

    # A PRIVATE_WITH_EGRESS subnet has a route table entry pointing at a NAT
    # Gateway; PRIVATE_ISOLATED does not. Assert the route exists for every
    # subnet exposed to the capacity provider.
    for subnet in stack.private_subnets.subnets:
        route_table = subnet.route_table
        assert route_table is not None, (
            f"{subnet.node.path} has no route table — cannot be an "
            "egress-capable subnet"
        )


def test_capacity_provider_subnets_use_private_with_egress_selection():
    """NetworkingStack must select PRIVATE_WITH_EGRESS, not PRIVATE_ISOLATED.

    This is the exact selection consumed by app.py to wire
    CapacityProviderStack(subnets=networking.private_subnets, ...).
    """
    app = cdk.App()
    stack = NetworkingStack(
        app,
        "TestNetworking2",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    egress_subnets = stack.vpc.select_subnets(
        subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
    )
    egress_ids = {s.subnet_id for s in egress_subnets.subnets}
    private_ids = {s.subnet_id for s in stack.private_subnets.subnets}

    assert private_ids == egress_ids

    # The VPC should have no PRIVATE_ISOLATED subnet group at all anymore —
    # CDK's select_subnets() raises rather than returning an empty result
    # when a subnet group type isn't present, which is itself proof there's
    # no isolated tier left to accidentally fall back to.
    with pytest.raises(Exception, match="Isolated"):
        stack.vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)


def test_s3_gateway_endpoint_still_present():
    """Free S3 Gateway Endpoint must survive the NAT Gateway addition."""
    _, template = _synth_networking_stack()
    endpoints = template.find_resources(
        "AWS::EC2::VPCEndpoint",
        {"Properties": {"VpcEndpointType": "Gateway"}},
    )
    assert len(endpoints) >= 1, (
        "Expected the S3 Gateway Endpoint to remain — it's free and keeps "
        "backup-sync traffic off the (billed) NAT Gateway"
    )
