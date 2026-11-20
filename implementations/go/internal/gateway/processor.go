package gateway

// The optional HTTP processor is a separate, durable side-effect boundary. No
// HTTP call below is made while a SQL transaction or pooled connection is held.
import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"

	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

type processorAdapter struct {
	DemoPspAdapter
	result     Object
	validation bool
}

func (p processorAdapter) Charge(ctx context.Context, c PaymentCommand) (ProviderResult, error) {
	if p.validation {
		if _, e := (DemoPspAdapter{}).Charge(ctx, c); e != nil {
			return ProviderResult{}, e
		}
		if c.AmountE5 > c.AvailableE5 {
			return ProviderResult{State: "REJECTED", Detail: "INSUFFICIENT_BALANCE"}, nil
		}
		if c.CaptureMethod == "MANUAL" {
			return ProviderResult{State: "AUTHORIZED"}, nil
		}
		return ProviderResult{State: "CONFIRMED"}, nil
	}
	state := str(p.result, "state")
	if c.CaptureMethod == "MANUAL" && state == "CONFIRMED" {
		state = "AUTHORIZED"
	}
	return ProviderResult{State: state, Detail: str(p.result, "detail")}, nil
}
func (p processorAdapter) Payout(ctx context.Context, c PaymentCommand) (ProviderResult, error) {
	c.CaptureMethod = "AUTOMATIC"
	return p.Charge(ctx, c)
}
func (p processorAdapter) Capture(ctx context.Context, c CaptureCommand) (ProviderResult, error) {
	if p.validation {
		if _, e := (DemoPspAdapter{}).Capture(ctx, c); e != nil {
			return ProviderResult{}, e
		}
		return ProviderResult{State: "CONFIRMED"}, nil
	}
	return ProviderResult{State: str(p.result, "state"), Detail: str(p.result, "detail")}, nil
}
func (p processorAdapter) Refund(ctx context.Context, c PaymentCommand) (ProviderResult, error) {
	if p.validation {
		if _, e := (DemoPspAdapter{}).Refund(ctx, c); e != nil {
			return ProviderResult{}, e
		}
		return ProviderResult{State: "CONFIRMED"}, nil
	}
	return ProviderResult{State: str(p.result, "state"), Detail: str(p.result, "detail")}, nil
}

