ALTER TABLE sessions ADD COLUMN IF NOT EXISTS target_amount NUMERIC(10, 2);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS wind_down_nudge_sent BOOLEAN NOT NULL DEFAULT false;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sessions_status_check2'
    ) THEN
        ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_status_check;
        ALTER TABLE sessions ADD CONSTRAINT sessions_status_check2
            CHECK (status IN ('awaiting_target', 'active', 'ended'));
    END IF;
END $$;

-- "awaiting_target" counts as being out too (already started, just hasn't stated
-- a target yet), so the one-active-session index must cover both statuses.
DROP INDEX IF EXISTS one_active_session;
CREATE UNIQUE INDEX IF NOT EXISTS one_active_session ON sessions ((true))
    WHERE status IN ('awaiting_target', 'active');

-- Ad-hoc spending trackers, independent of going-out sessions. Progress is
-- computed live from transactions.logged_at falling in [starts_at, ends_at)
-- rather than tagging rows, so trackers can overlap each other and/or a
-- going-out session without needing a join table.
CREATE TABLE IF NOT EXISTS trackers (
    id SERIAL PRIMARY KEY,
    target_amount NUMERIC(10, 2) NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ends_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended')),
    over_target_notified BOOLEAN NOT NULL DEFAULT false
);
