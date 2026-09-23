CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checkin_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended')),
    running_total NUMERIC(10, 2) NOT NULL DEFAULT 0
);

-- Only one active session at a time makes sense for a single-user bot.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_session ON sessions ((true)) WHERE status = 'active';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_transactions_session'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT fk_transactions_session FOREIGN KEY (session_id) REFERENCES sessions(id);
    END IF;
END $$;
