package gateway

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"math/rand/v2"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
)

type CallbackDefinition struct {
	Path, Scope string
	Idempotency bool
}

var Callbacks = map[string]CallbackDefinition{
	"chargeCallback": {"/v1/payments/charges/%s", "payments.deposits", true},
	"payoutCallback": {"/v1/payments/payouts/%s", "payments.payouts", true},
	"updateRefund":   {"/v1/payments/refunds/%s", "payments.deposits", false},
	"confirmDeposit": {"/v1/payments/deposits/%s/confirm", "payments.deposits", true},
	"cancelDeposit":  {"/v1/payments/deposits/%s/cancel", "payments.deposits", true},
	"updateDeposit":  {"/v1/payments/deposits/%s", "payments.deposits", true},
}

func (c Contract) Callback(name string, payload any) error {
	def, ok := object(object(c.Spec["components"])["callbacks"])[name]
	if !ok {
		return errors.New("unknown callback")
	}
	for _, path := range object(def) {
		op := object(object(path)["post"])
		schema := object(object(object(object(op["requestBody"])["content"])["application/json"])["schema"])
		return c.Validate(schema, payload)
	}
	return errors.New("missing callback schema")
}
func BuildCallback(name, id string, payload any) (string, string, bool, error) {
	d, ok := Callbacks[name]
	if !ok || id == "" {
		return "", "", false, errors.New("unknown callback or empty id")
	}
	return fmt.Sprintf(d.Path, url.PathEscape(id)), d.Scope, d.Idempotency, nil
}

type cachedToken struct {
	Value   string
	Expires time.Time
}
type UberTokenProvider struct {
	mu      sync.Mutex
	entries map[string]cachedToken
}

