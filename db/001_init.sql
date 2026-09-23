CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    raw_text TEXT NOT NULL,
    merchant TEXT,
    amount NUMERIC(10, 2) NOT NULL,
    category TEXT NOT NULL,
    confidence REAL NOT NULL,
    logged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id INTEGER
);

CREATE TABLE IF NOT EXISTS budgets (
    category TEXT PRIMARY KEY,
    monthly_limit NUMERIC(10, 2) NOT NULL,
    cycle_start_day INTEGER NOT NULL DEFAULT 1
);