func row(ctx context.Context, tx pgx.Tx, sql string, args ...any) (Object, error) {
	rows, e := tx.Query(ctx, sql, args...)
	if e != nil {
		return nil, e
	}
	return pgx.CollectOneRow(rows, pgx.RowToMap)
}
func integer(o Object, k string) int64 { n, _ := number(o[k]); return n }
func responseRow(o Object) Reply {
	var headers map[string]string
	b, _ := json.Marshal(o["response_headers"])
	_ = json.Unmarshal(b, &headers)
	return Reply{int(integer(o, "response_status")), str(o, "response_body"), headers}
}
func (a *App) processorConfigHash() string {
	return fingerprint(encode(Object{"data": a.Features.Data, "aliases": a.Features.Aliases}))
}
func (a *App) processorCrypt(id string, data []byte, decrypt bool) ([]byte, error) {
	key, e := base64.StdEncoding.DecodeString(a.Config.ProcessorKey)
	if e != nil {
		return nil, e
	}
	block, e := aes.NewCipher(key)
	if e != nil {
		return nil, e
	}
	gcm, e := cipher.NewGCM(block)
	if e != nil {
		return nil, e
	}
	aad := []byte(a.Config.ClientID + "|" + id)
	if decrypt {
		if len(data) < gcm.NonceSize()+gcm.Overhead() {
			return nil, errors.New("invalid encrypted command")
		}
		return gcm.Open(nil, data[:gcm.NonceSize()], data[gcm.NonceSize():], aad)
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, e = rand.Read(nonce); e != nil {
		return nil, e
	}
	return gcm.Seal(nonce, nonce, data, aad), nil
}
func (a *App) processorContext(o Object) (Object, error) {
	b, e := base64.StdEncoding.DecodeString(str(o, "request_encrypted"))
	if e != nil {
		return nil, e
	}
	b, e = a.processorCrypt(str(o, "id"), b, true)
	if e != nil {
		return nil, e
	}
	v, e := DecodeJSON(b)
	if e != nil {
		return nil, e
	}
	ctx := object(v)
	if str(ctx, "config_hash") != a.processorConfigHash() {
		return nil, errors.New("restore pinned funding configuration before reconciliation")
	}
	return ctx, nil
}
func (a *App) processorRemote(key string, body any) (Object, error) {
	method := http.MethodGet
	var r io.Reader
	if body != nil {
		method = http.MethodPost
		r = strings.NewReader(encode(body))
	}
	ctx, cancel := context.WithTimeout(context.Background(), a.Config.ProcessorTimeout)
	defer cancel()
	req, e := http.NewRequestWithContext(ctx, method, a.Config.ProcessorURL+"/operations/"+key, r)
	if e != nil {
		return nil, e
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+a.Config.ProcessorToken)
	req.Header.Set("X-Reference-Client", a.Config.ClientID)
	res, e := a.Client.Do(req)
	if e != nil {
		return nil, e
	}
	defer res.Body.Close()
	if method == "GET" && res.StatusCode == 404 {
		return nil, nil
	}
	if res.StatusCode != 200 {
		return nil, errors.New("processor outcome unresolved")
	}
	b, e := io.ReadAll(io.LimitReader(res.Body, 1048577))
	if e != nil || len(b) > 1048576 {
		return nil, errors.New("processor response too large or unreadable")
	}
	v, e := DecodeJSON(b)
	if e != nil {
		return nil, e
	}
	return object(v), nil
}
func (a *App) processorDecision(op string, v Object) (Object, error) {
	state := str(v, "state")
	valid := state == "CONFIRMED" || state == "REJECTED" || state == "PENDING"
	if op == "capture" {
		valid = state == "CONFIRMED" || state == "REJECTED"
	}
	if op == "cancel" {
		valid = state == "CANCELLED"
	}
	ref := str(v, "psp_reference")
	if !valid || ref == "" || len(ref) > 256 {
		return nil, errors.New("invalid processor decision")
	}
	if terminal, ok := v["terminal"]; ok {
		if _, valid := terminal.(bool); !valid {
			return nil, errors.New("provider terminal must be boolean")
		}
	}
	version := int64(1)
	if n, ok := v["version"]; ok {
		var e error
		version, e = number(n)
		if e != nil {
			return nil, e
		}
	}
	if version < 1 || version > 2147483647 {
		return nil, errors.New("invalid provider version")
	}
	v["version"] = version
	if state == "REJECTED" && op != "initRefund" {
		schema := "StateReasonCode"
		if op == "payout" {
			schema = "PayoutCallbackStateDetail"
		}
		if op == "capture" {
			if str(v, "detail") != "BANK_HARD_ERROR" && str(v, "detail") != "BANK_SOFT_ERROR" {
				return nil, errors.New("invalid capture decline")
			}
		} else if e := a.Contract.Validate(object(object(object(a.Contract.Spec["components"])["schemas"])[schema]), v["detail"]); e != nil {
			return nil, e
		}
	}
	if actions, ok := v["actions"]; ok {
		if op != "charge" || state != "PENDING" {
			return nil, errors.New("actions require pending charge")
		}
		if e := a.Contract.Validate(object(object(object(a.Contract.Spec["components"])["schemas"])["Actions"]), actions); e != nil {
			return nil, e
		}
		list, _ := actions.([]any)
		for _, action := range list {
			if link := str(object(action), "url"); link != "" {
				u, e := url.Parse(link)
				if e != nil || u.Host == "" || u.User != nil || (u.Scheme != "http" && u.Scheme != "https") {
					return nil, errors.New("invalid processor action URL")
				}
			}
		}
	}
	if amount, ok := v["amount"]; ok {
		if e := a.Contract.Validate(Object{"$ref": "#/components/schemas/CurrencyAmount"}, amount); e != nil {
			return nil, e
		}
	}
	return v, nil
}
func (a *App) processorExecute(r *http.Request, pattern, op, id string, b Object) (Reply, error) {
	key := r.Header.Get("X-Idempotency-Key")
	if key == "" || len(key) > 256 {
		return a.failure(pattern, 400, "VALIDATION_ERROR"), nil
	}
	logical := key
	if op == "charge" || op == "payout" || op == "initRefund" {
		logical = str(b, "id")
	}
	intent := fingerprint(a.Config.ClientID + "|" + op + "|" + logical)
	fp := fingerprint(r.URL.EscapedPath() + "\n" + encode(b))
	ctx := context.Background()
	tx, e := a.Store.Pool.Begin(ctx)
	if e != nil {
		return Reply{}, e
	}
	defer tx.Rollback(ctx)
	if _, e = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", intent); e != nil {
		return Reply{}, e
	}
	mapping, e := row(ctx, tx, "SELECT * FROM provider_keys WHERE client_id=$1 AND operation_name=$2 AND idem_key=$3", a.Config.ClientID, op, key)
	if e != nil && !notFound(e) {
		return Reply{}, e
	}
	if mapping != nil {
		if str(mapping, "fingerprint") != fp {
			return a.processorConflict(pattern, op), nil
		}
		intent = str(mapping, "intent_id")
	}
	current, e := row(ctx, tx, "SELECT * FROM provider_intents WHERE id=$1", intent)
	if e != nil && !notFound(e) {
		return Reply{}, e
	}
	fresh := false
	if current != nil && str(current, "fingerprint") != fp {
		return a.processorConflict(pattern, op), nil
	}
	if current == nil {
		if _, e = tx.Exec(ctx, "SAVEPOINT processor_validate"); e != nil {
			return Reply{}, e
		}
		validation := *a
		validation.Adapter = processorAdapter{validation: true}
		reply, e := validation.operationTx(ctx, tx, pattern, op, id, b, r)
		if e != nil {
			return Reply{}, e
		}
		if _, e = tx.Exec(ctx, "ROLLBACK TO SAVEPOINT processor_validate"); e != nil {
			return Reply{}, e
		}
		local := reply.Status >= 400
		if reply.Body != "" {
			v, _ := DecodeJSON([]byte(reply.Body))
			local = local || str(object(object(v)["state"]), "value") == "REJECTED"
		}
		var status any
		var response any
		state := "IN_FLIGHT"
		if local {
			if _, e = validation.operationTx(ctx, tx, pattern, op, id, b, r); e != nil {
				return Reply{}, e
			}
			state = "DONE"
			status = reply.Status
			response = reply.Body
		}
		request := Object{"operation": op, "template": pattern, "target": r.URL.RequestURI(), "id": id, "body": b, "idem": key, "language": locale(r.Header.Get("Accept-Language")), "config_hash": a.processorConfigHash()}
		encrypted, e := a.processorCrypt(intent, []byte(encode(request)), false)
		if e != nil {
			return Reply{}, e
		}
		resource := resourceKey(op, id, b)
		current, e = row(ctx, tx, "INSERT INTO provider_intents(id,client_id,operation_name,provider_key,resource_key,fingerprint,request_encrypted,status,attempts,lease_until,response_status,response_body) VALUES($1,$2,$3,$1,$4,$5,$6,$7,1,now()+($8*interval '1 millisecond'),$9,$10) RETURNING *", intent, a.Config.ClientID, op, resource, fp, base64.StdEncoding.EncodeToString(encrypted), state, a.Config.ProcessorLease.Milliseconds(), status, response)
		if e != nil {
			return Reply{}, e
		}
		if _, e = tx.Exec(ctx, "UPDATE provider_intents SET response_headers=$2::jsonb WHERE id=$1", intent, encode(reply.Headers)); e != nil {
			return Reply{}, e
		}
		current["response_headers"] = reply.Headers
		if !local {
			plan := Object{"operation": op, "amount": object(b["amount"])["value"], "currency": object(b["amount"])["currency"], "account": str(object(b["authorization"]), "account_id"), "payee": object(object(b["payee"])["account"])["account_id"]}
			if str(plan, "account") == "" {
				plan["account"] = "demo-account"
			}
			if op == "payout" {
				plan["account"] = str(object(b["payer"]), "id")
			}
			if op == "capture" || op == "cancel" || op == "initRefund" {
				original, e := getTransaction(ctx, tx, a.Config.ClientID, "charge", strings.TrimPrefix(resource, "charge:"))
				if e != nil {
					return Reply{}, e
				}
				plan["account"] = original.Account
				plan["original_id"] = original.ID
				plan["currency"] = original.Currency
				if op == "cancel" {
					plan["amount"] = original.Amount
				}
				if op == "capture" {
					plan["amount"] = object(b["amount_to_capture"])["value"]
				}
			}
			if _, e = tx.Exec(ctx, "SELECT reference_reserve_intent($1,$2,$3,$4::jsonb)", intent, a.Config.ClientID, resource, encode(plan)); e != nil {
				return Reply{}, e
			}
			fresh = true
		}
	}
	if _, e = tx.Exec(ctx, "INSERT INTO provider_keys(client_id,operation_name,idem_key,fingerprint,intent_id) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING", a.Config.ClientID, op, key, fp, intent); e != nil {
		return Reply{}, e
	}
	if e = tx.Commit(ctx); e != nil {
		return Reply{}, e
	}
	if current["response_status"] != nil {
		if str(current, "status") == "RECONCILE" {
			if _, e = a.Store.Pool.Exec(ctx, "UPDATE provider_intents SET status='PENDING',failures=0,next_attempt_at=now() WHERE id=$1 AND status='RECONCILE'", current["id"]); e != nil {
				return Reply{}, e
			}
		}
		return responseRow(current), nil
	}
	if fresh {
		if a.Config.ProcessorPauseAfterPrepare {
			return Reply{Status: 500}, nil
		}
		return a.processorDispatch(current, false), nil
	}
	end := time.Now().Add(a.Config.ProcessorWait)
	for {
		tx, e := a.Store.Pool.Begin(ctx)
		if e != nil {
			return Reply{}, e
		}
		latest, e := row(ctx, tx, "SELECT * FROM provider_intents WHERE id=$1", intent)
		_ = tx.Commit(ctx)
		if e != nil {
			return Reply{}, e
		}
		if latest["response_status"] != nil {
			return responseRow(latest), nil
		}
		claimed, e := a.processorClaim(intent, true)
		if e != nil {
			return Reply{}, e
		}
		if claimed != nil {
			return a.processorDispatch(claimed, true), nil
		}
		if time.Now().After(end) {
			return Reply{Status: 500}, nil
		}
		time.Sleep(25 * time.Millisecond)
	}
}
func (a *App) processorConflict(pattern, op string) Reply {
	if op == "payout" {
		return a.failure(pattern, 409, "DUPLICATE_REQUEST_ERROR")
	}
	return a.failure(pattern, 400, "VALIDATION_ERROR")
}
func (a *App) processorClaim(id string, explicit bool) (Object, error) {
	ctx := context.Background()
	tx, e := a.Store.Pool.Begin(ctx)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(ctx)
	o, e := row(ctx, tx, "UPDATE provider_intents SET generation=generation+1,attempts=attempts+1,lease_until=now()+($3*interval '1 millisecond') WHERE id=$1 AND client_id=$2 AND status<>'DONE' AND ($4 OR status<>'RECONCILE') AND (lease_until IS NULL OR lease_until<now()) RETURNING *", id, a.Config.ClientID, a.Config.ProcessorLease.Milliseconds(), explicit)
	if notFound(e) {
		return nil, nil
	}
	if e != nil {
		return nil, e
	}
	return o, tx.Commit(ctx)
}
func (a *App) processorDispatch(o Object, lookup bool) Reply {
	result, e := a.processorRun(o, lookup)
	if e == nil {
		return result
	}
	_, err := a.Store.Pool.Exec(context.Background(), "UPDATE provider_intents SET failures=failures+1,status=CASE WHEN failures+1>=$3 THEN 'RECONCILE' ELSE 'UNCERTAIN' END,lease_until=NULL,next_attempt_at=now()+($4*interval '1 millisecond'),last_error='processor_unresolved' WHERE id=$1 AND generation=$2", o["id"], o["generation"], a.Config.ProcessorMaxFailures, a.Config.ProcessorPoll.Milliseconds())
	if err != nil {
		log.Print("processor unresolved; durable lease retained")
	}
	return Reply{Status: 500}
}
func (a *App) processorRun(o Object, lookup bool) (Reply, error) {
	ctx, e := a.processorContext(o)
	if e != nil {
		return Reply{}, e
	}
	var result Object
	if lookup {
		result, e = a.processorRemote(str(o, "provider_key"), nil)
	}
	if e == nil && result == nil {
		result, e = a.processorRemote(str(o, "provider_key"), Object{"operation": ctx["operation"], "request": ctx["body"], "resource": strings.SplitN(str(o, "resource_key"), ":", 2)[1]})
	}
	if e != nil {
		return Reply{}, e
	}
	result, e = a.processorDecision(str(ctx, "operation"), result)
	if e != nil {
		return Reply{}, e
	}
	if str(object(ctx["body"]), "capture_method") == "MANUAL" && str(result, "state") == "PENDING" {
		return Reply{}, errors.New("manual authorization cannot be pending")
	}
	return a.processorFinalize(o, result)
}
func (a *App) processorFinalize(o, result Object) (Reply, error) {
	saved, e := a.processorContext(o)
	if e != nil {
		return Reply{}, e
	}
	ctx := context.Background()
	tx, e := a.Store.Pool.Begin(ctx)
	if e != nil {
		return Reply{}, e
	}
	defer tx.Rollback(ctx)
	current, e := row(ctx, tx, "SELECT * FROM provider_intents WHERE id=$1 FOR UPDATE", o["id"])
	if e != nil {
		return Reply{}, e
	}
	if integer(current, "generation") != integer(o, "generation") {
		if current["response_status"] != nil {
			return responseRow(current), nil
		}
		return Reply{Status: 500}, nil
	}
	if str(current, "provider_reference") != "" && str(current, "provider_reference") != str(result, "psp_reference") {
		return Reply{}, errors.New("provider reference changed")
	}
	if _, e = tx.Exec(ctx, "SELECT set_config('reference.intent_id',$1,true)", str(o, "id")); e != nil {
		return Reply{}, e
	}
	version := integer(result, "version")
	if current["response_status"] != nil && version <= integer(current, "last_provider_version") {
		_, e = tx.Exec(ctx, "UPDATE provider_intents SET lease_until=NULL WHERE id=$1", o["id"])
		if e != nil {
			return Reply{}, e
		}
		return responseRow(current), tx.Commit(ctx)
	}
	op := str(saved, "operation")
	kind := "charge"
	if op == "payout" {
		kind = "payout"
	}
	if op == "initRefund" {
		kind = "refund"
	}
	b := object(saved["body"])
	id := str(b, "id")
	if id == "" {
		id = str(saved, "id")
	}
	reply := Reply{}
	if current["response_status"] != nil {
		reply = responseRow(current)
		if version > integer(current, "last_provider_version") {
			t, err := getTransaction(ctx, tx, a.Config.ClientID, kind, id)
			if err != nil {
				return Reply{}, err
			}
			state := str(result, "state")
			var updated any
			if amount, ok := result["amount"]; ok {
				profile, _ := DecodeJSON([]byte(t.Profile))
				if kind != "charge" || t.State != "PENDING" || state != "CONFIRMED" || str(object(profile), "callback_mode") != "deposit" || str(object(amount), "currency") != t.Currency {
					return Reply{}, errors.New("amount adjustment requires pending deposit confirmation")
				}
				updated = object(amount)["value"]
			}
			if t.State == "PENDING" && state != "PENDING" {
				e = a.completeTransaction(ctx, tx, t, state, str(result, "detail"), updated)
			} else if kind == "refund" && t.State == "CONFIRMED" && state == "REJECTED" {
				e = a.reverseRefund(ctx, tx, id)
			} else if t.State != state && !(t.State == "AUTHORIZED" && state == "CONFIRMED") {
				e = errors.New("invalid provider transition")
			}
			if e != nil {
				return Reply{}, e
			}
		}
	} else {
		if _, ok := result["amount"]; ok {
			return Reply{}, errors.New("amount update requires an existing pending deposit")
		}
		native := *a
		native.Adapter = processorAdapter{result: result}
		r, e := http.NewRequest("POST", a.Config.PublicBaseURL[:len(a.Config.PublicBaseURL)-len(a.Config.BasePath)]+str(saved, "target"), bytes.NewReader(nil))
		if e != nil {
			return Reply{}, e
		}
		r.Header.Set("X-Idempotency-Key", str(saved, "idem"))
		r.Header.Set("Accept-Language", str(saved, "language"))
		reply, e = native.operationTx(ctx, tx, str(saved, "template"), op, str(saved, "id"), b, r)
		if e != nil {
			return Reply{}, e
		}
		expected := 200
		if op == "initRefund" {
			expected = 201
		}
		if str(result, "state") == "REJECTED" {
			if op == "capture" {
				expected = 422
			}
			if op == "payout" {
				expected = 422
				if d := str(result, "detail"); d == "VALIDATION_ERROR" || d == "INVALID_PAYMENT_DETAILS_ERROR" || d == "INSUFFICIENT_BALANCE" {
					expected = 400
				}
			}
		}
		if reply.Status != expected {
			return Reply{}, errors.New("provider outcome must reconcile; native preconditions changed")
		}

		if reply.Status < 300 && reply.Body != "" && (op == "charge" || op == "payout" || op == "initRefund") {
			v, e := DecodeJSON([]byte(reply.Body))
			if e != nil {
				return Reply{}, e
			}
			out := object(v)
			field := "psp_reference"
			if kind == "refund" {
				field = "merchant_reference"
			}
			out[field] = result["psp_reference"]
			if actions, ok := result["actions"]; ok {
				out["actions"] = actions
			}
			if e = a.Contract.Response(str(saved, "template"), "post", reply.Status, out); e != nil {
				return Reply{}, e
			}
			reply.Body = encode(out)
			if _, e = tx.Exec(ctx, "UPDATE transactions SET psp_reference=$4,response_body=$5 WHERE client_id=$1 AND kind=$2 AND id=$3", a.Config.ClientID, kind, id, result["psp_reference"], reply.Body); e != nil {
				return Reply{}, e
			}
			if _, e = tx.Exec(ctx, "UPDATE operations SET response_body=$4 WHERE client_id=$1 AND operation_name=$2 AND idem_key=$3", a.Config.ClientID, op, saved["idem"], reply.Body); e != nil {
				return Reply{}, e
			}
		}
	}
	completion := "DONE"
	state := "DONE"
	if str(result, "state") == "PENDING" {
		completion = "PROVIDER_WAIT"
		state = "PENDING"
	}
	if kind == "refund" && str(result, "state") == "CONFIRMED" && result["terminal"] == false {
		state = "PENDING"
	}
	if _, e = tx.Exec(ctx, "UPDATE jobs SET status=$4 WHERE client_id=$1 AND tx_kind=$2 AND tx_id=$3 AND job_type='COMPLETION' AND status IN ('PENDING','WAITING','PROVIDER_WAIT')", a.Config.ClientID, kind, id, completion); e != nil {
		return Reply{}, e
	}
	if _, e = tx.Exec(ctx, "UPDATE provider_intents SET status=$3,last_provider_version=GREATEST(last_provider_version,$4),provider_reference=$5,lease_until=NULL,next_attempt_at=now()+($6*interval '1 millisecond'),response_status=COALESCE(response_status,$7),response_body=COALESCE(response_body,$8),response_headers=CASE WHEN response_status IS NULL THEN $9::jsonb ELSE response_headers END,last_error=NULL,failures=0,updated_at=now() WHERE id=$1 AND generation=$2", o["id"], o["generation"], state, version, result["psp_reference"], a.Config.ProcessorPoll.Milliseconds(), reply.Status, reply.Body, encode(reply.Headers)); e != nil {
		return Reply{}, e
	}
	if str(result, "state") == "PENDING" {
		_, e = tx.Exec(ctx, "UPDATE intent_reservations SET debit_e5=0 WHERE intent_id=$1", o["id"])
	} else {
		_, e = tx.Exec(ctx, "DELETE FROM intent_reservations WHERE intent_id=$1", o["id"])
		if e == nil {
			_, e = tx.Exec(ctx, "DELETE FROM resource_claims WHERE intent_id=$1", o["id"])
		}
	}
	if e != nil {
		return Reply{}, e
	}
	return reply, tx.Commit(ctx)
}
func (a *App) RunProcessor(ctx context.Context) {
	ticker := time.NewTicker(a.Config.ProcessorPoll)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			rows, e := a.Store.Pool.Query(ctx, "SELECT id FROM provider_intents WHERE client_id=$1 AND status IN ('IN_FLIGHT','UNCERTAIN','PENDING') AND next_attempt_at<=now() AND (lease_until IS NULL OR lease_until<now()) ORDER BY next_attempt_at LIMIT 4", a.Config.ClientID)
			if e != nil {
				continue
			}
			var ids []string
			for rows.Next() {
				var id string
				if rows.Scan(&id) == nil {
					ids = append(ids, id)
				}
			}
			rows.Close()
			for _, id := range ids {
				o, e := a.processorClaim(id, false)
				if e == nil && o != nil {
					a.processorDispatch(o, true)
				}
			}
		}
	}
}
func (a *App) processorEvent(w http.ResponseWriter, r *http.Request) {
	if a.Config.ProcessorMode != "http" || r.Method != "POST" || subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+a.Config.ProcessorToken)) != 1 {
		w.WriteHeader(404)
		return
	}
	raw, e := io.ReadAll(io.LimitReader(r.Body, 1048577))
	if e != nil || len(raw) > 1048576 {
		w.WriteHeader(400)
		return
	}
	v, e := DecodeJSON(raw)
	if e != nil {
		w.WriteHeader(400)
		return
	}
	value := object(v)
	ctx := context.Background()
	tx, e := a.Store.Pool.Begin(ctx)
	if e != nil {
		w.WriteHeader(500)
		return
	}
	defer tx.Rollback(ctx)
	o, e := row(ctx, tx, "SELECT * FROM provider_intents WHERE client_id=$1 AND provider_key=$2 FOR UPDATE", a.Config.ClientID, str(value, "provider_key"))
	if notFound(e) {
		w.WriteHeader(404)
		return
	}
	if e != nil {
		w.WriteHeader(500)
		return
	}
	result, e := a.processorDecision(str(o, "operation_name"), value)
	if e != nil {
		w.WriteHeader(400)
		return
	}
	if str(o, "provider_reference") != "" && str(o, "provider_reference") != str(result, "psp_reference") {
		w.WriteHeader(400)
		return
	}
	if integer(result, "version") <= integer(o, "last_provider_version") {
		w.WriteHeader(204)
		return
	}
	if until, ok := o["lease_until"].(time.Time); ok && until.After(time.Now()) {
		w.WriteHeader(503)
		return
	}
	o, e = row(ctx, tx, "UPDATE provider_intents SET generation=generation+1,lease_until=now()+($2*interval '1 millisecond') WHERE id=$1 RETURNING *", o["id"], a.Config.ProcessorLease.Milliseconds())
	if e != nil {
		w.WriteHeader(500)
		return
	}
	if e = tx.Commit(ctx); e != nil {
		w.WriteHeader(500)
		return
	}
	if _, e = a.processorFinalize(o, result); e != nil {
		_, _ = a.Store.Pool.Exec(ctx, "UPDATE provider_intents SET lease_until=NULL,status='RECONCILE',last_error='event_unresolved' WHERE id=$1 AND generation=$2", o["id"], o["generation"])
		w.WriteHeader(500)
		return
	}
	w.WriteHeader(204)
}
