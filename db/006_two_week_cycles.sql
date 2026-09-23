-- Switch budget cycles from "day-of-month" (which can't express a 2-week
-- period cleanly) to an anchor-date + cycle-length model, and halve the
-- existing limits since cycles are now half as long.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'budgets' AND column_name = 'cycle_length_days'
    ) THEN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'budgets' AND column_name = 'monthly_limit'
        ) THEN
            ALTER TABLE budgets RENAME COLUMN monthly_limit TO cycle_limit;
        END IF;

        ALTER TABLE budgets DROP COLUMN IF EXISTS cycle_start_day;
        ALTER TABLE budgets ADD COLUMN cycle_length_days INTEGER NOT NULL DEFAULT 14;
        ALTER TABLE budgets ADD COLUMN cycle_anchor DATE NOT NULL DEFAULT CURRENT_DATE;

        UPDATE budgets SET cycle_limit = cycle_limit / 2;
    END IF;
END $$;
