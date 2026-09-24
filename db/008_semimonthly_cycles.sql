-- Paycheck-aligned cycles: 1st-15th, then 16th-end of month. Calendar months
-- vary in length, so this can't reuse the fixed cycle_length_days model —
-- add a cycle_type discriminator instead. cycle_anchor/cycle_length_days are
-- simply ignored for 'semimonthly' rows.
ALTER TABLE budgets ADD COLUMN IF NOT EXISTS cycle_type TEXT NOT NULL DEFAULT 'fixed_days'
    CHECK (cycle_type IN ('fixed_days', 'semimonthly'));

UPDATE budgets SET cycle_type = 'semimonthly';
