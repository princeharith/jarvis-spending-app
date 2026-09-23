-- One-time seed of the original 7-category budget scheme. Superseded by
-- db/005_narrow_categories.sql, which renames 'food' -> 'food_drink' and
-- drops the rest — guarded so this doesn't resurrect the old 'food' row
-- (and its now-dropped columns) on every re-run of apply_schema.sh.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM budgets WHERE category = 'food_drink') THEN
        INSERT INTO budgets (category, monthly_limit, cycle_start_day) VALUES
            ('food', 500.00, 1),
            ('coffee', 20.00, 1),
            ('groceries', 500.00, 1),
            ('transport', 150.00, 1),
            ('entertainment', 150.00, 1),
            ('shopping', 300.00, 1),
            ('other', 200.00, 1)
        ON CONFLICT (category) DO UPDATE
            SET monthly_limit = EXCLUDED.monthly_limit,
                cycle_start_day = EXCLUDED.cycle_start_day;
    END IF;
END $$;
