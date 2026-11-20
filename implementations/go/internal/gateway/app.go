package gateway

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

type Reply struct {
	Status  int
	Body    string
	Headers map[string]string
}
type App struct {
	Features ReferenceFeatures
	Config   Config
	Store    *Store
	Contract Contract
	Tokens   *UberTokenProvider
	Client   *http.Client
	Adapter  PspAdapter
	Wallet   WalletProvider
}

func New(ctx context.Context, c Config) (*App, error) {
	return NewWithDependencies(ctx, c, DemoPspAdapter{}, DemoWalletProvider{})
}

// NewWithDependencies accepts test/local ports only. No injected external adapter
// may run remote side effects while the synthetic ledger transaction is locked.
func NewWithDependencies(ctx context.Context, c Config, adapter PspAdapter, wallet WalletProvider) (*App, error) {
	if adapter == nil || wallet == nil || !adapter.LocalOnly() || !wallet.LocalOnly() {
		return nil, errors.New("only local synthetic adapter and wallet implementations are supported")
	}

	b, e := os.ReadFile(c.ContractFile)
	if e != nil {
		return nil, e
	}
	v, e := DecodeJSON(b)
	if e != nil {
		return nil, e
	}
	if c.StrictAccountName {
		schema := object(object(object(v)["components"])["schemas"])["Account"]
		object(schema)["required"] = append(object(schema)["required"].([]any), "name")
	}
	features, e := LoadFeatures()
	if e != nil {
		return nil, e
	}
	store, e := OpenStore(ctx, c)
	if e != nil {
		return nil, e
	}
	return &App{Features: features, Config: c, Store: store, Contract: Contract{Spec: object(v)}, Tokens: &UberTokenProvider{entries: map[string]cachedToken{}}, Client: httpClient(), Adapter: adapter, Wallet: wallet}, nil
}
func (a *App) Close() { a.Store.Pool.Close() }
func (a *App) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Language", locale(r.Header.Get("Accept-Language")))
	defer func() {
		if recover() != nil {
			log.Print("request failure")
			http.Error(w, "", 500)
		}
	}()
	if r.URL.Path == "/adapter/events" {
		a.processorEvent(w, r)
		return
	}
	if r.URL.Path == "/health" {
		if a.Store.Pool.Ping(r.Context()) != nil {
			w.WriteHeader(503)
			return
		}
		a.write(w, Reply{200, `{"status":"ok","language":"go"}`, nil})
		return
	}
	if strings.HasPrefix(r.URL.Path, "/_test/") {
		a.testControl(w, r)
		return
	}
	if r.URL.Path == "/demo/consent" {
		a.consent(w, r)
		return
	}
	if r.URL.Path == "/demo/action" {
		a.action(w, r)
		return
	}
	if !strings.HasPrefix(r.URL.Path, a.Config.BasePath+"/") {
		w.WriteHeader(404)
		return
	}
	path := strings.TrimPrefix(r.URL.EscapedPath(), a.Config.BasePath)
	pattern, op, id := route(path, r.Method)
	if op == "" {
		w.WriteHeader(404)
		return
	}
	if op == "authorize" {
		a.authorize(w, r)
		return
	}
	raw, e := io.ReadAll(http.MaxBytesReader(w, r.Body, 1048576))
	if e != nil {
		a.write(w, a.failure(pattern, 400, "VALIDATION_ERROR"))
		return
	}
	if e = VerifySignature(r, raw, a.Config.Keys, time.Now(), a.Config.MaxSkew); e != nil {
		status := 400
		if pattern == "/charge" || pattern == "/payout" || pattern == "/balance" {
			status = 401
		}
		a.write(w, a.failure(pattern, status, "VALIDATION_ERROR"))
		return
	}
	opdef := a.Contract.operation(pattern, strings.ToLower(r.Method))
	parameters, _ := opdef["parameters"].([]any)
	for _, entry := range parameters {
		param := object(entry)
		if ref := str(param, "$ref"); ref != "" {
			param = a.Contract.resolve(ref)
		}
		if str(param, "in") == "header" && param["required"] == true && r.Header.Get(str(param, "name")) == "" {
			a.write(w, a.failure(pattern, 400, "VALIDATION_ERROR"))
			return
		}
	}
	if strings.ToLower(strings.TrimSpace(strings.Split(r.Header.Get("Content-Type"), ";")[0])) != "application/json" {
		a.write(w, a.failure(pattern, 400, "VALIDATION_ERROR"))
		return
	}
	if _, err := url.ParseQuery(r.URL.RawQuery); err != nil {
		a.write(w, a.failure(pattern, 400, "VALIDATION_ERROR"))
		return
	}
	body := Object{}
	if r.Method == "POST" {
		if !strings.HasPrefix(strings.ToLower(r.Header.Get("Content-Type")), "application/json") {
			a.write(w, a.failure(pattern, 400, "VALIDATION_ERROR"))
			return
		}
		v, e := DecodeJSON(raw)
		if e != nil {
			a.write(w, a.failure(pattern, 400, "VALIDATION_ERROR"))
			return
		}
		var ok bool
		body, ok = v.(map[string]any)
		if !ok || a.Contract.Request(pattern, "post", body) != nil {
			a.write(w, a.failure(pattern, 400, "VALIDATION_ERROR"))
			return
		}
	}
	if op == "createToken" {
		a.token(w, r, body)
		return
	}
	ctx := r.Context()
	tx, e := a.Store.Begin(ctx)
	if e != nil {
		a.write(w, a.failure(pattern, 500, "PAYMENT_PSP_ERROR"))
		return
	}
	defer tx.Rollback(ctx)
	if e = a.verifyBearer(ctx, tx, r, op, body); e != nil {
		if !errors.Is(e, errBearer) {
			a.write(w, a.failure(pattern, 500, "PAYMENT_PSP_ERROR"))
			return
		}
		status := 400
		if pattern == "/charge" || pattern == "/balance" {
			status = 401
		}
		a.write(w, a.failure(pattern, status, "VALIDATION_ERROR"))
		return
	}
	mutating := moneyOperation(op)
	if mutating && a.Config.ProcessorMode == "http" {
		if e = tx.Commit(ctx); e != nil {
			a.write(w, a.failure(pattern, 500, ""))
			return
		}
		reply, err := a.processorExecute(r, pattern, op, id, body)
		if err != nil {
			a.write(w, a.failure(pattern, 500, ""))
		} else {
			a.write(w, reply)
		}
		return
	}
	reply, e := a.operationTx(ctx, tx, pattern, op, id, body, r)
	if e != nil {
		log.Printf("database operation failed (%s)", op)
		a.write(w, a.failure(pattern, 500, ""))
		return
	}
	if e = tx.Commit(ctx); e != nil {
		a.write(w, a.failure(pattern, 500, ""))
		return
	}
	a.write(w, reply)
}
func moneyOperation(op string) bool {
	return op == "charge" || op == "capture" || op == "cancel" || op == "initRefund" || op == "payout"
}
func resourceKey(op, id string, b Object) string {
	if op == "payout" {
		return "payout:" + str(b, "id")
	}
	if op == "initRefund" {
		return "charge:" + str(b, "original_transaction_id")
	}
	if op == "capture" || op == "cancel" {
		return "charge:" + id
	}
	return "charge:" + str(b, "id")
}
func (a *App) operationTx(ctx context.Context, tx pgx.Tx, pattern, op, id string, body Object, r *http.Request) (Reply, error) {
	mutating := moneyOperation(op)
	key := r.Header.Get("X-Idempotency-Key")
	hash := fingerprint(strings.TrimPrefix(r.URL.EscapedPath(), a.Config.BasePath) + ":" + encode(body))
	if mutating {
		if key == "" || len(key) > 256 {
			return a.failure(pattern, 400, "VALIDATION_ERROR"), nil
		}
		var oldhash, oldbody string
		var oldheaders []byte
		var status int
		err := tx.QueryRow(ctx, "SELECT fingerprint,status,response_body,response_headers FROM operations WHERE client_id=$1 AND operation_name=$2 AND idem_key=$3", a.Config.ClientID, op, key).Scan(&oldhash, &status, &oldbody, &oldheaders)
		if err == nil {
			if oldhash != hash {
				return a.failure(pattern, map[bool]int{true: 409, false: 400}[op == "payout"], map[bool]string{true: "DUPLICATE_REQUEST_ERROR", false: "VALIDATION_ERROR"}[op == "payout"]), nil
			}
			var headers map[string]string
			if e := json.Unmarshal(oldheaders, &headers); e != nil {
				return Reply{}, e
			}
			return Reply{status, oldbody, headers}, nil
		}
		if !notFound(err) {
			return Reply{}, err
		}
		if _, err = tx.Exec(ctx, "SELECT reference_assert_unclaimed($1,$2)", a.Config.ClientID, resourceKey(op, id, body)); err != nil {
			return Reply{}, err
		}
	}
	reply, err := a.execute(ctx, tx, op, id, body, r, hash)
	if err != nil {
		return Reply{}, err
	}
	reply.Headers = map[string]string{"Content-Language": locale(r.Header.Get("Accept-Language"))}
	if reply.Status >= 400 && reply.Body != "" {
		v, e := DecodeJSON([]byte(reply.Body))
		if e != nil {
			return Reply{}, e
		}
		out := object(v)
		out["error_description"] = a.Features.Text(reply.Headers["Content-Language"], "invalid_request")
		reply.Body = encode(out)
	}
	if reply.Body != "" {
		v, err := DecodeJSON([]byte(reply.Body))
		if err != nil {
			return Reply{}, err
		}
		if err = a.Contract.Response(pattern, strings.ToLower(r.Method), reply.Status, v); err != nil {
			return Reply{}, err
		}
	}
	if mutating && reply.Status < 500 {
		_, err = tx.Exec(ctx, "INSERT INTO operations(client_id,operation_name,idem_key,fingerprint,status,response_body,tx_id,response_headers) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)", a.Config.ClientID, op, key, hash, reply.Status, reply.Body, str(body, "id"), encode(reply.Headers))
	}
	return reply, err
}

