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

LOG_PURCHASE_TOOL = {
    "name": "log_purchase",
    "description": "Extract structured purchase details from a message describing something the user bought or spent money on.",
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

START_SESSION_TOOL = {
    "name": "start_going_out_session",
    "description": (
        "Call this when the user says they're heading out / going out for the night and "
        "want Jarvis to start tracking a going-out session with periodic check-ins "
        "(e.g. 'I'm going out', 'heading out tonight', 'going out with friends')."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

END_SESSION_TOOL = {
    "name": "end_going_out_session",
    "description": (
        "Call this when the user signals they're done going out / heading home for the "
        "night, ending the active going-out session (e.g. 'heading home', 'I'm done', "
        "'done for the night', 'home now')."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

ALL_TOOLS = [LOG_PURCHASE_TOOL, START_SESSION_TOOL, END_SESSION_TOOL]


def classify_message(raw_text: str):
    """Returns (tool_name, tool_input) for the best-matching intent, or (None, None)
    if the message doesn't clearly match logging a purchase or a session command."""
    response = anthropic_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=256,
        tools=ALL_TOOLS,
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": raw_text}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.name, block.input
    return None, None


def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )


def insert_transaction(conn, raw_text: str, parsed: dict, session_id=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transactions (raw_text, merchant, amount, category, confidence, session_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                raw_text,
                parsed["merchant"],
                parsed["amount"],
                parsed["category"],
                parsed["confidence"],
                session_id,
            ),
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


def get_active_session(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, started_at, running_total FROM sessions WHERE status = 'active' LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "started_at": row[1], "running_total": float(row[2])}


def start_session(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (status) VALUES ('active') RETURNING id"
        )
        (session_id,) = cur.fetchone()
    conn.commit()
    return session_id


def end_session(conn, session_id: int) -> float:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sessions SET status = 'ended' WHERE id = %s RETURNING running_total",
            (session_id,),
        )
        (running_total,) = cur.fetchone()
    conn.commit()
    return float(running_total)


def refresh_session_total(conn, session_id: int) -> float:
    # Recomputed from transactions each time rather than incremented, so it can't drift.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE session_id = %s",
            (session_id,),
        )
        (total,) = cur.fetchone()
        cur.execute("UPDATE sessions SET running_total = %s WHERE id = %s", (total, session_id))
    conn.commit()
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


def handle_start_session(conn, chat_id):
    existing = get_active_session(conn)
    if existing:
        send_telegram_message(
            chat_id,
            f"You're already out — tracking since {existing['started_at']:%-I:%M %p} "
            f"(${existing['running_total']:.2f} so far).",
        )
        return _ok("session already active")

    start_session(conn)
    send_telegram_message(
        chat_id,
        "Have fun! I'll check in roughly every 45 min — text me what you're spending as you go.",
    )
    return _ok("session started")


def handle_end_session(conn, chat_id):
    session = get_active_session(conn)
    if not session:
        send_telegram_message(chat_id, "No active going-out session to end.")
        return _ok("no active session")

    total = end_session(conn, session["id"])
    elapsed = None
    with conn.cursor() as cur:
        cur.execute("SELECT now() - started_at FROM sessions WHERE id = %s", (session["id"],))
        (elapsed,) = cur.fetchone()
    hours = elapsed.total_seconds() / 3600
    send_telegram_message(
        chat_id, f"Welcome home! Spent ${total:.2f} over {hours:.1f} hours tonight."
    )
    return _ok("session ended")


def handle_log_purchase(conn, chat_id, text, parsed):
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

    session = get_active_session(conn)
    session_id = session["id"] if session else None

    insert_transaction(conn, text, parsed, session_id=session_id)
    budget = get_budget(conn, parsed["category"])
    if budget:
        cycle_total = get_cycle_total(conn, parsed["category"], budget["cycle_start_day"])

    reply = f"Logged: {parsed['merchant']} — ${parsed['amount']:.2f} ({parsed['category']})"
    if budget:
        reply += f"\n{parsed['category']}: ${cycle_total:.2f}/${budget['monthly_limit']:.2f} this cycle"
        if cycle_total > budget["monthly_limit"]:
            reply += " — over budget"
    else:
        reply += "\n(no budget set for this category)"

    if session_id:
        session_total = refresh_session_total(conn, session_id)
        reply += f"\nGoing out total: ${session_total:.2f}"

    send_telegram_message(chat_id, reply)
    return _ok()


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

        tool_name, tool_input = classify_message(text)

        if tool_name is None:
            send_telegram_message(
                chat_id,
                "Didn't catch a purchase or a going-out update in that — "
                "try something like 'Chipotle $14' or 'I'm going out'.",
            )
            return _ok("unrecognized")

        conn = get_db_connection()
        try:
            if tool_name == "start_going_out_session":
                return handle_start_session(conn, chat_id)
            elif tool_name == "end_going_out_session":
                return handle_end_session(conn, chat_id)
            else:
                return handle_log_purchase(conn, chat_id, text, tool_input)
        finally:
            conn.close()

    except Exception:
        # Always return 200 so Telegram doesn't retry-storm us on a transient error;
        # the failure is still visible in CloudWatch.
        logger.exception("Failed to process Telegram update")
        return _ok("error logged")
