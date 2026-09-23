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
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "jarvis")
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

CHECKIN_INTERVAL_MINUTES = 45
WIND_DOWN_HOURS = 5
MAX_SESSION_HOURS = 6
GOING_OUT_CATEGORIES = ("food", "entertainment", "shopping")

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)


def overspend_tail(over_by: float) -> str:
    return f" That's ${over_by:.2f} over. Cut it the fuck out."


def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )


def get_active_sessions(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, started_at, last_checkin_at, running_total, target_amount,
                   wind_down_nudge_sent,
                   EXTRACT(EPOCH FROM (now() - started_at)) / 3600.0 AS hours_elapsed,
                   EXTRACT(EPOCH FROM (now() - last_checkin_at)) / 60.0 AS minutes_since_checkin
            FROM sessions
            WHERE status IN ('awaiting_target', 'active')
            """
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "started_at": r[1],
            "last_checkin_at": r[2],
            "running_total": float(r[3]),
            "target_amount": float(r[4]) if r[4] is not None else None,
            "wind_down_nudge_sent": r[5],
            "hours_elapsed": float(r[6]),
            "minutes_since_checkin": float(r[7]),
        }
        for r in rows
    ]


def refresh_session_total(conn, session_id: int) -> float:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE session_id = %s",
            (session_id,),
        )
        (total,) = cur.fetchone()
        cur.execute("UPDATE sessions SET running_total = %s WHERE id = %s", (total, session_id))
    conn.commit()
    return float(total)


def get_going_out_budget_headroom(conn) -> float:
    # Loose proxy for "budget remaining for a night out": combined remaining
    # this cycle across food/entertainment/shopping. Just context for the LLM's
    # tone, not an exact per-session allowance.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT category, monthly_limit, cycle_start_day
            FROM budgets WHERE category = ANY(%s)
            """,
            (list(GOING_OUT_CATEGORIES),),
        )
        budgets = cur.fetchall()

        remaining_total = 0.0
        for category, monthly_limit, cycle_start_day in budgets:
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
            (cycle_total,) = cur.fetchone()
            remaining_total += float(monthly_limit) - float(cycle_total)
    return remaining_total


