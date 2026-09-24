-- Budget windows now go through a 'pending' state while duration/category
-- limits are still being collected across multiple messages (previously this
-- was fully stateless, so a partial follow-up like "food and drink $125" with
-- no duration had nowhere to attach and fell through as unrecognized).
ALTER TABLE budget_windows ALTER COLUMN ends_at DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'budget_windows_status_check2'
    ) THEN
        ALTER TABLE budget_windows DROP CONSTRAINT IF EXISTS budget_windows_status_check;
        ALTER TABLE budget_windows ADD CONSTRAINT budget_windows_status_check2
            CHECK (status IN ('pending', 'active', 'ended'));
    END IF;
END $$;

-- 'pending' counts as "already have one going" too, same reasoning as sessions'
-- awaiting_target status.
DROP INDEX IF EXISTS one_active_budget_window;
CREATE UNIQUE INDEX IF NOT EXISTS one_open_budget_window ON budget_windows ((true))
    WHERE status IN ('pending', 'active');
