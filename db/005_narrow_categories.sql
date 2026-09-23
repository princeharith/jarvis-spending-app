-- Narrowing tracked categories to food_drink, groceries, transport.
-- Coffee folds into food_drink; entertainment/shopping/other are no longer tracked.
-- Guarded on the monthly_limit column still existing, since db/006 later
-- renames it to cycle_limit — without this guard, re-running this file after
-- 006 has already run errors on the renamed column.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'budgets' AND column_name = 'monthly_limit'
    ) THEN
        UPDATE budgets SET category = 'food_drink', monthly_limit = 550.00 WHERE category = 'food';
        DELETE FROM budgets WHERE category IN ('coffee', 'entertainment', 'shopping', 'other');
    END IF;
END $$;