func (p *UberTokenProvider) Get(ctx context.Context, a *App, scope string) (string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if t, ok := p.entries[scope]; ok && time.Now().Before(t.Expires) {
		return t.Value, nil
	}
	form := url.Values{"client_id": {a.Config.UberClientID}, "client_secret": {a.Config.UberClientSecret}, "scope": {scope}, "grant_type": {"client_credentials"}}
	r, e := http.NewRequestWithContext(ctx, "POST", a.Config.TokenURL, strings.NewReader(form.Encode()))
	if e != nil {
		return "", e
	}
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, e := a.Client.Do(r)
	if e != nil {
		return "", e
	}
	defer resp.Body.Close()
	raw, e := io.ReadAll(io.LimitReader(resp.Body, 1048576))
	if e != nil || resp.StatusCode < 200 || resp.StatusCode > 299 {
		return "", errors.New("callback token request failed")
	}
	v, e := DecodeJSON(raw)
	if e != nil {
		return "", e
	}
	b := object(v)
	token := str(b, "access_token")
	secs, e := number(b["expires_in"])
	if e != nil {
		secs, e = strconv.ParseInt(str(b, "expires_in"), 10, 64)
	}
	if e != nil || secs <= 0 || secs > 86400*366 || token == "" {
		return "", errors.New("invalid OAuth token response")
	}
	margin := secs / 10
	if margin > 60 {
		margin = 60
	}
	p.entries[scope] = cachedToken{token, time.Now().Add(time.Duration(secs-margin) * time.Second)}
	return token, nil
}
func (p *UberTokenProvider) Invalidate(scope string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.entries, scope)
}
func (a *App) RunWorkers(ctx context.Context) {
	ticker := time.NewTicker(a.Config.Interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if e := a.expireAuthorizations(ctx); e != nil && ctx.Err() == nil {
				log.Print("expiry worker database failure")
			}
			for i := 0; i < 8; i++ {
				ok, e := a.processCompletion(ctx)
				if e != nil {
					if ctx.Err() == nil {
						log.Print("completion worker database failure")
					}
					break
				}
				if !ok {
					break
				}
			}
			if e := a.processReversal(ctx); e != nil && ctx.Err() == nil {
				log.Print("refund reversal worker failure")
			}
			for i := 0; i < 8; i++ {
				ok, e := a.processCallback(ctx)
				if e != nil {
					if ctx.Err() == nil {
						log.Print("callback worker database failure")
					}
					break
				}
				if !ok {
					break
				}
			}
		}
	}
}
func (a *App) expireAuthorizations(ctx context.Context) error {
	tx, e := a.Store.Begin(ctx)
	if e != nil {
		return e
	}
	defer tx.Rollback(ctx)
	rows, e := tx.Query(ctx, "SELECT id FROM transactions WHERE client_id=$1 AND kind='charge' AND state='AUTHORIZED' AND expires_at<=now() AND NOT EXISTS(SELECT 1 FROM resource_claims r WHERE r.client_id=transactions.client_id AND r.resource_key='charge:'||transactions.id)", a.Config.ClientID)
	if e != nil {
		return e
	}
	var ids []string
	for rows.Next() {
		var id string
		if e = rows.Scan(&id); e != nil {
			return e
		}
		ids = append(ids, id)
	}
	rows.Close()
	for _, id := range ids {
		t, e := getTransaction(ctx, tx, a.Config.ClientID, "charge", id)
		if e != nil {
			return e
		}
		if e = a.cancelAuthorization(ctx, tx, t); e != nil {
			return e
		}
	}
	_, e = tx.Exec(ctx, `UPDATE jobs SET status='PENDING',payload=jsonb_set(jsonb_set(payload::jsonb,'{state}','"REJECTED"'),'{detail}','"MERCHANT_EXPIRED"')::text,next_attempt_at=now()
 WHERE client_id=$1 AND job_type='COMPLETION' AND status='WAITING' AND (payload::jsonb->>'expires_at')::bigint <= $2`, a.Config.ClientID, time.Now().UnixMilli())
	if e != nil {
		return e
	}
	return tx.Commit(ctx)
}
func (a *App) processCompletion(ctx context.Context) (bool, error) {
	tx, e := a.Store.Begin(ctx)
	if e != nil {
		return false, e
	}
	defer tx.Rollback(ctx)
	var job, kind, id, payload string
	e = tx.QueryRow(ctx, "SELECT id,tx_kind,tx_id,payload FROM jobs WHERE client_id=$1 AND job_type='COMPLETION' AND status='PENDING' AND next_attempt_at<=now() ORDER BY next_attempt_at LIMIT 1 FOR UPDATE SKIP LOCKED", a.Config.ClientID).Scan(&job, &kind, &id, &payload)
	if notFound(e) {
		return false, nil
	}
	if e != nil {
		return false, e
	}
	v, e := DecodeJSON([]byte(payload))
	if e != nil {
		return false, e
	}
	state := str(object(v), "state")
	if state != "CONFIRMED" && state != "REJECTED" {
		return false, errors.New("invalid completion state")
	}
	t, e := getTransaction(ctx, tx, a.Config.ClientID, kind, id)
	if e != nil {
		return false, e
	}
	if t.State == "PENDING" {
		if e = a.completeTransaction(ctx, tx, t, state, str(object(v), "detail"), object(v)["amount_e5"]); e != nil {
			return false, e
		}
	}
	_, e = tx.Exec(ctx, "UPDATE jobs SET status='DONE',lease_until=NULL WHERE id=$1", job)
	if e != nil {
		return false, e
	}
	return true, tx.Commit(ctx)
}
func (a *App) completeTransaction(ctx context.Context, tx pgx.Tx, t Transaction, state string, detail string, updated any) error {
	if t.State != "PENDING" {
		return nil
	}
	profileValue, _ := DecodeJSON([]byte(t.Profile))
	profile := object(profileValue)
	originalAmount := t.Amount
	if t.Kind == "charge" && state == "CONFIRMED" && str(profile, "callback_mode") == "deposit" && updated != nil {
		n, err := number(updated)
		if err != nil || n <= 0 || n > t.Amount {
			return errors.New("invalid local deposit amount")
		}
		if n != t.Amount {
			if err = adjust(ctx, tx, a.Config.ClientID, t.Account, t.Currency, t.Amount-n, 0); err != nil {
				return err
			}
			t.Amount = n
			if _, err = tx.Exec(ctx, "UPDATE transactions SET amount_e5=$3 WHERE client_id=$1 AND kind='charge' AND id=$2", a.Config.ClientID, t.ID, n); err != nil {
				return err
			}
		}
	}
	if t.Kind == "refund" {
		if state == "CONFIRMED" {
			_, balance, e := accountBalance(ctx, tx, a.Config.ClientID, t.Account, t.Currency)
			if e != nil {
				return e
			}
			if balance > math.MaxInt64-t.Amount {
				return errors.New("refund overflow")
			}
			if e = adjust(ctx, tx, a.Config.ClientID, t.Account, t.Currency, t.Amount, t.Amount); e != nil {
				return e
			}
		}
	} else {
		available, balance := int64(0), int64(0)
		if state == "CONFIRMED" {
			balance = -t.Amount
		} else {
			available = t.Amount
		}
		if e := adjust(ctx, tx, a.Config.ClientID, t.Account, t.Currency, available, balance); e != nil {
			return e
		}
	}
	if t.Kind == "payout" && state == "CONFIRMED" {
		if e := adjust(ctx, tx, a.Config.ClientID, t.Payee, t.Currency, t.Amount, t.Amount); e != nil {
			return e
		}
	}
	if detail == "" {
		detail = "MERCHANT_REJECTED"
	}
	_, e := tx.Exec(ctx, "UPDATE transactions SET state=$4,detail=CASE WHEN $4='REJECTED' THEN $5 ELSE NULL END,updated_at=now() WHERE client_id=$1 AND kind=$2 AND id=$3", a.Config.ClientID, t.Kind, t.ID, state, detail)
	if e != nil {
		return e
	}
	if t.Kind == "charge" && str(profile, "callback_mode") == "deposit" {
		if state == "CONFIRMED" {
			if t.Amount != originalAmount {
				if err := queue(ctx, tx, a.Config.ClientID, t.Kind, t.ID, "CALLBACK", "updateDeposit", Object{"merchant_reference": t.Ref, "amount": Object{"value": t.Amount, "currency": t.Currency}}, "PENDING", 0); err != nil {
					return err
				}
			}
			return queue(ctx, tx, a.Config.ClientID, t.Kind, t.ID, "CALLBACK", "confirmDeposit", Object{"merchant_reference": t.Ref}, "PENDING", 0)
		}
		return queue(ctx, tx, a.Config.ClientID, t.Kind, t.ID, "CALLBACK", "cancelDeposit", Object{"merchant_reference": t.Ref, "cancel_reason": detail}, "PENDING", 0)
	}
	callback := "chargeCallback"
	payload := Object{"state": Object{"value": state}, "psp_reference": t.Ref}
	if state == "REJECTED" {
		object(payload["state"])["detail"] = detail
	}
	if t.Kind == "payout" {
		callback = "payoutCallback"
	}
	if t.Kind == "refund" {
		callback = "updateRefund"
		payload = Object{"status": state, "merchant_reference": t.Ref}
		if state == "REJECTED" {
			payload["failed_reason"] = "Local synthetic rejection"
		}
	}
	if e = a.Contract.Callback(callback, payload); e != nil {
		return e
	}
	if e = queue(ctx, tx, a.Config.ClientID, t.Kind, t.ID, "CALLBACK", callback, payload, "PENDING", 0); e != nil {
		return e
	}
	if a.Config.ProcessorMode != "http" && t.Kind == "refund" && state == "CONFIRMED" && str(profile, "behavior") == "refund_reversal" {
		return queue(ctx, tx, a.Config.ClientID, t.Kind, t.ID, "REVERSAL", "", Object{}, "PENDING", a.Config.AsyncDelay)
	}
	return nil
}
func (a *App) processCallback(ctx context.Context) (bool, error) {
	tx, e := a.Store.Pool.Begin(ctx)
	if e != nil {
		return false, e
	}
	defer tx.Rollback(ctx)
	var id, kind, txID, name, body string
	var attempts int
	e = tx.QueryRow(ctx, "SELECT id,tx_kind,tx_id,callback_name,payload,attempts FROM jobs j WHERE client_id=$1 AND job_type='CALLBACK' AND ((status='PENDING' AND next_attempt_at<=now()) OR (status='PROCESSING' AND lease_until<=now())) AND NOT EXISTS(SELECT 1 FROM jobs older WHERE older.client_id=j.client_id AND older.tx_kind=j.tx_kind AND older.tx_id=j.tx_id AND older.job_type='CALLBACK' AND older.event_sequence<j.event_sequence AND older.status<>'DONE') ORDER BY next_attempt_at,event_sequence LIMIT 1 FOR UPDATE SKIP LOCKED", a.Config.ClientID).Scan(&id, &kind, &txID, &name, &body, &attempts)
	if notFound(e) {
		return false, nil
	}
	if e != nil {
		return false, e
	}
	var lease time.Time
	e = tx.QueryRow(ctx, "UPDATE jobs SET status='PROCESSING',attempts=attempts+1,lease_until=now()+interval '15 seconds' WHERE id=$1 RETURNING lease_until", id).Scan(&lease)
	if e != nil {
		return false, e
	}
	if e = tx.Commit(ctx); e != nil {
		return false, e
	}
	path, scope, idem, e := BuildCallback(name, txID, nil)
	code := 0
	if e == nil {
		var token string
		token, e = a.Tokens.Get(ctx, a, scope)
		if e == nil {
			var req *http.Request
			req, e = http.NewRequestWithContext(ctx, "POST", strings.TrimRight(a.Config.CallbackBaseURL, "/")+path, strings.NewReader(body))
			if e == nil {
				req.Header.Set("Authorization", "Bearer "+token)
				req.Header.Set("Content-Type", "application/json")
				if idem {
					req.Header.Set("X-Idempotency-Key", id)
				}
				var resp *http.Response
				resp, e = a.Client.Do(req)
				if e == nil {
					code = resp.StatusCode
					_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1048576))
					resp.Body.Close()
				}
			}
		}
	}
	if code == 401 {
		a.Tokens.Invalidate(scope)
	}
	status := "PENDING"
	last := "retryable callback failure"
	if e == nil && code >= 200 && code < 300 {
		status = "DONE"
		last = ""
	} else if code == 409 {
		status = "RECONCILE"
		last = "callback conflict requires reconciliation"
	} else if e == nil && code >= 400 && code < 500 && code != 429 && code != 401 || attempts+1 >= a.Config.MaxAttempts {
		status = "FAILED"
		last = "callback rejected or retry budget exhausted"
	}
	shift := attempts
	if shift > 7 {
		shift = 7
	}
	delay := a.Config.Retry * time.Duration(1<<shift)
	if delay > 30*time.Second {
		delay = 30 * time.Second
	}
	delay += time.Duration(rand.Int64N(max(1, int64(a.Config.Retry/5))))
	_, err := a.Store.Pool.Exec(ctx, "UPDATE jobs SET status=$2,lease_until=NULL,last_error=NULLIF($3,''),next_attempt_at=now()+($4*interval '1 millisecond') WHERE id=$1 AND status='PROCESSING' AND lease_until=$5", id, status, last, delay.Milliseconds(), lease)
	return true, err
}

