import base64
import json
import logging
import os
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


def get_budget(conn, category: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT monthly_limit, cycle_start_day FROM budgets WHERE category = %s",
            (category,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"monthly_limit": float(row[0]), "cycle_start_day": row[1]}


def get_cycle_total(conn, category: str, cycle_start_day: int) -> float:
    # Cycle start: cycle_start_day of this month if we've reached it, else
    # cycle_start_day of last month. Computed in SQL against the DB's own clock
    # so it stays consistent regardless of Lambda's clock.
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH bounds AS (
                SELECT CASE
                    WHEN EXTRACT(DAY FROM now()) >= %(csd)s
                        THEN date_trunc('month', now()) + (%(csd)s - 1) * INTERVAL '1 day'
                    ELSE date_trunc('month', now() - INTERVAL '1 month') + (%(csd)s - 1) * INTERVAL '1 day'
                END AS cycle_start
            )
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions, bounds
            WHERE category = %(category)s AND logged_at >= bounds.cycle_start
            """,
            {"csd": cycle_start_day, "category": category},
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

        parsed = parse_purchase(text)

        if parsed["confidence"] < LOW_CONFIDENCE_THRESHOLD:
            # Don't guess-log an ambiguous parse — ask for clarification instead.
            # The user just resends a clearer message, which gets parsed fresh.
            reply = (
                f"Not sure I got that right — best guess: {parsed['merchant']}, "
                f"${parsed['amount']:.2f}, {parsed['category']} "
                f"({parsed['confidence']:.0%} confidence). "
                "Mind resending with the merchant and amount spelled out?"
            )
            send_telegram_message(chat_id, reply)
            return _ok("clarification requested")

        conn = get_db_connection()
        try:
            insert_transaction(conn, text, parsed)
            budget = get_budget(conn, parsed["category"])
            if budget:
                cycle_total = get_cycle_total(conn, parsed["category"], budget["cycle_start_day"])
        finally:
            conn.close()

        reply = f"Logged: {parsed['merchant']} — ${parsed['amount']:.2f} ({parsed['category']})"
        if budget:
            reply += f"\n{parsed['category']}: ${cycle_total:.2f}/${budget['monthly_limit']:.2f} this cycle"
            if cycle_total > budget["monthly_limit"]:
                reply += " — over budget"
        else:
            reply += "\n(no budget set for this category)"

        send_telegram_message(chat_id, reply)
        return _ok()

    except Exception:
        # Always return 200 so Telegram doesn't retry-storm us on a transient error;
        # the failure is still visible in CloudWatch.
        logger.exception("Failed to process Telegram update")
        return _ok("error logged")
