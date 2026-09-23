#!/usr/bin/env bash
# Creates two EventBridge Scheduler schedules that invoke jarvis-daily-checkin:
# lunch check at 3:00pm America/New_York, dinner check at 10:00pm America/New_York.
# Uses EventBridge Scheduler (not classic EventBridge rules) so the IANA timezone
# handles DST automatically instead of a fixed UTC cron offset.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env
set +a

REGION="${AWS_REGION:-us-east-2}"
FUNCTION_NAME="jarvis-daily-checkin"
ROLE_NAME="jarvis-scheduler-invoke-role"
TIMEZONE="America/New_York"

LAMBDA_ARN=$(aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text)

# --- IAM role EventBridge Scheduler assumes to invoke the Lambda ---
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "Creating $ROLE_NAME..."
  cat > /tmp/scheduler-trust.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"}, "Action": "sts:AssumeRole"}]
}
EOF
  cat > /tmp/scheduler-invoke-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": "$LAMBDA_ARN"}]
}
EOF
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document file:///tmp/scheduler-trust.json >/dev/null
  aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name invoke-daily-checkin --policy-document file:///tmp/scheduler-invoke-policy.json
  rm -f /tmp/scheduler-trust.json /tmp/scheduler-invoke-policy.json
  echo "Waiting for IAM role propagation..."
  sleep 10
fi
ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)

create_or_update_schedule() {
  local name="$1" cron="$2" meal="$3"
  local input="{\"meal\":\"$meal\"}"

  if aws scheduler get-schedule --name "$name" --region "$REGION" >/dev/null 2>&1; then
    echo "Updating schedule $name..."
    aws scheduler update-schedule \
      --name "$name" \
      --schedule-expression "cron($cron)" \
      --schedule-expression-timezone "$TIMEZONE" \
      --flexible-time-window '{"Mode":"OFF"}' \
      --target "{\"Arn\":\"$LAMBDA_ARN\",\"RoleArn\":\"$ROLE_ARN\",\"Input\":\"$(echo "$input" | sed 's/"/\\"/g')\"}" \
      --region "$REGION" >/dev/null
  else
    echo "Creating schedule $name..."
    aws scheduler create-schedule \
      --name "$name" \
      --schedule-expression "cron($cron)" \
      --schedule-expression-timezone "$TIMEZONE" \
      --flexible-time-window '{"Mode":"OFF"}' \
      --target "{\"Arn\":\"$LAMBDA_ARN\",\"RoleArn\":\"$ROLE_ARN\",\"Input\":\"$(echo "$input" | sed 's/"/\\"/g')\"}" \
      --region "$REGION" >/dev/null
  fi
}

# cron(minute hour day-of-month month day-of-week year)
create_or_update_schedule "jarvis-lunch-checkin" "0 15 * * ? *" "lunch"
create_or_update_schedule "jarvis-dinner-checkin" "0 22 * * ? *" "dinner"

echo "Done. Schedules:"
aws scheduler list-schedules --region "$REGION" --query "Schedules[?starts_with(Name, 'jarvis-')].{Name:Name,State:State}" --output table
