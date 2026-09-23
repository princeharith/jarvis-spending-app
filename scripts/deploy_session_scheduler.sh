#!/usr/bin/env bash
# Packages and deploys the jarvis-session-scheduler Lambda function.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env
set +a

REGION="${AWS_REGION:-us-east-2}"
FUNCTION_NAME="jarvis-session-scheduler"
ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/jarvis-lambda-role"
BUILD_DIR="$(mktemp -d)"
ZIP_PATH="/tmp/jarvis-session-scheduler.zip"

echo "Building package in $BUILD_DIR..."
python3 -m pip install -r lambda/session_scheduler/requirements.txt -t "$BUILD_DIR" --quiet --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12
cp lambda/session_scheduler/handler.py "$BUILD_DIR/"

(cd "$BUILD_DIR" && zip -r -q "$ZIP_PATH" .)
rm -rf "$BUILD_DIR"

ENV_VARS="Variables={TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN,TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID,ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY,DB_HOST=$DB_HOST,DB_PORT=$DB_PORT,DB_NAME=$DB_NAME,DB_USER=$DB_USER,DB_PASSWORD=$DB_PASSWORD}"

if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "Updating existing function code..."
  aws lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --zip-file "fileb://$ZIP_PATH" \
    --region "$REGION" >/dev/null
  aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$REGION"
  echo "Updating function configuration (env vars)..."
  aws lambda update-function-configuration \
    --function-name "$FUNCTION_NAME" \
    --environment "$ENV_VARS" \
    --timeout 60 \
    --region "$REGION" >/dev/null
else
  echo "Creating new function..."
  aws lambda create-function \
    --function-name "$FUNCTION_NAME" \
    --runtime python3.12 \
    --role "$ROLE_ARN" \
    --handler handler.lambda_handler \
    --zip-file "fileb://$ZIP_PATH" \
    --timeout 60 \
    --memory-size 256 \
    --environment "$ENV_VARS" \
    --region "$REGION" >/dev/null
fi

rm -f "$ZIP_PATH"
echo "Deployed $FUNCTION_NAME."
aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" --query 'Configuration.{FunctionArn:FunctionArn,LastModified:LastModified,State:State}' --output table
