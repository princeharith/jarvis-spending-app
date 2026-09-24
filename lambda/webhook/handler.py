import base64
import json
import logging
import os
import random
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

CATEGORIES = ["food_drink", "groceries", "transport", "other"]
TRACKED_CATEGORIES = {"food_drink", "groceries", "transport"}
LOW_CONFIDENCE_THRESHOLD = 0.5

# "food_drink" is the stable internal category slug (DB storage, tool schema),
# but reads better in a message as "food/drink".
DISPLAY_CATEGORY = {"food_drink": "food/drink"}


def display_category(category: str) -> str:
    return DISPLAY_CATEGORY.get(category, category)

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)


def overspend_tail(over_by: float) -> str:
    # Blunt on purpose — only ever appended when actually over a budget/target.
    return f" That's ${over_by:.2f} over. Cut it the fuck out."


# Openers for routine setup confirmations (setting a budget/tracker/session/window):
# a mix of "talking to a friend" and Iron Man JARVIS-style AI butler lines. Kept
# separate from overspend_tail, which stays serious. Not used for purchase logs,
# warnings, or query results.
CASUAL_OPENERS = [
    "Sure bro",
    "You got it brotha",
    "Say less",
    "Bet, locked in",
    "On it dawg",
    "You got it chief",
    "Done deal",
    "Bet",
    "Initiating budget protocol, sir",
    "Initiating the save_money directive",
    "Budget protocol engaged, sir",
    "Save_money directive online",
]


def casual_opener() -> str:
    return random.choice(CASUAL_OPENERS)


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


GET_CYCLE_SUMMARY_TOOL = {
    "name": "get_cycle_summary",
    "description": (
        "Call this when the user asks for a status/progress update on the PAYCHECK PERIOD "
        "specifically — the standing semimonthly (1st-15th, 16th-end of month) budget, e.g. "
        "'how am I doing this paycheck period', 'how's my paycheck period spending'. NOT for "
        "an ad-hoc 'budgeting window'/'budgeting cycle' the user separately started with its "
        "own per-category limits and duration — use get_budget_window_status for that, "
        "especially for vaguer phrasing like 'how am I doing in my current cycle'."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

GET_CATEGORY_STATUS_TOOL = {
    "name": "get_category_status",
    "description": (
        "Call this when the user asks how much they've spent or have left in ONE specific "
        "tracked category for the paycheck period, e.g. 'how much do I have left in "
        "transport', 'how much have I spent on food and drink', 'what's my groceries budget "
        "looking like'. If a budgeting window is active and the user's phrasing suggests they "
        "mean that instead (e.g. mentions 'this week', 'the window'), prefer "
        "get_budget_window_status."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": sorted(TRACKED_CATEGORIES),
                "description": "Which tracked category they're asking about.",
            },
        },
        "required": ["category"],
    },
}

def build_start_budget_window_tool(window_pending: bool) -> dict:
    pending_hint = (
        (
            "A budgeting window is currently PENDING (already started, still missing its "
            "duration and/or category limits) — Jarvis just asked for those details. ALSO call "
            "this for a short follow-up reply that answers that question, even a bare one like "
            "'food and drink 125' or 'groceries 40, transport 20' or just 'a week' or 'till "
            "October 1st' with no other context. "
        )
        if window_pending
        else ""
    )
    return {
        "name": "start_budget_window",
        "description": (
            "Call this when the user wants to start a new ad-hoc 'budgeting window' or "
            "'budgeting cycle' with per-category spending limits for some duration (a week, a "
            "weekend, 3 days, a specific date, etc.) — separate from the standing paycheck "
            "period. E.g. 'I want to start a new budgeting cycle, food and drink $100, "
            "groceries $50, transport $30 for the next week'. Also call this for a vaguer "
            "intent to save/budget with no numbers yet, e.g. 'I need to save money for a "
            "while' — just omit whatever fields weren't given, Jarvis will ask for the rest. "
            f"{pending_hint}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "duration_hours": {
                    "type": "number",
                    "description": (
                        "How many hours the window lasts, as stated or implied (e.g. 'a week' "
                        "-> 168, 'the weekend' -> 60, '3 days' -> 72). If an explicit end date "
                        "is given instead (e.g. 'till October 1st'), compute the hours between "
                        "today and that date. Omit if no duration or end date was given."
                    ),
                },
                "food_drink_limit": {"type": "number", "description": "Window limit for food_drink, if stated."},
                "groceries_limit": {"type": "number", "description": "Window limit for groceries, if stated."},
                "transport_limit": {"type": "number", "description": "Window limit for transport, if stated."},
            },
        },
    }

