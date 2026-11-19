package gateway

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Store struct {
	Pool   *pgxpool.Pool
	Client string
}

func OpenStore(ctx context.Context, c Config) (*Store, error) {
	pc, e := pgxpool.ParseConfig(c.DatabaseURL)
	if e != nil {
		return nil, e
	}
	pc.ConnConfig.RuntimeParams["application_name"] = c.ClientID
	pc.ConnConfig.RuntimeParams["statement_timeout"] = "5000"
	pc.ConnConfig.RuntimeParams["lock_timeout"] = "1500"
	pool, e := pgxpool.NewWithConfig(ctx, pc)
	if e != nil {
		return nil, e
	}
	fail := func(e error) (*Store, error) { pool.Close(); return nil, e }
	if e = pool.Ping(ctx); e != nil {
		return fail(e)
	}
	sql, e := os.ReadFile(c.MigrationFile)
	if e != nil {
		return fail(e)
	}
	if _, e = pool.Exec(ctx, string(sql)); e != nil {
		return fail(e)
	}
	for id, amount := range map[string]int64{"demo-settlement": 1000000000000000000, "demo-account": 1000000000000000000, "insufficient-account": 100000} {
		_, e = pool.Exec(ctx, "INSERT INTO accounts(client_id,account_id,currency,available_e5,balance_e5) VALUES($1,$2,'BRL',$3,$3) ON CONFLICT DO NOTHING", c.ClientID, id, amount)
		if e != nil {
			return fail(e)
		}
	}
	return &Store{Pool: pool, Client: c.ClientID}, nil
}
func (s *Store) Begin(ctx context.Context) (pgx.Tx, error) {
	tx, e := s.Pool.Begin(ctx)
	if e != nil {
		return nil, e
	}
	_, e = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", s.Client)
	if e != nil {
		tx.Rollback(ctx)
		return nil, e
	}
	return tx, nil
}
func randomToken() string {
	var b [32]byte
	if _, e := rand.Read(b[:]); e != nil {
		panic(e)
	}
	return hex.EncodeToString(b[:])
}

type Transaction struct {
	Kind, ID, Ref, Original, Account, Currency, Capture, Funding, State, Detail, Hash, Response, Source, Payee, Profile string
	Amount                                                                                                              int64
	Status                                                                                                              int
	Created, Updated                                                                                                    time.Time
}

func getTransaction(ctx context.Context, tx pgx.Tx, client, kind, id string) (Transaction, error) {
	t := Transaction{}
	e := tx.QueryRow(ctx, "SELECT kind,id,psp_reference,COALESCE(original_id,''),account_id,amount_e5,currency,capture_method,funding_method,state,COALESCE(detail,''),request_hash,response_status,response_body,created_at,updated_at,COALESCE(source_account_id,''),COALESCE(payee_account_id,''),reference_profile FROM transactions WHERE client_id=$1 AND kind=$2 AND id=$3 FOR UPDATE", client, kind, id).Scan(&t.Kind, &t.ID, &t.Ref, &t.Original, &t.Account, &t.Amount, &t.Currency, &t.Capture, &t.Funding, &t.State, &t.Detail, &t.Hash, &t.Status, &t.Response, &t.Created, &t.Updated, &t.Source, &t.Payee, &t.Profile)
	return t, e
}
func insertTransaction(ctx context.Context, tx pgx.Tx, client string, t Transaction) error {
	_, e := tx.Exec(ctx, "INSERT INTO transactions(client_id,kind,id,psp_reference,original_id,account_id,amount_e5,currency,capture_method,funding_method,state,detail,request_hash,response_status,response_body,expires_at) VALUES($1,$2,$3,$4,NULLIF($5,''),$6,$7,$8,$9,$10,$11,NULLIF($12,''),$13,$14,$15,CASE WHEN $11='AUTHORIZED' THEN now()+interval '72 hours' ELSE NULL END)", client, t.Kind, t.ID, t.Ref, t.Original, t.Account, t.Amount, t.Currency, t.Capture, t.Funding, t.State, t.Detail, t.Hash, t.Status, t.Response)
	if e == nil {
		_, e = tx.Exec(ctx, "UPDATE transactions SET source_account_id=NULLIF($4,''),payee_account_id=NULLIF($5,''),reference_profile=COALESCE(NULLIF($6,''),'{}') WHERE client_id=$1 AND kind=$2 AND id=$3", client, t.Kind, t.ID, t.Source, t.Payee, t.Profile)
	}
	return e
}
func accountBalance(ctx context.Context, tx pgx.Tx, client, account, currency string) (int64, int64, error) {
	var a, b int64
	e := tx.QueryRow(ctx, "SELECT reference_spendable(a.client_id,a.account_id,a.currency),GREATEST(0,a.balance_e5-COALESCE(d.debt_e5,0))::bigint FROM accounts a LEFT JOIN account_debts d USING(client_id,account_id,currency) WHERE a.client_id=$1 AND a.account_id=$2 AND a.currency=$3 FOR UPDATE OF a", client, account, currency).Scan(&a, &b)
	return a, b, e
}
func adjust(ctx context.Context, tx pgx.Tx, client, account, currency string, available, balance int64) error {
	_, e := tx.Exec(ctx, "SELECT reference_adjust($1,$2,$3,$4,$5)", client, account, currency, available, balance)
	return e
}
func queue(ctx context.Context, tx pgx.Tx, client, kind, id, job, callback string, payload any, status string, delay time.Duration) error {
	var sequence int64
	if job == "CALLBACK" {
		if e := tx.QueryRow(ctx, "SELECT COALESCE(reference_next_event($1,$2,$3),0)", client, kind, id).Scan(&sequence); e != nil {
			return e
		}
	}
	jobID := fingerprint(client + "|" + kind + "|" + id + "|" + job + "|" + callback + "|" + fmt.Sprint(sequence))
	_, e := tx.Exec(ctx, "INSERT INTO jobs(id,client_id,tx_kind,tx_id,job_type,callback_name,payload,status,next_attempt_at,event_sequence) VALUES($1,$2,$3,$4,$5,$6,$7,$8,now()+($9*interval '1 millisecond'),$10) ON CONFLICT DO NOTHING", jobID, client, kind, id, job, callback, encode(payload), status, delay.Milliseconds(), sequence)
	return e
}
func notFound(e error) bool { return errors.Is(e, pgx.ErrNoRows) }
