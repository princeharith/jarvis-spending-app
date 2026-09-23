import json
import logging
import os
from datetime import datetime, timedelta
from urllib import request as urlrequest
from zoneinfo import ZoneInfo

import psycopg2

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "jarvis")
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

LOCAL_TZ = ZoneInfo("America/New_York")
MEAL_CATEGORIES = ("food_drink", "groceries")

# Each meal's window is [window_start_hour, now) local time on the day the check runs.
MEAL_WINDOWS = {
    "lunch": {"window_start_hour": 11, "nudge": "You didn't buy lunch today?"},
    "dinner": {"window_start_hour": 17, "nudge": "You didn't get dinner tonight?"},
}


def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )


def has_meal_logged(conn, window_start: datetime) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM transactions
                WHERE category = ANY(%s) AND logged_at >= %s
            )
            """,
            (list(MEAL_CATEGORIES), window_start),
        )
        (exists,) = cur.fetchone()
    return exists


def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
    req = urlrequest.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urlrequest.urlopen(req, timeout=10) as resp:
        resp.read()


def lambda_handler(event, context):
    meal = event.get("meal")
    window = MEAL_WINDOWS.get(meal)
    if not window:
        raise ValueError(f"Unknown or missing 'meal' in event input: {meal!r}")

    now_local = datetime.now(LOCAL_TZ)
    window_start = now_local.replace(
        hour=window["window_start_hour"], minute=0, second=0, microsecond=0
    )
    if window_start > now_local:
        window_start -= timedelta(days=1)

    conn = get_db_connection()
    try:
        logged = has_meal_logged(conn, window_start)
    finally:
        conn.close()

    if logged:
        logger.info("%s already logged since %s, staying silent", meal, window_start.isoformat())
        return {"meal": meal, "nudged": False}

    send_telegram_message(window["nudge"])
    logger.info("Sent %s nudge (nothing logged since %s)", meal, window_start.isoformat())
    return {"meal": meal, "nudged": True}
