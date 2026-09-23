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

CATEGORIES = ["food_drink", "groceries", "transport", "other"]
TRACKED_CATEGORIES = {"food_drink", "groceries", "transport"}
LOW_CONFIDENCE_THRESHOLD = 0.5

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)


def overspend_tail(over_by: float) -> str:
    # Blunt on purpose — only ever appended when actually over a budget/target.
    return f" That's ${over_by:.2f} over. Cut it the fuck out."


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
                "description": (
                    "Best-fit category. 'food_drink' covers meals, snacks, coffee, and "
                    "drinks/alcohol (including going-out spend). 'groceries' and 'transport' "
                    "are literal. Use 'other' for anything that isn't one of those three "
                    "(e.g. shopping, entertainment, subscriptions) — it still needs a merchant "
                    "and amount, it just won't be logged."
                ),
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
    "input_schema": {
        "type": "object",
        "properties": {
            "target_amount": {
                "type": "number",
                "description": (
                    "The dollar spending target for the night, ONLY if the user stated one "
                    "in this same message (e.g. 'going out, planning to spend 100' -> 100). "
                    "Omit this field entirely if no amount was mentioned."
                ),
            }
        },
    },
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

SET_TARGET_TOOL = {
    "name": "set_going_out_target",
    "description": (
        "The user was just asked how much they're planning to spend during their current "
        "going-out session, and this message is their answer — a bare dollar figure with no "
        "named merchant (e.g. '100', '$150', 'like 80 bucks'). Call this to record that target."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target_amount": {"type": "number", "description": "The dollar target stated."},
        },
        "required": ["target_amount"],
    },
}

SET_TRACKER_TOOL = {
    "name": "set_spending_tracker",
    "description": (
        "Call this when the user wants to set an arbitrary spending cap for a time window "
        "unrelated to a going-out session or the monthly category budgets, e.g. "
        "'I want to spend $200 in the next 8 hours', 'cap it at $50 for the next 3 hours'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target_amount": {"type": "number", "description": "The dollar cap stated."},
            "duration_hours": {
                "type": "number",
                "description": "How many hours the cap applies for, as stated or implied.",
            },
        },
        "required": ["target_amount", "duration_hours"],
    },
}


def classify_message(raw_text: str, awaiting_target: bool):
    """Returns (tool_name, tool_input) for the best-matching intent, or (None, None)
    if the message doesn't clearly match any known intent."""
    tools = [LOG_PURCHASE_TOOL, START_SESSION_TOOL, END_SESSION_TOOL, SET_TRACKER_TOOL]
    if awaiting_target:
        tools.append(SET_TARGET_TOOL)

    response = anthropic_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=256,
        tools=tools,
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
            "SELECT cycle_limit, cycle_anchor, cycle_length_days FROM budgets WHERE category = %s",
            (category,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"cycle_limit": float(row[0]), "cycle_anchor": row[1], "cycle_length_days": row[2]}


def get_cycle_total(conn, category: str, cycle_anchor, cycle_length_days: int) -> float:
    # Cycle start: cycle_anchor plus however many whole cycle_length_days blocks
    # have elapsed since then. Computed in SQL against the DB's own clock so it
    # stays consistent regardless of Lambda's clock.
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH bounds AS (
                SELECT (
                    %(anchor)s::date + (
                        ((CURRENT_DATE - %(anchor)s::date) / %(length)s::int) * %(length)s::int
                    ) * INTERVAL '1 day'
                )::timestamptz AS cycle_start
            )
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions, bounds
            WHERE category = %(category)s AND logged_at >= bounds.cycle_start
            """,
            {"anchor": cycle_anchor, "length": cycle_length_days, "category": category},
        )
        (total,) = cur.fetchone()
    return float(total)


def get_active_session(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, started_at, status, target_amount, running_total
            FROM sessions WHERE status IN ('awaiting_target', 'active') LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "started_at": row[1],
        "status": row[2],
        "target_amount": float(row[3]) if row[3] is not None else None,
        "running_total": float(row[4]),
    }


def start_session(conn, target_amount=None) -> int:
    status = "active" if target_amount is not None else "awaiting_target"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (status, target_amount) VALUES (%s, %s) RETURNING id",
            (status, target_amount),
        )
        (session_id,) = cur.fetchone()
    conn.commit()
    return session_id


def set_session_target(conn, session_id: int, target_amount: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sessions SET target_amount = %s, status = 'active' WHERE id = %s",
            (target_amount, session_id),
        )
    conn.commit()


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


def create_tracker(conn, target_amount: float, duration_hours: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trackers (target_amount, ends_at)
            VALUES (%s, now() + (%s || ' hours')::interval)
            """,
            (target_amount, duration_hours),
        )
    conn.commit()