def end_session(conn, session_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE sessions SET status = 'ended' WHERE id = %s", (session_id,))
    conn.commit()


def touch_checkin(conn, session_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE sessions SET last_checkin_at = now() WHERE id = %s", (session_id,))
    conn.commit()


def mark_wind_down_sent(conn, session_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE sessions SET wind_down_nudge_sent = true WHERE id = %s", (session_id,))
    conn.commit()


def generate_checkin_message(
    hours_elapsed: float, running_total: float, budget_headroom: float, target_amount
) -> str:
    pace_per_hour = running_total / hours_elapsed if hours_elapsed > 0 else running_total
    target_line = (
        f"- Target for tonight: ${target_amount:.2f}\n" if target_amount is not None else ""
    )
    prompt = (
        "You're Jarvis, a personal-finance texting assistant checking in on the user mid-way "
        "through a night out. Write ONE short text message (1-2 sentences max) asking how the "
        "night's going and prompting them to text you what they've spent if they haven't logged it.\n\n"
        f"Session state:\n"
        f"- Out for {hours_elapsed:.1f} hours so far\n"
        f"- Spent ${running_total:.2f} tonight (~${pace_per_hour:.2f}/hour pace)\n"
        f"{target_line}"
        f"- Remaining budget headroom this month across food/entertainment/shopping: ${budget_headroom:.2f}\n\n"
        "Tone rules: serious and direct, never jokey or cutesy. NEVER use emoji, not even one. "
        "If they're over or close to their stated target, spending fast relative to elapsed "
        "time, or budget headroom is low/negative, be blunt about it and use profanity (e.g. "
        "'fuck', 'shit') to make the point land — don't soften it. If spending is modest and "
        "there's plenty of headroom, stay serious but neutral, no profanity needed. "
        "Output only the message text, nothing else."
    )
    response = anthropic_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()


def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
    req = urlrequest.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urlrequest.urlopen(req, timeout=10) as resp:
        resp.read()


def process_sessions(conn):
    sessions = get_active_sessions(conn)
    results = []

    for session in sessions:
        if session["hours_elapsed"] >= MAX_SESSION_HOURS:
            total = refresh_session_total(conn, session["id"])
            end_session(conn, session["id"])
            msg = f"Auto-ended your going-out session after {MAX_SESSION_HOURS} hours. Total spent: ${total:.2f}."
            if session["target_amount"] is not None and total > session["target_amount"]:
                msg += overspend_tail(total - session["target_amount"])
            send_telegram_message(msg)
            results.append({"session_id": session["id"], "action": "timed_out"})
            continue

        if session["hours_elapsed"] >= WIND_DOWN_HOURS and not session["wind_down_nudge_sent"]:
            total = refresh_session_total(conn, session["id"])
            msg = "5 hours in — are you home or done for the night? Text 'heading home' to close this out."
            if session["target_amount"] is not None and total > session["target_amount"]:
                msg += overspend_tail(total - session["target_amount"])
            send_telegram_message(msg)
            mark_wind_down_sent(conn, session["id"])
            results.append({"session_id": session["id"], "action": "wind_down_nudge"})
            continue

        if session["minutes_since_checkin"] < CHECKIN_INTERVAL_MINUTES:
            results.append({"session_id": session["id"], "action": "not_due"})
            continue

        total = refresh_session_total(conn, session["id"])
        headroom = get_going_out_budget_headroom(conn)
        message = generate_checkin_message(
            session["hours_elapsed"], total, headroom, session["target_amount"]
        )
        send_telegram_message(message)
        touch_checkin(conn, session["id"])
        results.append({"session_id": session["id"], "action": "checked_in"})

    return results


def get_open_trackers(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, target_amount, starts_at, ends_at, over_target_notified,
                   COALESCE((
                       SELECT SUM(amount) FROM transactions
                       WHERE logged_at >= t.starts_at AND logged_at <= LEAST(now(), t.ends_at)
                   ), 0) AS spent
            FROM trackers t
            WHERE status = 'active'
            """
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "target_amount": float(r[1]),
            "starts_at": r[2],
            "ends_at": r[3],
            "over_target_notified": r[4],
            "spent": float(r[5]),
        }
        for r in rows
    ]


def close_tracker(conn, tracker_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE trackers SET status = 'ended' WHERE id = %s", (tracker_id,))
    conn.commit()


def mark_tracker_notified(conn, tracker_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE trackers SET over_target_notified = true WHERE id = %s", (tracker_id,))
    conn.commit()


def process_trackers(conn):
    results = []
    now = datetime.now(timezone.utc)

    for tracker in get_open_trackers(conn):
        if now >= tracker["ends_at"]:
            msg = f"Tracker done: spent ${tracker['spent']:.2f} of your ${tracker['target_amount']:.2f} target."
            if tracker["spent"] > tracker["target_amount"]:
                msg += overspend_tail(tracker["spent"] - tracker["target_amount"])
            else:
                msg += " Stayed under."
            send_telegram_message(msg)
            close_tracker(conn, tracker["id"])
            results.append({"tracker_id": tracker["id"], "action": "closed"})
            continue

        if tracker["spent"] > tracker["target_amount"] and not tracker["over_target_notified"]:
            msg = f"Tracker alert: spent ${tracker['spent']:.2f}, over your ${tracker['target_amount']:.2f} target."
            msg += overspend_tail(tracker["spent"] - tracker["target_amount"])
            send_telegram_message(msg)
            mark_tracker_notified(conn, tracker["id"])
            results.append({"tracker_id": tracker["id"], "action": "over_target_notified"})
            continue

        results.append({"tracker_id": tracker["id"], "action": "not_due"})

    return results


def lambda_handler(event, context):
    conn = get_db_connection()
    try:
        session_results = process_sessions(conn)
        tracker_results = process_trackers(conn)
        logger.info("Sessions: %s | Trackers: %s", session_results, tracker_results)
        return {"sessions": session_results, "trackers": tracker_results}
    finally:
        conn.close()
