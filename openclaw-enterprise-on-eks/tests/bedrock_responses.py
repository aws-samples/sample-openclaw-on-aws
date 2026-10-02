#!/usr/bin/env python3
import json
import os
import secrets
import urllib.request
from pathlib import Path

region = os.environ["AWS_REGION"]
model_id = os.environ["MODEL_ID"]
key = Path(os.environ["BEDROCK_API_KEY_FILE"]).read_text().strip()
if not key:
    raise SystemExit("Bedrock API key file is empty")

nonce = f"BEDROCK_{secrets.token_hex(12)}"
payload = {
    "model": model_id,
    "input": f"Reply with exactly this nonce: {nonce}",
    "max_output_tokens": 128,
    "store": False,
}
request = urllib.request.Request(
    f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1/responses",
    data=json.dumps(payload).encode(),
    method="POST",
    headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    },
)
# The request URL is constructed with a fixed HTTPS scheme and AWS hostname.
with urllib.request.urlopen(request, timeout=180) as response:  # nosec B310
    result = json.load(response)
    request_id = response.headers.get("x-amzn-requestid")


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)


if nonce not in "\n".join(strings(result)):
    raise SystemExit("response did not contain the nonce")
if key in json.dumps(result):
    raise SystemExit("credential appeared in the response")
print(
    json.dumps(
        {
            "http": 200,
            "model": result.get("model"),
            "requestId": request_id,
            "nonceMatched": True,
        },
        indent=2,
    )
)
