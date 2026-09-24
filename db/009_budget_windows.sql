-- "Current budgeting window": a separate, user-started, arbitrary-duration
-- budget (a week, a weekend, etc.) with its own per-category limits —
-- distinct from the semimonthly "paycheck period" in budgets/cycle_*.
-- Progress is computed live from transactions.logged_at, same pattern as
-- trackers, so it doesn't need to tag rows.
CREATE TABLE IF NOT EXISTS budget_windows (
    id SERIAL PRIMARY KEY,
    starts_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ends_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended'))
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_budget_window ON budget_windows ((true))
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS budget_window_limits (
    window_id INTEGER NOT NULL REFERENCES budget_windows(id),
    category TEXT NOT NULL,
    limit_amount NUMERIC(10, 2) NOT NULL,
    PRIMARY KEY (window_id, category)
);
