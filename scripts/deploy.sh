#!/usr/bin/env bash
# One-shot deploy: build interceptor asset, then cdk deploy.
# Prereqs: `source .env` (from .env.example), AWS creds, CDK bootstrapped.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

: "${ENTRA_TENANT_ID:?set it (source .env)}"
: "${ENTRA_AUDIENCE:?set it}"
: "${THIRD_PARTY_BASE_URL:?set it}"
: "${SPECIAL_TOKEN_ENDPOINT:?set it}"

export CDK_DEFAULT_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export CDK_DEFAULT_REGION="${AWS_REGION:-us-east-1}"

echo "1/3 Vendoring interceptor deps..."
bash scripts/build_interceptor.sh

echo "2/3 Synthesizing..."
npx aws-cdk@latest synth --quiet

echo "3/3 Deploying quick-entra-bearer-mcp-dev..."
npx aws-cdk@latest deploy quick-entra-bearer-mcp-dev --require-approval never

echo "Done. Read the outputs above (GatewayMcpUrl, DemoThirdPartyApiUrl)."
