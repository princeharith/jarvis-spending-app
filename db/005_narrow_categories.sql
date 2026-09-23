-- Narrowing tracked categories to food_drink, groceries, transport.
-- Coffee folds into food_drink; entertainment/shopping/other are no longer tracked.
UPDATE budgets SET category = 'food_drink', monthly_limit = 550.00 WHERE category = 'food';
DELETE FROM budgets WHERE category IN ('coffee', 'entertainment', 'shopping', 'other');
