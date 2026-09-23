import base64
import json
import logging
import os
from datetime import datetime, timezone
from urllib import request as urlrequest

import psycopg2
from anthropic import Anthropic

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "jarvis")
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

CATEGORIES = ["food", "coffee", "groceries", "transport", "entertainment", "shopping", "other"]
LOW_CONFIDENCE_THRESHOLD = 0.5

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

PARSE_TOOL = {
    "name": "log_purchase",
    "description": "Extract structured purchase details from a short free-text spending message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "merchant": {
                "type": "string",
                "description": "The merchant or vendor name, or a short description if no named merchant (e.g. 'coffee').",
            },
            "amount": {
                "type": "number",
                "description": "The dollar amount spent, as a plain number (e.g. 14.00).",
            },
            "category": {
                "type": "string",
                "enum": CATEGORIES,
                "description": "Best-fit spending category.",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in this parse from 0.0 to 1.0. Lower if the amount or merchant is ambiguous.",
            },
        },
        "required": ["merchant", "amount", "category", "confidence"],
    },
}


def parse_purchase(raw_text: str) -> dict:
    response = anthropic_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=256,
        tools=[PARSE_TOOL],
        tool_choice={"type": "tool", "name": "log_purchase"},
        messages=[
            {
                "role": "user",
                "content": (
                    "Extract the purchase details from this message and call log_purchase. "
                    f"Message: {raw_text!r}"
                ),
            }
        ],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "log_purchase":
            return block.input
    raise ValueError("Claude did not return a log_purchase tool call")


def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )


def insert_transaction(conn, raw_text: str, parsed: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transactions (raw_text, merchant, amount, category, confidence)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (raw_text, parsed["merchant"], parsed["amount"], parsed["category"], parsed["confidence"]),
        )
    conn.commit()


def get_month_total(conn, category: str) -> float:
    # Running total is scoped to the current calendar month across all categories,
    # not just the category just logged — more useful for an at-a-glance reply than a
    # single-category total. Budget-per-category totals land in Phase 2.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE date_trunc('month', logged_at) = date_trunc('month', now())
            """
        )
        (total,) = cur.fetchone()
    return float(total)


def send_telegram_message(chat_id, text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urlrequest.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urlrequest.urlopen(req, timeout=10) as resp:
        resp.read()


def _ok(body: str = "ok"):
    return {"statusCode": 200, "body": body}


def lambda_handler(event, context):
    try:
        raw_body = event.get("body", "")
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")

        update = json.loads(raw_body) if raw_body else {}
        message = update.get("message")
        if not message:
            return _ok("no message")

        chat_id = message.get("chat", {}).get("id")
        text = message.get("text")
        if not text or chat_id is None:
            # Non-text message (photo, sticker, etc.) or malformed update — nothing to log.
            return _ok("ignored")

        # TODO(Phase 2): on low-confidence parses, reply asking the user to clarify
        # (e.g. "did you mean $14 at Chipotle?") instead of logging a guess.
        parsed = parse_purchase(text)

        conn = get_db_connection()
        try:
            insert_transaction(conn, text, parsed)
            month_total = get_month_total(conn, parsed["category"])
        finally:
            conn.close()

        reply = (
            f"Logged: {parsed['merchant']} — ${parsed['amount']:.2f} ({parsed['category']})\n"
            f"Month total: ${month_total:.2f}"
        )
        if parsed["confidence"] < LOW_CONFIDENCE_THRESHOLD:
            reply += "\n(low confidence parse — reply to correct it if that's wrong)"

        send_telegram_message(chat_id, reply)
        return _ok()

    except Exception:
        # Always return 200 so Telegram doesn't retry-storm us on a transient error;
        # the failure is still visible in CloudWatch.
        logger.exception("Failed to process Telegram update")
        return _ok("error logged")
