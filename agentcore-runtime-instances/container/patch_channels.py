"""Patch openclaw.json with channel tokens from Secrets Manager.

Reads JSON from stdin: {"telegram": "TOKEN", "discord": "TOKEN", ...}
Patches the config file at the path given as argv[1].
"""

import json
import sys


def main():
    if len(sys.argv) != 2:
        print("[patch_channels] Usage: echo '{...}' | python3 patch_channels.py <config-path>")
        sys.exit(1)

    config_path = sys.argv[1]
    secrets = json.load(sys.stdin)

    with open(config_path) as f:
        config = json.load(f)

    channels = {}

    if "telegram" in secrets:
        channels["telegram"] = {"botToken": secrets["telegram"]}
    if "discord" in secrets:
        channels["discord"] = {"botToken": secrets["discord"]}
    if "slack_app_token" in secrets and "slack_bot_token" in secrets:
        channels["slack"] = {
            "enabled": True,
            "appToken": secrets["slack_app_token"],
            "botToken": secrets["slack_bot_token"],
        }

    if channels:
        config["channels"] = channels
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        print(f"[start.sh] Configured {len(channels)} channel(s) from Secrets Manager.")
    else:
        print("[start.sh] No recognized channel tokens in secret.")


if __name__ == "__main__":
    main()
