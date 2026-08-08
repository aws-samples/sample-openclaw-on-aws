← Back to [README](../README.md)

# Optional: Pre-installed AWS Skills

Enhance your agent with AWS-native capabilities. These are optional — OpenClaw works without them, but they add useful AWS superpowers.

## S3 File Sharing

Share files securely via pre-signed URLs. Useful for sending logs, screenshots, or documents through messaging channels without exposing your S3 bucket.

```bash
# Already bundled in this sample at:
# container/.openclaw/workspace/skills/s3-files/
```

See: [s3-files skill](../container/.openclaw/workspace/skills/s3-files/) — Upload files, generate shareable download links, create upload pages.

## Agent Toolkit for AWS

The official [Agent Toolkit for AWS](https://github.com/aws/agent-toolkit-for-aws) — gives your agent knowledge of CloudFormation, Lambda, DynamoDB, S3, CloudWatch, and 30+ AWS services.

**Install at runtime** (ask your agent):
```
"Install the AWS agent toolkit core skills"
```

**Or pre-install in the container** (add to Dockerfile):
```dockerfile
RUN npx skills add aws/agent-toolkit-for-aws/skills/core-skills
```

## Recommended skills for this deployment

| Skill | Purpose | Install |
|-------|---------|--------|
| **s3-files** | File sharing via pre-signed URLs | Pre-bundled ✅ |
| **amazon-bedrock** | Model management, guardrails config | `npx skills add aws/agent-toolkit-for-aws/skills/specialized-skills/amazon-bedrock` |
| **amazon-cloudwatch** | Log search, metrics, alarms | Included in core-skills |
| **amazon-s3** | Bucket management, lifecycle rules | Included in core-skills |
| **aws-lambda** | Function deployment, log tailing | Included in core-skills |

> **Note:** Skills are stored in the workspace directory and persist on EBS across session stop/resume. Install once, available forever.
