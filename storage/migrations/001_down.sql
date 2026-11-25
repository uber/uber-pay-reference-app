DROP TRIGGER IF EXISTS skip_archived_job ON jobs;
DROP FUNCTION IF EXISTS reference_skip_archived_job();
DROP FUNCTION IF EXISTS reference_maintain(TEXT,INTEGER);
DROP FUNCTION IF EXISTS reference_admit_consent(TEXT,TEXT,INTEGER,INTEGER);
DROP FUNCTION IF EXISTS reference_bind_runtime(TEXT,TEXT,TEXT);
DROP VIEW IF EXISTS reference_operations;
DROP TABLE IF EXISTS operations_archive,jobs_archive,runtime_bindings,public_buckets;
-- DESTRUCTIVE: disposable local test DB ONLY; never run against a PSP ledger.
DROP TRIGGER IF EXISTS transaction_settlement_entry ON transactions;
DROP TRIGGER IF EXISTS immutable_settlement_entries ON settlement_entries;
DROP TRIGGER IF EXISTS immutable_settlement_batches ON settlement_batches;
DROP TRIGGER IF EXISTS immutable_settlement_files ON settlement_batch_files;
DROP FUNCTION IF EXISTS reference_settlement_entry();
DROP FUNCTION IF EXISTS reference_immutable_settlement();
DROP TABLE IF EXISTS settlement_deliveries,settlement_batch_files,settlement_batches,settlement_entries;
DROP TRIGGER IF EXISTS transaction_authorized_amount ON transactions;
DROP FUNCTION IF EXISTS reference_authorized_amount();
DROP FUNCTION IF EXISTS reference_spendable(TEXT,TEXT,TEXT);
DROP FUNCTION IF EXISTS reference_assert_unclaimed(TEXT,TEXT);
DROP FUNCTION IF EXISTS reference_reserve_intent(TEXT,TEXT,TEXT,JSONB);
DROP FUNCTION IF EXISTS reference_adjust(TEXT,TEXT,TEXT,BIGINT,BIGINT);
DROP FUNCTION IF EXISTS reference_next_event(TEXT,TEXT,TEXT);
DROP FUNCTION IF EXISTS reference_reverse_refund(TEXT,TEXT);
DROP TABLE IF EXISTS intent_reservations, resource_claims, provider_keys, provider_intents,
 refund_reversals, account_debts, tokens, jobs, operations, transactions, accounts;
DROP TABLE IF EXISTS operator_actions;
DROP FUNCTION IF EXISTS reference_lock_accounts(TEXT,JSONB);
