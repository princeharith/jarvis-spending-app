#!/usr/bin/env bash
# Creates an HTTP API Gateway in front of jarvis-webhook (idempotent-ish: reuses
# an existing API named jarvis-webhook-api if found), and grants it invoke permission
# via an explicit IAM role attached as integration credentials.
#
# NOTE: a bare Lambda resource-based policy (lambda add-permission for
# apigateway.amazonaws.com) was NOT honored in this AWS environment — API Gateway
# returned 500 "API_CONFIGURATION_ERROR" / integrationStatus 403 even with a
# correctly-scoped resource policy. The fix that worked: create an IAM role API
# Gateway assumes (jarvis-apigateway-invoke-role) with an inline lambda:InvokeFunction
# policy, and set it as the integration's --credentials-arn. This script does that
# directly rather than relying on add-permission.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env
set +a

REGION="${AWS_REGION:-us-east-2}"
FUNCTION_NAME="jarvis-webhook"
API_NAME="jarvis-webhook-api"
INVOKE_ROLE_NAME="jarvis-apigateway-invoke-role"

LAMBDA_ARN=$(aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text)

# --- IAM role API Gateway assumes to invoke the Lambda ---
if ! aws iam get-role --role-name "$INVOKE_ROLE_NAME" >/dev/null 2>&1; then
  echo "Creating $INVOKE_ROLE_NAME..."
  cat > /tmp/apigw-invoke-trust.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Principal": {"Service": "apigateway.amazonaws.com"}, "Action": "sts:AssumeRole"}]
}
EOF
  cat > /tmp/apigw-invoke-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": "$LAMBDA_ARN"}]
}
EOF
  aws iam create-role --role-name "$INVOKE_ROLE_NAME" --assume-role-policy-document file:///tmp/apigw-invoke-trust.json >/dev/null
  aws iam put-role-policy --role-name "$INVOKE_ROLE_NAME" --policy-name invoke-webhook --policy-document file:///tmp/apigw-invoke-policy.json
  rm -f /tmp/apigw-invoke-trust.json /tmp/apigw-invoke-policy.json
  echo "Waiting for IAM role propagation..."
  sleep 10
fi
INVOKE_ROLE_ARN=$(aws iam get-role --role-name "$INVOKE_ROLE_NAME" --query 'Role.Arn' --output text)

# --- HTTP API + integration + route ---
API_ID=$(aws apigatewayv2 get-apis --region "$REGION" --query "Items[?Name=='$API_NAME'].ApiId | [0]" --output text)

if [ "$API_ID" == "None" ] || [ -z "$API_ID" ]; then
  echo "Creating HTTP API..."
  API_ID=$(aws apigatewayv2 create-api --name "$API_NAME" --protocol-type HTTP --region "$REGION" --query 'ApiId' --output text)

  echo "Creating integration with explicit invoke credentials..."
  INTEGRATION_ID=$(aws apigatewayv2 create-integration \
    --api-id "$API_ID" \
    --integration-type AWS_PROXY \
    --integration-uri "$LAMBDA_ARN" \
    --payload-format-version 2.0 \
    --integration-method POST \
    --timeout-in-millis 29000 \
    --credentials-arn "$INVOKE_ROLE_ARN" \
    --region "$REGION" \
    --query 'IntegrationId' --output text)

  echo "Creating \$default route..."
  aws apigatewayv2 create-route --api-id "$API_ID" --route-key '$default' --target "integrations/$INTEGRATION_ID" --region "$REGION" >/dev/null

  echo "Creating \$default auto-deploy stage..."
  aws apigatewayv2 create-stage --api-id "$API_ID" --stage-name '$default' --auto-deploy --region "$REGION" >/dev/null
else
  echo "Reusing existing API $API_ID — ensuring integration uses invoke role..."
  INTEGRATION_ID=$(aws apigatewayv2 get-integrations --api-id "$API_ID" --region "$REGION" --query 'Items[0].IntegrationId' --output text)
  aws apigatewayv2 update-integration --api-id "$API_ID" --integration-id "$INTEGRATION_ID" --credentials-arn "$INVOKE_ROLE_ARN" --region "$REGION" >/dev/null
fi

INVOKE_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com/"
echo "Invoke URL: $INVOKE_URL"
echo "$INVOKE_URL" > .api_gateway_url