func (a *App) reverseRefund(ctx context.Context, tx pgx.Tx, id string) error {
	var changed bool
	if e := tx.QueryRow(ctx, "SELECT reference_reverse_refund($1,$2)", a.Config.ClientID, id).Scan(&changed); e != nil {
		return e
	}
	if !changed {
		return nil
	}
	t, e := getTransaction(ctx, tx, a.Config.ClientID, "refund", id)
	if e != nil {
		return e
	}
	return queue(ctx, tx, a.Config.ClientID, "refund", id, "CALLBACK", "updateRefund", Object{"status": "REJECTED", "merchant_reference": t.Ref, "failed_reason": "Synthetic processor reversed the refund; any unrecovered funds are recorded as debt"}, "PENDING", 0)
}
func (a *App) processReversal(ctx context.Context) error {
	tx, e := a.Store.Begin(ctx)
	if e != nil {
		return e
	}
	defer tx.Rollback(ctx)
	var job, id string
	e = tx.QueryRow(ctx, "SELECT id,tx_id FROM jobs WHERE client_id=$1 AND job_type='REVERSAL' AND status='PENDING' AND next_attempt_at<=now() LIMIT 1 FOR UPDATE SKIP LOCKED", a.Config.ClientID).Scan(&job, &id)
	if notFound(e) {
		return nil
	}
	if e != nil {
		return e
	}
	if e = a.reverseRefund(ctx, tx, id); e != nil {
		return e
	}
	if _, e = tx.Exec(ctx, "UPDATE jobs SET status='DONE' WHERE id=$1", job); e != nil {
		return e
	}
	return tx.Commit(ctx)
}
