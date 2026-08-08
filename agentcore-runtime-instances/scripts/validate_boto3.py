#!/usr/bin/env python3
"""Validate AgentCore Runtime Instances APIs via boto3."""

import boto3
import sys

REGION = "us-east-1"


def main():
    print("=" * 60)
    print(" AgentCore Runtime Instances - boto3 Validation")
    print("=" * 60)

    control = boto3.client("bedrock-agentcore-control", region_name=REGION)

    print()
    print("[1/4] Checking boto3 API surface...")
    capacity_methods = [m for m in dir(control) if "capacity" in m.lower()]
    print(f"  Methods: {capacity_methods}")

    if not capacity_methods:
        print(f"  FAIL: boto3={boto3.__version__} has no capacity provider support")
        sys.exit(1)
    print("  PASS: Capacity provider API available")

    print()
    print("[2/4] Introspecting CreateCapacityProvider...")
    try:
        sm = control._service_model
        cp_op = sm.operation_model("CreateCapacityProvider")
        print(f"  Params: {list(cp_op.input_shape.members.keys())}")
    except Exception as exc:
        print(f"  Error: {type(exc).__name__}: {exc}")

    print()
    print("[3/4] Introspecting CreateAgentRuntime...")
    try:
        rt_op = sm.operation_model("CreateAgentRuntime")
        members = list(rt_op.input_shape.members.keys())
        print(f"  Params: {members}")
        has_cp = "capacityProviderConfiguration" in members
        has_fs = "filesystemConfigurations" in members
        print(f"  capacityProviderConfiguration: {has_cp}")
        print(f"  filesystemConfigurations: {has_fs}")
    except Exception as exc:
        print(f"  Error: {type(exc).__name__}: {exc}")

    print()
    print("[4/4] Calling list_capacity_providers...")
    try:
        resp = control.list_capacity_providers()
        providers = resp.get("capacityProviders", [])
        print(f"  PASS: API reachable. Found {len(providers)} provider(s)")
    except Exception as exc:
        code = ""
        if hasattr(exc, "response"):
            code = exc.response.get("Error", {}).get("Code", "")
        if code in ("UnrecognizedClientException", "ExpiredTokenException"):
            print("  WARN: Creds expired but API endpoint exists")
        else:
            print(f"  {type(exc).__name__}: {exc}")

    print()
    print(f"boto3={boto3.__version__} region={REGION}")
    print("=" * 60)


if __name__ == "__main__":
    main()
