#!/usr/bin/env bash
# Create the (optional) Secrets Manager secret holding a credential the special
# token endpoint itself requires. Only needed if your special endpoint is not
# public. The JSON key must match what lambda_function.py reads (e.g. "api_key").
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAME="${SPECIAL_ENDPOINT_SECRET_NAME:-quick-entra-bearer/special-endpoint}"

read -r -s -p "Special-endpoint api_key (input hidden): " API_KEY
echo

aws secretsmanager create-secret --region "$REGION" \
  --name "$NAME" \
  --description "Credential the email->bearer special endpoint requires" \
  --secret-string "{\"api_key\":\"$API_KEY\"}"

echo "Created secret: $NAME in $REGION"
echo "Set SPECIAL_ENDPOINT_SECRET_NAME=$NAME before cdk deploy."
