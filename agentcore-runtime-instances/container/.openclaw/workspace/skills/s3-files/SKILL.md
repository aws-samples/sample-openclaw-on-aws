---
name: s3-files
description: Upload and share files via Amazon S3 with time-limited pre-signed URLs. Generate download links, create upload pages for receiving files, and manage secure file sharing without exposing S3 buckets publicly.
metadata:
  {
    "openclaw": {
      "emoji": "📤",
      "requires": { "aws": ["s3"], "node": ">=18.0.0" },
      "homepage": "https://github.com/aws-samples/sample-OpenClaw-on-AWS-with-Bedrock"
    }
  }
---

# S3 Files Skill

Upload and share files via Amazon S3 with automatic expiration and clean download filenames.

## Features

- 📤 Upload files to S3 and generate shareable download links
- 🔗 Create pre-signed URLs for existing S3 objects
- 📥 Generate upload pages for receiving files from others
- ⏰ Automatic expiration (configurable, default 24 hours)
- 🔒 No public S3 buckets required

## Quick Reference

| Command | Purpose |
|---------|---------|
| `node upload.js <file-path>` | Upload file and get download link |
| `node download-url.js <s3-key>` | Generate download URL for existing file |
| `node generate-upload-page.js [max-size-mb]` | Create upload page for receiving files |

## Configuration

Copy `config.example.json` to `config.json`:

```json
{
  "bucketName": "your-bucket-name",
  "region": "us-east-1",
  "defaultExpirationHours": 24,
  "maxUploadSizeMB": 100
}
```

The bucket name can be set to the S3 backup bucket created by the CDK stack (check deploy output for `BucketName`).

## Usage

```bash
cd ~/.openclaw/workspace/skills/s3-files
node upload.js /path/to/file.pdf
```

## Rules for OpenClaw

1. Always ask before uploading files
2. Remind user that URLs expire (default 24h)
3. Don't share sensitive files without user confirmation
4. Respect rate limits (10 requests/minute)
5. Only create upload pages when explicitly requested
