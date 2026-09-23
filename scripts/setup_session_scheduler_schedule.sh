#!/usr/bin/env bash
# Creates a rate-based EventBridge Scheduler schedule that invokes
# jarvis-session-scheduler every 15 minutes. It's a fixed-rate schedule (not
# tied to local time-of-day) so no timezone is needed, unlike the daily checkins.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env
set +a

REGION="${AWS_REGION:-us-east-2}"
FUNCTION_NAME="jarvis-session-scheduler"
ROLE_NAME="jarvis-scheduler-invoke-role"
SCHEDULE_NAME="jarvis-session-scheduler-tick"

LAMBDA_ARN=$(aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text)

# Reuse the invoke role created for the daily check-in schedules, but broaden its
# policy to cover any jarvis-* function so we don't need a role per schedule.
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "Creating $ROLE_NAME..."
  cat > /tmp/scheduler-trust.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"}, "Action": "sts:AssumeRole"}]
}
EOF
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document file:///tmp/scheduler-trust.json >/dev/null
  rm -f /tmp/scheduler-trust.json
  echo "Waiting for IAM role propagation..."
  sleep 10
fi

cat > /tmp/scheduler-invoke-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": "arn:aws:lambda:${REGION}:${AWS_ACCOUNT_ID}:function:jarvis-*"}]
}
EOF
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name invoke-jarvis-functions --policy-document file:///tmp/scheduler-invoke-policy.json
rm -f /tmp/scheduler-invoke-policy.json
ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)

TARGET="{\"Arn\":\"$LAMBDA_ARN\",\"RoleArn\":\"$ROLE_ARN\"}"

if aws scheduler get-schedule --name "$SCHEDULE_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "Updating schedule $SCHEDULE_NAME..."
  aws scheduler update-schedule \
    --name "$SCHEDULE_NAME" \
    --schedule-expression "rate(15 minutes)" \
    --flexible-time-window '{"Mode":"OFF"}' \
    --target "$TARGET" \
    --region "$REGION" >/dev/null
else
  echo "Creating schedule $SCHEDULE_NAME..."
  aws scheduler create-schedule \
    --name "$SCHEDULE_NAME" \
    --schedule-expression "rate(15 minutes)" \
    --flexible-time-window '{"Mode":"OFF"}' \
    --target "$TARGET" \
    --region "$REGION" >/dev/null
fi

echo "Done. Schedules:"
aws scheduler list-schedules --region "$REGION" --query "Schedules[?starts_with(Name, 'jarvis-')].{Name:Name,State:State}" --output table