func (a *App) write(w http.ResponseWriter, r Reply) {
	for k, v := range r.Headers {
		w.Header().Set(k, v)
	}
	if r.Status >= 400 && r.Body != "" {
		v, err := DecodeJSON([]byte(r.Body))
		if err == nil {
			body := object(v)
			if _, present := body["error_description"]; !present {
				body["error_description"] = a.Features.Text(w.Header().Get("Content-Language"), "invalid_request")
			}
			r.Body = encode(body)
		}
	}
	if r.Body != "" {
		w.Header().Set("Content-Type", "application/json")
	}
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(r.Status)
	if r.Body != "" {
		_, _ = io.WriteString(w, r.Body)
	}
}
func route(path, method string) (string, string, string) {
	if method == "GET" {
		switch path {
		case "/find-transaction":
			return path, "findTransaction", ""
		case "/authorize":
			return path, "authorize", ""
		}
		return "", "", ""
	}
	if method != "POST" {
		return "", "", ""
	}
	switch path {
	case "/charge":
		return path, "charge", ""
	case "/refund":
		return path, "initRefund", ""
	case "/payout":
		return path, "payout", ""
	case "/balance":
		return path, "getBalance", ""
	case "/token":
		return path, "createToken", ""
	}
	parts := strings.Split(path, "/")
	if len(parts) == 4 && parts[1] == "charge" && parts[2] != "" && (parts[3] == "capture" || parts[3] == "cancel") {
		decoded, err := url.PathUnescape(parts[2])
		if err != nil || decoded == "" {
			return "", "", ""
		}
		return "/charge/{id}/" + parts[3], parts[3], decoded
	}
	return "", "", ""
}
func (a *App) failure(path string, status int, detail string) Reply {
	method := "post"
	if path == "/find-transaction" {
		method = "get"
	}
	response := object(object(a.Contract.operation(path, method)["responses"])[fmt.Sprint(status)])
	if status >= 500 && len(response) == 0 {
		return Reply{Status: 500}
	}
	if path == "/token" && status < 500 {
		allowed := map[string]bool{"invalid_request": true, "invalid_client": true, "invalid_grant": true, "invalid_scope": true, "unsupported_grant_type": true}
		if !allowed[detail] {
			detail = "invalid_request"
		}
		return Reply{400, encode(Object{"error": detail}), nil}
	}
	if ref := str(response, "$ref"); ref != "" {
		response = a.Contract.resolve(ref)
	}
	schema := object(object(response["content"])["application/json"])
	if len(schema) == 0 {
		return Reply{Status: status}
	}
	return Reply{status, encode(Object{"detail": detail}), nil}
}
func amount(o Object) (int64, string, error) {
	v, e := number(o["value"])
	cur := str(o, "currency")
	if e != nil || v <= 0 || len(cur) != 3 {
		return 0, "", errors.New("invalid amount")
	}
	return v, cur, nil
}
func knownFunding(code string) bool {
	f, e := LoadFeatures()
	if e != nil {
		return false
	}
	_, e = f.Method(code, "", "")
	return e == nil
}

