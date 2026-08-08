#!/usr/bin/env python3
"""CDK app for deploying OpenClaw on AgentCore Runtime Instances."""

import os

import aws_cdk as cdk

from stacks.networking_stack import NetworkingStack
from stacks.storage_stack import StorageStack
from stacks.capacity_provider_stack import CapacityProviderStack
from stacks.runtime_stack import RuntimeStack

app = cdk.App()

# Region resolution order: AWS_REGION env var → cdk.json context → us-east-1
region = (
    os.environ.get("AWS_REGION")
    or app.node.try_get_context("region")
    or "us-east-1"
)

env = cdk.Environment(
    account=app.node.try_get_context("account") or None,
    region=region,
)

networking = NetworkingStack(app, "OpenClaw-Networking", env=env)

storage = StorageStack(
    app,
    "OpenClaw-Storage",
    env=env,
)

capacity_provider = CapacityProviderStack(
    app,
    "OpenClaw-CapacityProvider",
    vpc=networking.vpc,
    subnets=networking.private_subnets,
    security_group=networking.agent_security_group,
    env=env,
)

runtime = RuntimeStack(
    app,
    "OpenClaw-Runtime",
    capacity_provider_name=capacity_provider.capacity_provider_name,
    s3_backup_bucket_arn=storage.bucket.bucket_arn,
    env=env,
)

runtime.add_stack_dependency(capacity_provider)
runtime.add_stack_dependency(storage)

app.synth()
