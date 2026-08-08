# Contributing Guidelines for Coding Agents

## Project Overview

AWS deployment patterns for OpenClaw (monorepo). Each subdirectory is an independent deployment approach. See [README.md](README.md) for the full list.

## Quality Standards

- Validate changes end-to-end before committing
- Any code change must update the relevant documentation
- Keep READMEs concise — reference material goes in `docs/`
- Avoid regression: check that existing functionality still works after your changes

## Documentation

- Each deployment pattern has its own README + `docs/` folder
- Getting started instructions must be prominent and tested
- Architecture decisions belong in docs, not inline comments

## Security

- No secrets in container images or git history
- IAM roles use least-privilege (scoped to specific resources)
- Channel tokens go through AWS Secrets Manager
- AWS Shared Responsibility Model disclosure must be present
- Run ASH security scan before submitting:
  ```bash
  uvx "git+https://github.com/awslabs/automated-security-helper.git@v3" --mode local --source-dir . --output-dir /tmp/ash-output
  ```
- Zero actionable findings required
