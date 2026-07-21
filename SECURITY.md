# Security policy

## Reporting a vulnerability

If you find a security issue in Bedrock Ops Lens, please do not file a public issue. Instead, contact the maintainers privately so the fix can ship before disclosure.

## What's in scope

- The CloudFormation template (`infra/cloudformation.yaml`)
- The reader-role template (`infra/monitored-account-role.yaml`)
- The Backend Lambda code (`backend/`)
- The ingestion code (`ingestion/`)
- The MCP server (`mcp/`)
- The deploy script and helpers (`deploy.sh`, `scripts/`)

## What's not in scope

- Issues in third-party dependencies (report upstream)
- Issues that require pre-existing AWS access at admin level (the project assumes the deployer is trusted)
- Theoretical attacks that depend on AWS service behaviour we don't control

## What we ship in this repo to help you stay safe

- `infra/policy-guard.rules` (cfn-guard) blocks known-unsafe patterns at deploy time: bare `Principal: "*"`, S3 buckets without `BlockPublicAccess`, Lambda Function URLs with `AuthType: NONE`, and a few more.
- The CloudFormation template uses least-privilege IAM by default, encrypts at rest with AES256 or KMS, and keeps Aurora and ElastiCache in private subnets.
- The dashboard auth model is documented in `README.md`; the Lambda Function URL uses `AuthType: AWS_IAM` and is reachable only via CloudFront OAC or via SigV4-signed requests from same-account IAM principals.