GET_BUDGET_WINDOW_STATUS_TOOL = {
    "name": "get_budget_window_status",
    "description": (
        "Call this when the user asks how they're doing in their currently active ad-hoc "
        "budgeting window/cycle (started via start_budget_window), e.g. 'how am I doing in "
        "my current cycle', 'how's my budgeting window going', 'where do I stand this week'. "
        "NOT for the standing paycheck period — use get_cycle_summary for that."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

CANCEL_BUDGET_WINDOW_TOOL = {
    "name": "cancel_budget_window",
    "description": (
        "Call this when the user wants to cancel/end/stop their currently active or pending "
        "ad-hoc budgeting window before its duration naturally runs out, e.g. 'cancel my "
        "budgeting window', 'end this budgeting cycle', 'stop the window', 'scrap that "
        "budget'. NOT for the standing paycheck period, which can't be cancelled this way."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

SET_PAYCHECK_LIMIT_TOOL = {
    "name": "set_paycheck_period_limit",
    "description": (
        "Call this when the user wants to change or reset a category's dollar limit for the "
        "standing PAYCHECK PERIOD (the semimonthly 1st-15th / 16th-end-of-month budget) — "
        "e.g. they made a mistake setting it up or just want a different number. NOT for a "
        "budgeting window or tracker. E.g. 'change my food_drink paycheck limit to 300', "
        "'reset my transport budget to 100', 'set groceries limit to 250 for the paycheck "
        "period'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": sorted(TRACKED_CATEGORIES),
                "description": "Which tracked category's paycheck-period limit to change.",
            },
            "new_limit": {"type": "number", "description": "The new dollar limit."},
        },
        "required": ["category", "new_limit"],
    },
}


CORRECT_LAST_TOOL = {
    "name": "correct_last_purchase",
    "description": (
        "Call this when the user is correcting/amending the MOST RECENTLY logged purchase "
        "rather than logging a new one, e.g. 'actually that was $25 not $250', 'correction, "
        "it was $16', 'wrong amount, should be $12', 'that was actually Chipotle not Subway'. "
        "Only include the field(s) actually being corrected; leave the rest out."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "amount": {"type": "number", "description": "Corrected dollar amount, if being corrected."},
            "merchant": {"type": "string", "description": "Corrected merchant, if being corrected."},
            "category": {
                "type": "string",
                "enum": sorted(TRACKED_CATEGORIES),
                "description": "Corrected category, if being corrected.",
            },
        },
    },
}

UNDO_LAST_TOOL = {
    "name": "undo_last_purchase",
    "description": (
        "Call this when the user wants to remove the most recently logged purchase entirely "
        "(not correct it, delete it), e.g. 'undo', 'undo last', 'delete that', 'remove the "
        "last one', 'that shouldn't have been logged'."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

ACKNOWLEDGE_NO_SPEND_TOOL = {
    "name": "acknowledge_no_spend",
    "description": (
        "Call this when the user is simply telling Jarvis they haven't spent/bought/eaten "
        "anything — not logging a purchase, not asking a question, not starting/ending "
        "anything. Often a reply to a check-in nudge. E.g. 'I haven't eaten anything', "
        "'nope, nothing today', 'no purchases', 'haven't gotten groceries yet'."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

GREET_TOOL = {
    "name": "greet",
    "description": (
        "Call this when the user is just greeting Jarvis or making small talk with no other "
        "intent, e.g. 'hi', 'hey Jarvis', 'what's up', 'yo', 'good morning'. Not for anything "
        "else, even if a greeting is combined with another intent (log/start/end/query first)."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

ADD_WINDOW_LIMIT_TOOL = {
    "name": "add_budget_window_category_limit",
    "description": (
        "Call this ONLY when the user is explicitly adding/setting a spending LIMIT or CAP "
        "for a category on their CURRENTLY ACTIVE budgeting window — using words like "
        "'limit', 'cap', 'budget', or 'window' — not when they're logging an actual purchase. "
        "E.g. 'add a groceries limit of 40 to the window', 'let's cap groceries at 40 too'. "
        "A bare 'groceries 40' or 'spent 40 on groceries' with no limit/cap/window language "
        "is a purchase — use log_purchase for that instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": sorted(TRACKED_CATEGORIES)},
            "limit_amount": {"type": "number"},
        },
        "required": ["category", "limit_amount"],
    },
}


def classify_message(raw_text: str, awaiting_target: bool, window_active: bool, window_pending: bool):
    """Returns (tool_name, tool_input) for the best-matching intent, or (None, None)
    if the message doesn't clearly match any known intent."""
    tools = [
        LOG_PURCHASE_TOOL,
        START_SESSION_TOOL,
        END_SESSION_TOOL,
        SET_TRACKER_TOOL,
        GET_CYCLE_SUMMARY_TOOL,
        GET_CATEGORY_STATUS_TOOL,
        CORRECT_LAST_TOOL,
        UNDO_LAST_TOOL,
        build_start_budget_window_tool(window_pending),
        GET_BUDGET_WINDOW_STATUS_TOOL,
        CANCEL_BUDGET_WINDOW_TOOL,
        SET_PAYCHECK_LIMIT_TOOL,
        ACKNOWLEDGE_NO_SPEND_TOOL,
        GREET_TOOL,
    ]
    if awaiting_target:
        tools.append(SET_TARGET_TOOL)
    if window_active:
        tools.append(ADD_WINDOW_LIMIT_TOOL)

    response = anthropic_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=256,
        system=f"Today's date is {datetime.now(timezone.utc):%Y-%m-%d}.",
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


def get_last_transaction(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, merchant, amount, category, session_id
            FROM transactions ORDER BY logged_at DESC, id DESC LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "merchant": row[1],
        "amount": float(row[2]),
        "category": row[3],
        "session_id": row[4],
    }


def apply_correction(conn, tx_id: int, merchant=None, amount=None, category=None) -> None:
    fields, params = [], []
    if merchant is not None:
        fields.append("merchant = %s")
        params.append(merchant)
    if amount is not None:
        fields.append("amount = %s")
        params.append(amount)
    if category is not None:
        fields.append("category = %s")
        params.append(category)
    if not fields:
        return
    params.append(tx_id)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE transactions SET {', '.join(fields)} WHERE id = %s", params)
    conn.commit()


def delete_transaction(conn, tx_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
    conn.commit()


def get_budget(conn, category: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cycle_limit, cycle_type, cycle_anchor, cycle_length_days FROM budgets WHERE category = %s",
            (category,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "cycle_limit": float(row[0]),
        "cycle_type": row[1],
        "cycle_anchor": row[2],
        "cycle_length_days": row[3],
    }


def get_cycle_total(conn, category: str, budget: dict) -> float:
    # Cycle start, computed in SQL against the DB's own clock so it stays
    # consistent regardless of Lambda's clock:
    # - 'semimonthly': paycheck-aligned, the 1st or the 16th of the month
    # - 'fixed_days': cycle_anchor plus however many whole cycle_length_days
    #   blocks have elapsed since then
    if budget["cycle_type"] == "semimonthly":
        sql = """
            WITH bounds AS (
                SELECT CASE
                    WHEN EXTRACT(DAY FROM CURRENT_DATE) <= 15
                        THEN date_trunc('month', CURRENT_DATE)
                    ELSE date_trunc('month', CURRENT_DATE) + INTERVAL '15 days'
                END AS cycle_start
            )
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions, bounds
            WHERE category = %(category)s AND logged_at >= bounds.cycle_start
        """
        params = {"category": category}
    else:
        sql = """
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
        """
        params = {
            "anchor": budget["cycle_anchor"],
            "length": budget["cycle_length_days"],
            "category": category,
        }

    with conn.cursor() as cur:
        cur.execute(sql, params)
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


def create_pending_window(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO budget_windows (status) VALUES ('pending') RETURNING id")
        (window_id,) = cur.fetchone()
    conn.commit()
    return window_id


def upsert_window_limit(conn, window_id: int, category: str, limit_amount: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO budget_window_limits (window_id, category, limit_amount)
            VALUES (%s, %s, %s)
            ON CONFLICT (window_id, category) DO UPDATE SET limit_amount = EXCLUDED.limit_amount
            """,
            (window_id, category, limit_amount),
        )
    conn.commit()


def set_window_duration(conn, window_id: int, duration_hours: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE budget_windows SET ends_at = starts_at + (%s || ' hours')::interval WHERE id = %s",
            (duration_hours, window_id),
        )
    conn.commit()


def finalize_window(conn, window_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE budget_windows SET status = 'active' WHERE id = %s", (window_id,))
    conn.commit()


def get_open_budget_window(conn):
    # 'pending' (setup in progress) or 'active' (fully running) — the one-open-
    # window-at-a-time state, mirroring sessions' awaiting_target/active pair.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, starts_at, ends_at FROM budget_windows WHERE status IN ('pending', 'active') LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "status": row[1], "starts_at": row[2], "ends_at": row[3]}


def get_active_budget_window(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, starts_at, ends_at FROM budget_windows WHERE status = 'active' LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "starts_at": row[1], "ends_at": row[2]}


def get_budget_window_limits(conn, window_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT category, limit_amount FROM budget_window_limits WHERE window_id = %s ORDER BY category",
            (window_id,),
        )
        rows = cur.fetchall()
    return {r[0]: float(r[1]) for r in rows}


def get_window_category_spent(conn, category: str, starts_at) -> float:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE category = %s AND logged_at >= %s",
            (category, starts_at),
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
            f"{casual_opener()}. Target's ${target_amount:.2f}, "
            "I'll check in roughly every 45 minutes.",
        )
    else:
        send_telegram_message(
            chat_id, f"{casual_opener()}, going-out mode on. How much are you planning to spend tonight?"
        )
    return _ok("session started")


def handle_set_target(conn, chat_id, session, target_amount):
    set_session_target(conn, session["id"], target_amount)
    send_telegram_message(
        chat_id,
        f"{casual_opener()}, target's ${target_amount:.2f}. I'll flag it if you're pacing over that.",
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
            reply += f" Target was ${session['target_amount']:.2f}, stayed under."

    send_telegram_message(chat_id, reply)
    return _ok("session ended")


def handle_set_tracker(conn, chat_id, target_amount, duration_hours):
    create_tracker(conn, target_amount, duration_hours)
    send_telegram_message(
        chat_id,
        f"{casual_opener()}, tracking ${target_amount:.2f} over the next {duration_hours:g} hours.",
    )
    return _ok("tracker set")


def window_category_line(conn, window, category: str, limit_amount: float) -> str:
    spent = get_window_category_spent(conn, category, window["starts_at"])
    remaining = limit_amount - spent
    line = f"{display_category(category)}: ${spent:.2f}/${limit_amount:.2f}"
    if remaining >= 0:
        line += f" (${remaining:.2f} left)"
    else:
        line += overspend_tail(-remaining)
    return line


def handle_start_budget_window(conn, chat_id, tool_input):
    open_window = get_open_budget_window(conn)
    if open_window and open_window["status"] == "active":
        send_telegram_message(
            chat_id,
            f"Already have a budgeting window running, ends {open_window['ends_at']:%a %-I:%M %p}. "
            "End it first (just let it run out) before starting a new one.",
        )
        return _ok("window already active")

    # Either starting fresh, or filling in details on an already-pending window.
    window_id = open_window["id"] if open_window else create_pending_window(conn)

    new_limits = {
        category: tool_input[f"{category}_limit"]
        for category in TRACKED_CATEGORIES
        if tool_input.get(f"{category}_limit") is not None
    }
    for category, limit_amount in new_limits.items():
        upsert_window_limit(conn, window_id, category, limit_amount)

    duration_hours = tool_input.get("duration_hours")
    if duration_hours is not None:
        set_window_duration(conn, window_id, duration_hours)

    current_limits = get_budget_window_limits(conn, window_id)
    with conn.cursor() as cur:
        cur.execute("SELECT ends_at FROM budget_windows WHERE id = %s", (window_id,))
        (ends_at,) = cur.fetchone()

    if ends_at is not None and current_limits:
        finalize_window(conn, window_id)
        with conn.cursor() as cur:
            cur.execute("SELECT starts_at FROM budget_windows WHERE id = %s", (window_id,))
            (starts_at,) = cur.fetchone()
        total_hours = (ends_at - starts_at).total_seconds() / 3600

        lines = [
            f"{display_category(category)}: ${amount:.2f}"
            for category, amount in sorted(current_limits.items())
        ]
        reply = f"{casual_opener()}. Budgeting window locked in for {total_hours:g} hours:\n" + "\n".join(lines)

        missing = sorted(TRACKED_CATEGORIES - current_limits.keys())
        if missing:
            missing_display = ', '.join(display_category(c) for c in missing)
            reply += f"\nNo limit set for {missing_display}. Let me know if you want to add one (fine to skip too)."

        send_telegram_message(chat_id, reply)
        return _ok("budget window started")

    # Still missing something — ask only for what's actually missing.
    still_need = []
    if ends_at is None:
        still_need.append("how long it should run (or an end date)")
    if not current_limits:
        still_need.append("at least one category limit (food/drink, groceries, transport)")

    got = []
    if new_limits:
        got.append(", ".join(f"{display_category(c)} ${a:.2f}" for c, a in sorted(new_limits.items())))
    if duration_hours is not None:
        got.append(f"{duration_hours:g} hours")

    reply = f"Got it{': ' + '; '.join(got) if got else ''}. Still need {' and '.join(still_need)}."
    send_telegram_message(chat_id, reply)
    return _ok("budget window pending")


def handle_add_window_limit(conn, chat_id, category, limit_amount):
    window = get_active_budget_window(conn)
    if not window:
        send_telegram_message(chat_id, "No active budgeting window to add a limit to.")
        return _ok("no active window")

    upsert_window_limit(conn, window["id"], category, limit_amount)

    send_telegram_message(
        chat_id, f"{casual_opener()}, added {display_category(category)} limit ${limit_amount:.2f} for this window."
    )
    return _ok("window limit added")


def handle_acknowledge_no_spend(conn, chat_id):
    send_telegram_message(chat_id, "Noted.")
    return _ok("acknowledged")


GREETINGS = [
    "Yo",
    "Yooo",
    "Hey, what's up",
    "Sup",
    "What's good",
    "Hey",
    "What's goodie fam",
    "Ayo",
    "Wassup",
]


def handle_greet(conn, chat_id):
    send_telegram_message(chat_id, random.choice(GREETINGS))
    return _ok("greeted")


def handle_get_budget_window_status(conn, chat_id):
    window = get_active_budget_window(conn)
    if not window:
        send_telegram_message(chat_id, "No active budgeting window right now.")
        return _ok("no active window")

    limits = get_budget_window_limits(conn, window["id"])
    lines = [
        window_category_line(conn, window, category, limit_amount)
        for category, limit_amount in sorted(limits.items())
    ]
    reply = f"Budgeting window (ends {window['ends_at']:%a %-I:%M %p}):\n" + "\n".join(lines)
    send_telegram_message(chat_id, reply)
    return _ok("window status")


def handle_cancel_budget_window(conn, chat_id):
    window = get_open_budget_window(conn)
    if not window:
        send_telegram_message(chat_id, "No active or pending budgeting window to cancel.")
        return _ok("no open window")

    limits = get_budget_window_limits(conn, window["id"])

    with conn.cursor() as cur:
        cur.execute("UPDATE budget_windows SET status = 'ended' WHERE id = %s", (window["id"],))
    conn.commit()

    if limits:
        lines = [
            window_category_line(conn, window, category, limit_amount)
            for category, limit_amount in sorted(limits.items())
        ]
        reply = "Cancelled. Final numbers:\n" + "\n".join(lines)
    else:
        reply = "Cancelled the budgeting window setup."

    send_telegram_message(chat_id, reply)
    return _ok("window cancelled")


def handle_set_paycheck_limit(conn, chat_id, category, new_limit):
    with conn.cursor() as cur:
        cur.execute("UPDATE budgets SET cycle_limit = %s WHERE category = %s", (new_limit, category))
    conn.commit()

    send_telegram_message(
        chat_id, f"{casual_opener()}, {display_category(category)} paycheck period limit is now ${new_limit:.2f}."
    )
    return _ok("paycheck limit updated")


def category_status_line(conn, category: str) -> str:
    budget = get_budget(conn, category)
    if not budget:
        return f"{display_category(category)}: no budget set"
    cycle_total = get_cycle_total(conn, category, budget)
    remaining = budget["cycle_limit"] - cycle_total
    line = f"{display_category(category)}: ${cycle_total:.2f}/${budget['cycle_limit']:.2f} this paycheck period"
    if remaining >= 0:
        line += f" (${remaining:.2f} left)"
    else:
        line += overspend_tail(-remaining)
    return line


def handle_get_cycle_summary(conn, chat_id):
    lines = [category_status_line(conn, category) for category in sorted(TRACKED_CATEGORIES)]
    send_telegram_message(chat_id, "Paycheck period:\n" + "\n".join(lines))
    return _ok("cycle summary")


def handle_get_category_status(conn, chat_id, category):
    send_telegram_message(chat_id, category_status_line(conn, category))
    return _ok("category status")


def handle_correct_last_purchase(conn, chat_id, tool_input):
    last = get_last_transaction(conn)
    if not last:
        send_telegram_message(chat_id, "No recent purchase to correct.")
        return _ok("no transaction")

    new_merchant = tool_input.get("merchant")
    new_amount = tool_input.get("amount")
    new_category = tool_input.get("category")

    apply_correction(conn, last["id"], merchant=new_merchant, amount=new_amount, category=new_category)

    updated_merchant = new_merchant or last["merchant"]
    updated_amount = new_amount if new_amount is not None else last["amount"]
    updated_category = new_category or last["category"]

    reply = (
        f"Corrected: {last['merchant']} ${last['amount']:.2f} ({display_category(last['category'])}) -> "
        f"{updated_merchant} ${updated_amount:.2f} ({display_category(updated_category)})"
    )

    budget = get_budget(conn, updated_category)
    if budget:
        cycle_total = get_cycle_total(conn, updated_category, budget)
        reply += f"\n{display_category(updated_category)}: ${cycle_total:.2f}/${budget['cycle_limit']:.2f} this paycheck period."
        if cycle_total > budget["cycle_limit"]:
            reply += overspend_tail(cycle_total - budget["cycle_limit"])

    if last["session_id"]:
        session_total = refresh_session_total(conn, last["session_id"])
        reply += f"\nGoing out total: ${session_total:.2f}"

    send_telegram_message(chat_id, reply)
    return _ok("corrected")


def handle_undo_last_purchase(conn, chat_id):
    last = get_last_transaction(conn)
    if not last:
        send_telegram_message(chat_id, "No recent purchase to undo.")
        return _ok("no transaction")

    delete_transaction(conn, last["id"])
    reply = f"Removed: {last['merchant']}, ${last['amount']:.2f} ({display_category(last['category'])})"

    if last["session_id"]:
        session_total = refresh_session_total(conn, last["session_id"])
        reply += f"\nGoing out total: ${session_total:.2f}"

    send_telegram_message(chat_id, reply)
    return _ok("undone")


def handle_log_purchase(conn, chat_id, text, parsed):
    if parsed["confidence"] < LOW_CONFIDENCE_THRESHOLD:
        # Don't guess-log an ambiguous parse — ask for clarification instead.
        # The user just resends a clearer message, which gets parsed fresh.
        reply = (
            f"Not sure that parsed right, best guess: {parsed['merchant']}, "
            f"${parsed['amount']:.2f}, {display_category(parsed['category'])} "
            f"({parsed['confidence']:.0%} confidence). Resend with the merchant and amount spelled out."
        )
        send_telegram_message(chat_id, reply)
        return _ok("clarification requested")

    if parsed["category"] not in TRACKED_CATEGORIES:
        send_telegram_message(
            chat_id,
            f"Not tracking {display_category(parsed['category'])} purchases right now "
            f"(just food/drink, groceries, and transport).",
        )
        return _ok("category not tracked")

    session = get_active_session(conn)
    session_id = session["id"] if session else None

    insert_transaction(conn, text, parsed, session_id=session_id)
    budget = get_budget(conn, parsed["category"])
    if budget:
        cycle_total = get_cycle_total(conn, parsed["category"], budget)

    reply = f"Logged: {parsed['merchant']}, ${parsed['amount']:.2f} ({display_category(parsed['category'])})"
    if budget:
        reply += f"\n{display_category(parsed['category'])}: ${cycle_total:.2f}/${budget['cycle_limit']:.2f} this paycheck period."
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

    window = get_active_budget_window(conn)
    if window:
        window_limits = get_budget_window_limits(conn, window["id"])
        if parsed["category"] in window_limits:
            reply += "\nWindow " + window_category_line(
                conn, window, parsed["category"], window_limits[parsed["category"]]
            )

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
            open_window = get_open_budget_window(conn)
            window_active = bool(open_window and open_window["status"] == "active")
            window_pending = bool(open_window and open_window["status"] == "pending")

            tool_name, tool_input = classify_message(text, awaiting_target, window_active, window_pending)

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
            elif tool_name == "get_cycle_summary":
                return handle_get_cycle_summary(conn, chat_id)
            elif tool_name == "get_category_status":
                return handle_get_category_status(conn, chat_id, tool_input["category"])
            elif tool_name == "correct_last_purchase":
                return handle_correct_last_purchase(conn, chat_id, tool_input)
            elif tool_name == "undo_last_purchase":
                return handle_undo_last_purchase(conn, chat_id)
            elif tool_name == "start_budget_window":
                return handle_start_budget_window(conn, chat_id, tool_input)
            elif tool_name == "get_budget_window_status":
                return handle_get_budget_window_status(conn, chat_id)
            elif tool_name == "cancel_budget_window":
                return handle_cancel_budget_window(conn, chat_id)
            elif tool_name == "set_paycheck_period_limit":
                return handle_set_paycheck_limit(conn, chat_id, tool_input["category"], tool_input["new_limit"])
            elif tool_name == "add_budget_window_category_limit":
                return handle_add_window_limit(
                    conn, chat_id, tool_input["category"], tool_input["limit_amount"]
                )
            elif tool_name == "acknowledge_no_spend":
                return handle_acknowledge_no_spend(conn, chat_id)
            elif tool_name == "greet":
                return handle_greet(conn, chat_id)
            else:
                return handle_log_purchase(conn, chat_id, text, tool_input)
        finally:
            conn.close()

    except Exception:
        # Always return 200 so Telegram doesn't retry-storm us on a transient error;
        # the failure is still visible in CloudWatch.
        logger.exception("Failed to process Telegram update")
        return _ok("error logged")
