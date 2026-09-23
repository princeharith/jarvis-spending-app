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
