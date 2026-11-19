-- DESTRUCTIVE: disposable local test DB ONLY; never run against a PSP ledger.
DROP FUNCTION IF EXISTS reference_spendable(TEXT,TEXT,TEXT);
DROP FUNCTION IF EXISTS reference_assert_unclaimed(TEXT,TEXT);
DROP FUNCTION IF EXISTS reference_reserve_intent(TEXT,TEXT,TEXT,JSONB);
DROP FUNCTION IF EXISTS reference_adjust(TEXT,TEXT,TEXT,BIGINT,BIGINT);
DROP FUNCTION IF EXISTS reference_next_event(TEXT,TEXT,TEXT);
DROP FUNCTION IF EXISTS reference_reverse_refund(TEXT,TEXT);
DROP TABLE IF EXISTS intent_reservations, resource_claims, provider_keys, provider_intents,
 refund_reversals, account_debts, tokens, jobs, operations, transactions, accounts;