func (a *App) execute(ctx context.Context, tx pgx.Tx, op, id string, b Object, r *http.Request, hash string) (Reply, error) {
	switch op {
	case "charge", "payout":
		return a.create(ctx, tx, op, b, hash, locale(r.Header.Get("Accept-Language")))
	case "capture", "cancel":
		return a.finishManual(ctx, tx, op, id, b)
	case "initRefund":
		return a.refund(ctx, tx, b, hash)
	case "getBalance":
		p, err := a.Features.Method(str(b, "funding_method"), str(b, "country_iso2"), "")
		if err != nil {
			return a.failure("/balance", 400, "VALIDATION_ERROR"), nil
		}
		cur := p["currencies"].([]any)[0].(string)
		av, bal, e := accountBalance(ctx, tx, a.Config.ClientID, str(b, "account_id"), cur)
		if notFound(e) {
			return a.failure("/balance", 404, ""), nil
		}
		if e != nil {
			return Reply{}, e
		}
		if !knownFunding(str(b, "funding_method")) {
			return a.failure("/balance", 400, ""), nil
		}
		if err := validatePaymentData(b, 0); err != nil {
			return a.failure("/balance", 400, "VALIDATION_ERROR"), nil
		}
		result, err := a.Adapter.GetBalance(ctx, BalanceQuery{Request: b, Account: str(b, "account_id"), Currency: cur, AvailableE5: av, BalanceE5: bal})
		if err != nil {
			return Reply{}, err
		}
		if result.AvailableE5 < 0 || result.BalanceE5 < result.AvailableE5 {
			return Reply{}, errors.New("invalid adapter balance")
		}
		out := Object{"available_balance": Object{"value": result.AvailableE5, "currency": result.Currency}, "balance": Object{"value": result.BalanceE5, "currency": result.Currency}}
		if p["itemized"] == true {
			out["itemized_balances"] = []any{Object{"display_name": "Meal voucher", "available_balance": out["available_balance"], "balance": out["balance"]}}
		}
		return Reply{200, encode(out), nil}, nil
	case "findTransaction":
		id = r.URL.Query().Get("id")
		if id == "" || len(r.URL.Query()["id"]) != 1 {
			return Reply{Status: 400}, nil
		}
		t, e := getTransaction(ctx, tx, a.Config.ClientID, "charge", id)
		if notFound(e) {
			t, e = getTransaction(ctx, tx, a.Config.ClientID, "refund", id)
		}
		if notFound(e) {
			return Reply{Status: 404}, nil
		}
		if e != nil {
			return Reply{}, e
		}
		if v := r.URL.Query().Get("merchant_reference"); v != "" && v != t.Ref {
			return Reply{Status: 404}, nil
		}
		provider, err := a.Adapter.LookupOperation(ctx, OperationQuery{ProviderKey: fingerprint(a.Config.ClientID + ":" + t.Kind + ":" + t.ID), Reference: t.Ref, State: t.State})
		if err != nil {
			return Reply{}, err
		}
		t.State = provider.State
		out := Object{"id": t.ID, "merchant_reference": t.Ref, "status": t.State, "amount": Object{"value": t.Amount, "currency": t.Currency}}
		if t.State == "CONFIRMED" {
			out["confirmed_at"] = t.Updated.UTC().Format(time.RFC3339Nano)
		}
		return Reply{200, encode(out), nil}, nil
	}
	return Reply{Status: 404}, nil
}
func (a *App) create(ctx context.Context, tx pgx.Tx, kind string, b Object, hash string, language string) (Reply, error) {
	path := "/" + kind
	source := str(object(b["source"]), "id")
	if kind == "payout" {
		source = str(object(b["payer"]), "id")
	}
	if source == "" {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	id := str(b, "id")
	if id == "" || len(id) > 512 {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	old, e := getTransaction(ctx, tx, a.Config.ClientID, kind, id)
	if e == nil {
		if old.Hash == hash {
			return Reply{old.Status, old.Response, nil}, nil
		}
		return a.failure(path, map[bool]int{true: 409, false: 400}[kind == "payout"], map[bool]string{true: "DUPLICATE_REQUEST_ERROR", false: "VALIDATION_ERROR"}[kind == "payout"]), nil
	}
	if !notFound(e) {
		return Reply{}, e
	}
	n, currency, e := amount(object(b["amount"]))
	funding := str(b, "funding_method")
	profile, profileError := a.Features.Method(funding, str(b, "country_iso2"), currency)
	capture := str(b, "capture_method")
	if capture == "" {
		capture = "AUTOMATIC"
	}
	if e != nil || profileError != nil || capture == "MANUAL" && (str(profile, "behavior") == "pending" || str(profile, "behavior") == "pending_reject" || str(profile, "behavior") == "webview" || str(profile, "behavior") == "qr") {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	account := "demo-account"
	if kind == "payout" {
		account = str(object(b["payer"]), "id")
		payee := str(object(object(b["payee"])["account"]), "account_id")
		declines := map[string]string{"inactive-account": "INACTIVE_ACCOUNT_ERROR", "fraud-account": "PAYMENT_FRAUD_ERROR", "hard-decline-account": "PAYMENT_PSP_HARD_DECLINE_ERROR", "bank-hard-account": "BANK_HARD_ERROR", "merchant-rejected-account": "MERCHANT_REJECTED", "network-error-account": "PAYMENT_NETWORK_ERROR", "psp-error-account": "PAYMENT_PSP_ERROR", "payment-error-account": "PAYMENT_ERROR"}
		if payee == "invalid-account" || str(object(object(b["payee"])["document"]), "id") == "invalid" {
			return a.failure(path, 400, "INVALID_PAYMENT_DETAILS_ERROR"), nil
		}
		if detail := declines[payee]; detail != "" {
			return a.failure(path, 422, detail), nil
		}
		if _, _, err := accountBalance(ctx, tx, a.Config.ClientID, payee, currency); err != nil {
			if notFound(err) {
				return a.failure(path, 400, "INVALID_PAYMENT_DETAILS_ERROR"), nil
			}
			return Reply{}, err
		}
	}
	if kind == "charge" && str(object(b["authorization"]), "account_id") != "" {
		account = str(object(b["authorization"]), "account_id")
	}
	available, _, e := accountBalance(ctx, tx, a.Config.ClientID, account, currency)
	if notFound(e) {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	if e != nil {
		return Reply{}, e
	}
	t := Transaction{Kind: kind, ID: id, Ref: "psp-" + fingerprint(kind + ":" + id)[:24], Account: account, Amount: n, Currency: currency, Capture: capture, Funding: funding, Hash: hash, Status: 200, State: "CONFIRMED"}
	if detail := str(profile, "detail"); detail != "" {
		key := "charge_reasons"
		if kind == "payout" {
			key = "payout_reasons"
		}
		valid := false
		for _, code := range a.Features.Data[key].([]any) {
			if code == detail {
				valid = true
			}
		}
		if !valid {
			return a.failure(path, 400, "VALIDATION_ERROR"), nil
		}
	}
	command := PaymentCommand{Request: b, ID: id, Account: account, Currency: currency, FundingMethod: funding, CaptureMethod: capture, ProviderKey: fingerprint(a.Config.ClientID + ":" + kind + ":" + id), AmountE5: n, AvailableE5: available}
	if err := validatePaymentData(b, n); err != nil {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	var result ProviderResult
	if kind == "charge" {
		result, e = a.Adapter.Charge(ctx, command)
	} else {
		result, e = a.Adapter.Payout(ctx, command)
	}
	if e != nil {
		return Reply{}, e
	}
	t.State, t.Detail = result.State, result.Detail
	if kind == "payout" && t.State == "REJECTED" {
		status := 422
		if t.Detail == "VALIDATION_ERROR" || t.Detail == "INVALID_PAYMENT_DETAILS_ERROR" || t.Detail == "INSUFFICIENT_BALANCE" {
			status = 400
		}
		return a.failure(path, status, t.Detail), nil
	}

	wire := t.State
	if wire == "AUTHORIZED" {
		wire = "CONFIRMED"
	}
	state := Object{"value": wire}
	if t.Detail != "" {
		state["detail"] = t.Detail
	}
	out := Object{"state": state, "psp_reference": t.Ref}
	actionToken := ""
	if a.Config.ProcessorMode != "http" && kind == "charge" && t.State == "PENDING" && (str(profile, "behavior") == "webview" || str(profile, "behavior") == "qr") {
		actionToken, e = a.putToken(ctx, tx, "ACTION", account, "", "", encode(Object{"id": id, "kind": kind, "locale": language, "profile": profile}), a.Config.ActionTTL)
		if e != nil {
			return Reply{}, e
		}
	}
	if actionToken != "" && str(profile, "behavior") == "webview" {
		out["actions"] = []any{Object{"type": "web_view_user_input_action", "url": strings.TrimSuffix(a.Config.PublicBaseURL, a.Config.BasePath) + "/demo/action?token=" + urlQueryEscape(actionToken)}}
	}
	if actionToken != "" && str(profile, "behavior") == "qr" {
		out["actions"] = []any{Object{"type": "qr_code_input_action", "code": "local-qr:" + actionToken, "expiry_time_millis": time.Now().Add(a.Config.ActionTTL).UnixMilli()}}
	}
	t.Profile = encode(profile)
	t.Source = str(object(b["source"]), "id")
	if kind == "payout" {
		t.Source = account
		t.Payee = str(object(object(b["payee"])["account"]), "account_id")
	}
	t.Response = encode(out)
	if t.State != "REJECTED" {
		balDelta := int64(0)
		if t.State == "CONFIRMED" {
			balDelta = -n
		}
		if e = adjust(ctx, tx, a.Config.ClientID, account, currency, -n, balDelta); e != nil {
			return Reply{}, e
		}
	}
	if e = insertTransaction(ctx, tx, a.Config.ClientID, t); e != nil {
		return Reply{}, e
	}
	items, _ := b["payment_data"].([]any)
	for _, item := range items {
		if str(object(item), "type") == "order_payment_data" {
			if _, e = tx.Exec(ctx, "UPDATE transactions SET order_id=$4 WHERE client_id=$1 AND kind=$2 AND id=$3", a.Config.ClientID, kind, id, str(object(item), "order_id")); e != nil {
				return Reply{}, e
			}
		}
	}
	if kind == "payout" && t.State == "CONFIRMED" {
		if e = adjust(ctx, tx, a.Config.ClientID, t.Payee, currency, n, n); e != nil {
			return Reply{}, e
		}
	}
	if t.State == "PENDING" {
		status := "PENDING"
		if actionToken != "" {
			status = "WAITING"
		}
		state := "CONFIRMED"
		if result.FinalState != "" {
			state = result.FinalState
		}
		e = queue(ctx, tx, a.Config.ClientID, kind, id, "COMPLETION", "", Object{"state": state, "detail": result.FinalDetail, "profile": profile, "locale": language, "return_url": str(b, "return_url"), "expires_at": time.Now().Add(a.Config.ActionTTL).UnixMilli()}, status, a.Config.AsyncDelay)
	}
	return Reply{200, t.Response, nil}, e
}
func (a *App) finishManual(ctx context.Context, tx pgx.Tx, op, id string, b Object) (Reply, error) {
	path := "/charge/{id}/" + op
	t, e := getTransaction(ctx, tx, a.Config.ClientID, "charge", id)
	if notFound(e) {
		return a.failure(path, 404, "RESOURCE_NOT_FOUND"), nil
	}
	if e != nil {
		return Reply{}, e
	}
	expired := false
	e = tx.QueryRow(ctx, "SELECT expires_at<=now() FROM transactions WHERE client_id=$1 AND kind='charge' AND id=$2", a.Config.ClientID, id).Scan(&expired)
	if e != nil && t.State == "AUTHORIZED" {
		return Reply{}, e
	}
	var claimed bool
	if e = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM resource_claims WHERE client_id=$1 AND resource_key=$2)", a.Config.ClientID, "charge:"+id).Scan(&claimed); e != nil {
		return Reply{}, e
	}
	if expired && t.State == "AUTHORIZED" && !claimed {
		if e = a.cancelAuthorization(ctx, tx, t); e != nil {
			return Reply{}, e
		}
		t.State = "CANCELLED"
	}
	if t.State != "AUTHORIZED" {
		return a.failure(path, 409, "INVALID_CHARGE_STATE"), nil
	}
	if ref := str(b, "psp_reference"); ref != "" && ref != t.Ref {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	if op == "cancel" {
		if err := a.Adapter.Cancel(ctx, t.Ref); err != nil {
			return Reply{}, err
		}
		return Reply{Status: 200}, a.cancelAuthorization(ctx, tx, t)
	}
	if t.Source == "" {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	n, c, e := amount(object(b["amount_to_capture"]))
	if e != nil || c != t.Currency {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	if n > t.Amount {
		return a.failure(path, 400, "EXCEEDS_CAPTURE_LIMIT"), nil
	}
	result, err := a.Adapter.Capture(ctx, CaptureCommand{FundingMethod: t.Funding, ID: id, ProviderKey: fingerprint(a.Config.ClientID + ":capture:" + id), Currency: c, AuthorizedCurrency: t.Currency, AmountE5: n, AuthorizedE5: t.Amount})
	if err != nil {
		return Reply{}, err
	}
	if result.State == "REJECTED" {
		if e = adjust(ctx, tx, a.Config.ClientID, t.Account, t.Currency, t.Amount, 0); e != nil {
			return Reply{}, e
		}
		if _, e = tx.Exec(ctx, "UPDATE transactions SET state='REJECTED',detail=$3,updated_at=now() WHERE client_id=$1 AND kind='charge' AND id=$2", a.Config.ClientID, id, result.Detail); e != nil {
			return Reply{}, e
		}
		return a.failure(path, 422, result.Detail), nil
	}
	if result.State != "CONFIRMED" {
		return Reply{}, errors.New("unexpected capture decision")
	}
	if e = adjust(ctx, tx, a.Config.ClientID, t.Account, t.Currency, t.Amount-n, -n); e != nil {
		return Reply{}, e
	}
	_, e = tx.Exec(ctx, "UPDATE transactions SET state='CONFIRMED',amount_e5=$3,updated_at=now() WHERE client_id=$1 AND kind='charge' AND id=$2", a.Config.ClientID, id, n)
	return Reply{Status: 200}, e
}
func (a *App) cancelAuthorization(ctx context.Context, tx pgx.Tx, t Transaction) error {
	if e := adjust(ctx, tx, a.Config.ClientID, t.Account, t.Currency, t.Amount, 0); e != nil {
		return e
	}
	_, e := tx.Exec(ctx, "UPDATE transactions SET state='CANCELLED',updated_at=now() WHERE client_id=$1 AND kind='charge' AND id=$2", a.Config.ClientID, t.ID)
	return e
}
func (a *App) refund(ctx context.Context, tx pgx.Tx, b Object, hash string) (Reply, error) {
	id := str(b, "id")
	if id == "" {
		return Reply{Status: 400}, nil
	}
	old, e := getTransaction(ctx, tx, a.Config.ClientID, "refund", id)
	if e == nil {
		if old.Hash == hash {
			return Reply{old.Status, old.Response, nil}, nil
		}
		return Reply{Status: 400}, nil
	}
	if !notFound(e) {
		return Reply{}, e
	}
	original, e := getTransaction(ctx, tx, a.Config.ClientID, "charge", str(b, "original_transaction_id"))
	if notFound(e) {
		return Reply{Status: 404}, nil
	}
	if e != nil {
		return Reply{}, e
	}
	if original.Source == "" {
		return Reply{Status: 400}, nil
	}
	n, c, e := amount(object(b["amount"]))
	if e != nil || c != original.Currency || original.State != "CONFIRMED" || str(b, "original_merchant_reference") != original.Ref {
		return Reply{Status: 400}, nil
	}
	var used string
	e = tx.QueryRow(ctx, "SELECT COALESCE(sum(amount_e5),0)::text FROM transactions WHERE client_id=$1 AND kind='refund' AND original_id=$2 AND state IN ('PENDING','CONFIRMED')", a.Config.ClientID, original.ID).Scan(&used)
	if e != nil {
		return Reply{}, e
	}
	spent, e := number(json.Number(used))
	if e != nil || spent > original.Amount || n > original.Amount-spent {
		return Reply{Status: 400}, nil
	}
	_, balance, e := accountBalance(ctx, tx, a.Config.ClientID, original.Account, c)
	if e != nil {
		return Reply{}, e
	}
	if balance > math.MaxInt64-n {
		return Reply{Status: 400}, nil
	}
	t := Transaction{Source: original.Source, Profile: original.Profile, Kind: "refund", ID: id, Ref: "psp-" + fingerprint("refund:" + id)[:24], Original: original.ID, Account: original.Account, Amount: n, Currency: c, Capture: "AUTOMATIC", Funding: original.Funding, State: "CONFIRMED", Hash: hash, Status: 201}
	result, err := a.Adapter.Refund(ctx, PaymentCommand{Request: b, ID: id, Account: original.Account, Currency: c, FundingMethod: original.Funding, ProviderKey: fingerprint(a.Config.ClientID + ":refund:" + id), AmountE5: n})
	if err != nil {
		return Reply{}, err
	}
	t.State = result.State
	t.Response = encode(Object{"status": t.State, "merchant_reference": t.Ref})
	if e = insertTransaction(ctx, tx, a.Config.ClientID, t); e != nil {
		return Reply{}, e
	}
	if t.State == "CONFIRMED" {
		e = adjust(ctx, tx, a.Config.ClientID, t.Account, c, n, n)
	} else if t.State == "PENDING" {
		e = queue(ctx, tx, a.Config.ClientID, "refund", id, "COMPLETION", "", Object{"state": map[bool]string{true: result.FinalState, false: "CONFIRMED"}[result.FinalState != ""], "detail": result.FinalDetail}, "PENDING", a.Config.AsyncDelay)
	}
	return Reply{201, t.Response, nil}, e
}

var errBearer = errors.New("invalid bearer token")

func (a *App) verifyBearer(ctx context.Context, tx pgx.Tx, r *http.Request, op string, b Object) error {
	scope := "charge"
	if op == "initRefund" {
		scope = "refund"
	}
	if op == "getBalance" {
		scope = "get_balance"
	}
	if op == "findTransaction" || op == "payout" {
		return nil
	}
	auth := r.Header.Get("Authorization")
	required := a.Config.OAuthRequired || op == "getBalance" || str(object(b["authorization"]), "type") == "access_token_authorization"
	if auth == "" && !required {
		return nil
	}
	if len(auth) < 7 || !strings.EqualFold(auth[:7], "Bearer ") {
		return errBearer
	}
	token := auth[7:]
	var account, scopes string
	e := tx.QueryRow(ctx, "SELECT account_id,scope FROM tokens WHERE token_hash=$1 AND client_id=$2 AND kind='ACCESS' AND NOT consumed AND expires_at>now()", fingerprint(token), a.Config.ClientID).Scan(&account, &scopes)
	if e != nil {
		if notFound(e) {
			return errBearer
		}
		return e
	}
	if notFound(e) {
		return errBearer
	}
	if !containsScope(scopes, scope) {
		return errBearer
	}
	wanted := str(object(b["authorization"]), "account_id")
	if op == "getBalance" {
		wanted = str(b, "account_id")
	}
	resourceID := ""
	if op == "capture" || op == "cancel" {
		_, _, resourceID = route(strings.TrimPrefix(r.URL.EscapedPath(), a.Config.BasePath), r.Method)
	}
	if op == "initRefund" {
		resourceID = str(b, "original_transaction_id")
	}
	if resourceID != "" {
		resource, err := getTransaction(ctx, tx, a.Config.ClientID, "charge", resourceID)
		if err == nil {
			wanted = resource.Account
		} else if !notFound(err) {
			return err
		}
	}

	if wanted != "" && wanted != account {
		return errBearer
	}
	return nil
}
func containsScope(scopes, scope string) bool {
	for _, s := range strings.Fields(scopes) {
		if s == scope {
			return true
		}
	}
	return false
}
func secureEqual(x, y string) bool { return subtle.ConstantTimeCompare([]byte(x), []byte(y)) == 1 }
