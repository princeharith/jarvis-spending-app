#!/usr/bin/env bash
# Registers the API Gateway URL as the Telegram bot's webhook.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env
set +a

if [ ! -f .api_gateway_url ]; then
  echo "Run scripts/setup_api_gateway.sh first." >&2
  exit 1
fi
URL=$(cat .api_gateway_url)

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" -d "url=${URL}"
echo
echo "Webhook info:"
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
echo