def get_active_trackers(conn):
    # Live-computed against logged_at falling in the window — trackers don't tag
    # transactions, so they can overlap each other or a going-out session freely.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.target_amount, t.ends_at,
                   COALESCE((
                       SELECT SUM(amount) FROM transactions
                       WHERE logged_at >= t.starts_at AND logged_at <= now()
                   ), 0) AS spent
            FROM trackers t
            WHERE t.status = 'active' AND t.ends_at > now()
            """
        )
        rows = cur.fetchall()
    return [
        {"id": r[0], "target_amount": float(r[1]), "ends_at": r[2], "spent": float(r[3])}
        for r in rows
    ]


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


def handle_start_session(conn, chat_id, target_amount=None):
    existing = get_active_session(conn)
    if existing:
        send_telegram_message(
            chat_id,
            f"Already tracking a going-out session, started {existing['started_at']:%-I:%M %p} "
            f"(${existing['running_total']:.2f} so far).",
        )
        return _ok("session already active")

    start_session(conn, target_amount=target_amount)
    if target_amount is not None:
        send_telegram_message(
            chat_id,
            f"Going-out session started. Target: ${target_amount:.2f}. "
            "I'll check in roughly every 45 minutes.",
        )
    else:
        send_telegram_message(chat_id, "Going-out session started. How much are you planning to spend tonight?")
    return _ok("session started")


def handle_set_target(conn, chat_id, session, target_amount):
    set_session_target(conn, session["id"], target_amount)
    send_telegram_message(
        chat_id, f"Target set: ${target_amount:.2f}. I'll flag it if you're pacing over that."
    )
    return _ok("target set")


def handle_end_session(conn, chat_id):
    session = get_active_session(conn)
    if not session:
        send_telegram_message(chat_id, "No active going-out session to end.")
        return _ok("no active session")

    total = end_session(conn, session["id"])
    with conn.cursor() as cur:
        cur.execute("SELECT now() - started_at FROM sessions WHERE id = %s", (session["id"],))
        (elapsed,) = cur.fetchone()
    hours = elapsed.total_seconds() / 3600

    reply = f"Session ended. Spent ${total:.2f} over {hours:.1f} hours."
    if session["target_amount"] is not None:
        if total > session["target_amount"]:
            reply += overspend_tail(total - session["target_amount"])
        else:
            reply += f" Target was ${session['target_amount']:.2f} — stayed under."

    send_telegram_message(chat_id, reply)
    return _ok("session ended")


def handle_set_tracker(conn, chat_id, target_amount, duration_hours):
    create_tracker(conn, target_amount, duration_hours)
    send_telegram_message(
        chat_id, f"Tracking ${target_amount:.2f} over the next {duration_hours:g} hours."
    )
    return _ok("tracker set")


def handle_log_purchase(conn, chat_id, text, parsed):
    if parsed["confidence"] < LOW_CONFIDENCE_THRESHOLD:
        # Don't guess-log an ambiguous parse — ask for clarification instead.
        # The user just resends a clearer message, which gets parsed fresh.
        reply = (
            f"Not sure that parsed right — best guess: {parsed['merchant']}, "
            f"${parsed['amount']:.2f}, {parsed['category']} "
            f"({parsed['confidence']:.0%} confidence). Resend with the merchant and amount spelled out."
        )
        send_telegram_message(chat_id, reply)
        return _ok("clarification requested")

    if parsed["category"] not in TRACKED_CATEGORIES:
        send_telegram_message(
            chat_id,
            f"Not tracking {parsed['category']} purchases right now "
            f"(just food_drink, groceries, and transport).",
        )
        return _ok("category not tracked")

    session = get_active_session(conn)
    session_id = session["id"] if session else None

    insert_transaction(conn, text, parsed, session_id=session_id)
    budget = get_budget(conn, parsed["category"])
    if budget:
        cycle_total = get_cycle_total(
            conn, parsed["category"], budget["cycle_anchor"], budget["cycle_length_days"]
        )

    reply = f"Logged: {parsed['merchant']} — ${parsed['amount']:.2f} ({parsed['category']})"
    if budget:
        reply += f"\n{parsed['category']}: ${cycle_total:.2f}/${budget['cycle_limit']:.2f} this cycle."
        if cycle_total > budget["cycle_limit"]:
            reply += overspend_tail(cycle_total - budget["cycle_limit"])
    else:
        reply += "\n(no budget set for this category)"

    if session_id:
        session_total = refresh_session_total(conn, session_id)
        reply += f"\nGoing out total: ${session_total:.2f}"
        if session["target_amount"] is not None and session_total > session["target_amount"]:
            reply += overspend_tail(session_total - session["target_amount"])

    for tracker in get_active_trackers(conn):
        reply += f"\nTracker: ${tracker['spent']:.2f}/${tracker['target_amount']:.2f}"
        if tracker["spent"] > tracker["target_amount"]:
            reply += overspend_tail(tracker["spent"] - tracker["target_amount"])

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

        conn = get_db_connection()
        try:
            session = get_active_session(conn)
            awaiting_target = bool(session and session["status"] == "awaiting_target")

            tool_name, tool_input = classify_message(text, awaiting_target)

            if tool_name is None:
                send_telegram_message(
                    chat_id,
                    "Didn't recognize that as a purchase, a going-out update, or a tracker. "
                    "Try 'Chipotle $14', 'I'm going out', or 'spend $200 in the next 8 hours'.",
                )
                return _ok("unrecognized")

            if tool_name == "start_going_out_session":
                return handle_start_session(conn, chat_id, tool_input.get("target_amount"))
            elif tool_name == "set_going_out_target":
                return handle_set_target(conn, chat_id, session, tool_input["target_amount"])
            elif tool_name == "end_going_out_session":
                return handle_end_session(conn, chat_id)
            elif tool_name == "set_spending_tracker":
                return handle_set_tracker(
                    conn, chat_id, tool_input["target_amount"], tool_input["duration_hours"]
                )
            else:
                return handle_log_purchase(conn, chat_id, text, tool_input)
        finally:
            conn.close()

    except Exception:
        # Always return 200 so Telegram doesn't retry-storm us on a transient error;
        # the failure is still visible in CloudWatch.
        logger.exception("Failed to process Telegram update")
        return _ok("error logged")
