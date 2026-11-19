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
	Status int
	Body   string
}
type App struct {
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
	store, e := OpenStore(ctx, c)
	if e != nil {
		return nil, e
	}
	return &App{Config: c, Store: store, Contract: Contract{Spec: object(v)}, Tokens: &UberTokenProvider{entries: map[string]cachedToken{}}, Client: httpClient(), Adapter: adapter, Wallet: wallet}, nil
}
func (a *App) Close() { a.Store.Pool.Close() }
func (a *App) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	defer func() {
		if recover() != nil {
			log.Print("request failure")
			http.Error(w, "", 500)
		}
	}()
	if r.URL.Path == "/health" {
		if a.Store.Pool.Ping(r.Context()) != nil {
			w.WriteHeader(503)
			return
		}
		a.write(w, Reply{200, `{"status":"ok","language":"go"}`})
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
		status := 400
		if pattern == "/charge" || pattern == "/balance" {
			status = 401
		}
		a.write(w, a.failure(pattern, status, "VALIDATION_ERROR"))
		return
	}
	mutating := op == "charge" || op == "capture" || op == "cancel" || op == "initRefund" || op == "payout"
	key := r.Header.Get("X-Idempotency-Key")
	hash := fingerprint(path + ":" + encode(body))
	if mutating {
		if key == "" || len(key) > 256 {
			a.write(w, a.failure(pattern, 400, "VALIDATION_ERROR"))
			return
		}
		var oldhash, oldbody string
		var status int
		e = tx.QueryRow(ctx, "SELECT fingerprint,status,response_body FROM operations WHERE client_id=$1 AND operation_name=$2 AND idem_key=$3", a.Config.ClientID, op, key).Scan(&oldhash, &status, &oldbody)
		if e == nil {
			if oldhash != hash {
				a.write(w, a.failure(pattern, 400, "VALIDATION_ERROR"))
			} else {
				a.write(w, Reply{status, oldbody})
			}
			return
		}
		if !notFound(e) {
			a.write(w, a.failure(pattern, 500, "PAYMENT_PSP_ERROR"))
			return
		}
	}
	reply, e := a.execute(ctx, tx, op, id, body, r, hash)
	if e != nil {
		log.Printf("database operation failed (%s)", op)
		a.write(w, a.failure(pattern, 500, "PAYMENT_PSP_ERROR"))
		return
	}
	if reply.Body != "" {
		value, e := DecodeJSON([]byte(reply.Body))
		if e != nil || a.Contract.Response(pattern, strings.ToLower(r.Method), reply.Status, value) != nil {
			log.Printf("response contract failure (%s)", op)
			a.write(w, a.failure(pattern, 500, "PAYMENT_PSP_ERROR"))
			return
		}
	}
	if mutating && reply.Status < 500 {
		_, e = tx.Exec(ctx, "INSERT INTO operations(client_id,operation_name,idem_key,fingerprint,status,response_body,tx_id) VALUES($1,$2,$3,$4,$5,$6,$7)", a.Config.ClientID, op, key, hash, reply.Status, reply.Body, str(body, "id"))
		if e != nil {
			a.write(w, a.failure(pattern, 500, "PAYMENT_PSP_ERROR"))
			return
		}
	}
	if e = tx.Commit(ctx); e != nil {
		a.write(w, a.failure(pattern, 500, "PAYMENT_PSP_ERROR"))
		return
	}
	a.write(w, reply)
}
func (a *App) write(w http.ResponseWriter, r Reply) {
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
	if status == 500 { // Unsupported operational error statuses are not invented on these routes.
		if path == "/refund" || path == "/find-transaction" || strings.HasPrefix(path, "/charge/{id}") {
			status = 400
			detail = "VALIDATION_ERROR"
		}
	}
	schema := object(object(object(object(a.Contract.operation(path, map[bool]string{true: "get", false: "post"}[path == "/find-transaction"])["responses"])[fmt.Sprint(status)])["content"])["application/json"])
	if len(schema) == 0 {
		return Reply{Status: status}
	}
	return Reply{status, encode(Object{"detail": detail})}
}
func amount(o Object) (int64, string, error) {
	v, e := number(o["value"])
	cur := str(o, "currency")
	if e != nil || v <= 0 || len(cur) != 3 {
		return 0, "", errors.New("invalid amount")
	}
	return v, cur, nil
}
func knownFunding(s string) bool {
	switch s {
	case "DEMO_SYNC", "DEMO_REJECT", "DEMO_PENDING", "DEMO_PENDING_REJECT", "DEMO_WEBVIEW", "DEMO_QR":
		return true
	}
	return false
}
func (a *App) execute(ctx context.Context, tx pgx.Tx, op, id string, b Object, r *http.Request, hash string) (Reply, error) {
	switch op {
	case "charge", "payout":
		return a.create(ctx, tx, op, b, hash)
	case "capture", "cancel":
		return a.finishManual(ctx, tx, op, id, b)
	case "initRefund":
		return a.refund(ctx, tx, b, hash)
	case "getBalance":
		av, bal, e := accountBalance(ctx, tx, a.Config.ClientID, str(b, "account_id"), "BRL")
		if notFound(e) {
			return a.failure("/balance", 404, ""), nil
		}
		if e != nil {
			return Reply{}, e
		}
		if !knownFunding(str(b, "funding_method")) {
			return a.failure("/balance", 400, ""), nil
		}
		result, err := a.Adapter.GetBalance(ctx, BalanceQuery{Account: str(b, "account_id"), Currency: "BRL", AvailableE5: av, BalanceE5: bal})
		if err != nil {
			return Reply{}, err
		}
		if result.AvailableE5 < 0 || result.BalanceE5 < result.AvailableE5 {
			return Reply{}, errors.New("invalid adapter balance")
		}
		return Reply{200, encode(Object{"available_balance": Object{"value": result.AvailableE5, "currency": result.Currency}, "balance": Object{"value": result.BalanceE5, "currency": result.Currency}})}, nil
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
		if t.State == "CANCELLED" {
			return Reply{Status: 400}, nil
		}
		out := Object{"id": t.ID, "merchant_reference": t.Ref, "status": t.State, "amount": Object{"value": t.Amount, "currency": t.Currency}}
		if t.State == "CONFIRMED" {
			out["confirmed_at"] = t.Updated.UTC().Format(time.RFC3339Nano)
		}
		return Reply{200, encode(out)}, nil
	}
	return Reply{Status: 404}, nil
}
func (a *App) create(ctx context.Context, tx pgx.Tx, kind string, b Object, hash string) (Reply, error) {
	path := "/" + kind
	id := str(b, "id")
	if id == "" || len(id) > 512 {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	old, e := getTransaction(ctx, tx, a.Config.ClientID, kind, id)
	if e == nil {
		if old.Hash == hash {
			return Reply{old.Status, old.Response}, nil
		}
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	if !notFound(e) {
		return Reply{}, e
	}
	n, currency, e := amount(object(b["amount"]))
	funding := str(b, "funding_method")
	capture := str(b, "capture_method")
	if capture == "" {
		capture = "AUTOMATIC"
	}
	if e != nil || !knownFunding(funding) || capture == "MANUAL" && strings.HasPrefix(funding, "DEMO_PENDING") || capture == "MANUAL" && (funding == "DEMO_WEBVIEW" || funding == "DEMO_QR") {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	account := "demo-account"
	if kind == "charge" && str(object(b["authorization"]), "account_id") != "" {
		account = str(object(b["authorization"]), "account_id")
	}
	if kind == "payout" && str(object(object(b["payee"])["account"]), "account_id") == "insufficient-account" {
		account = "insufficient-account"
	}
	available, _, e := accountBalance(ctx, tx, a.Config.ClientID, account, currency)
	if notFound(e) {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	if e != nil {
		return Reply{}, e
	}
	t := Transaction{Kind: kind, ID: id, Ref: "psp-" + fingerprint(kind + ":" + id)[:24], Account: account, Amount: n, Currency: currency, Capture: capture, Funding: funding, Hash: hash, Status: 200, State: "CONFIRMED"}
	command := PaymentCommand{ID: id, Account: account, Currency: currency, FundingMethod: funding, CaptureMethod: capture, ProviderKey: fingerprint(a.Config.ClientID + ":" + kind + ":" + id), AmountE5: n, AvailableE5: available}
	var result ProviderResult
	if kind == "charge" {
		result, e = a.Adapter.Charge(ctx, command)
	} else {
		result, e = a.Adapter.Payout(ctx, command)
	}
	if e != nil {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	t.State, t.Detail = result.State, result.Detail
	if kind == "payout" && t.State == "REJECTED" {
		return a.failure(path, 400, t.Detail), nil
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
	if t.State == "PENDING" && funding == "DEMO_WEBVIEW" {
		out["actions"] = []any{Object{"type": "web_view_user_input_action", "url": strings.TrimSuffix(a.Config.PublicBaseURL, a.Config.BasePath) + "/demo/action?id=" + urlQueryEscape(id) + "&kind=" + kind}}
	}
	if t.State == "PENDING" && funding == "DEMO_QR" {
		out["actions"] = []any{Object{"type": "qr_code_input_action", "code": "DEMO:" + id, "expiry_time_millis": time.Now().Add(10 * time.Minute).UnixMilli()}}
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
	if t.State == "PENDING" {
		status := "PENDING"
		if funding == "DEMO_WEBVIEW" || funding == "DEMO_QR" {
			status = "WAITING"
		}
		state := "CONFIRMED"
		if funding == "DEMO_PENDING_REJECT" {
			state = "REJECTED"
		}
		e = queue(ctx, tx, a.Config.ClientID, kind, id, "COMPLETION", "", Object{"state": state}, status, a.Config.AsyncDelay)
	}
	return Reply{200, t.Response}, e
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
	if expired && t.State == "AUTHORIZED" {
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
			return a.failure(path, 400, "VALIDATION_ERROR"), nil
		}
		return Reply{Status: 200}, a.cancelAuthorization(ctx, tx, t)
	}
	n, c, e := amount(object(b["amount_to_capture"]))
	if e != nil || c != t.Currency {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	if n > t.Amount {
		return a.failure(path, 400, "EXCEEDS_CAPTURE_LIMIT"), nil
	}
	if err := a.Adapter.Capture(ctx, CaptureCommand{ID: id, ProviderKey: fingerprint(a.Config.ClientID + ":capture:" + id), Currency: c, AuthorizedCurrency: t.Currency, AmountE5: n, AuthorizedE5: t.Amount}); err != nil {
		return a.failure(path, 400, "VALIDATION_ERROR"), nil
	}
	if e = adjust(ctx, tx, a.Config.ClientID, t.Account, t.Currency, 0, -n); e != nil {
		return Reply{}, e
	}
	_, e = tx.Exec(ctx, "UPDATE transactions SET state='CONFIRMED',updated_at=now() WHERE client_id=$1 AND kind='charge' AND id=$2", a.Config.ClientID, id)
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
			return Reply{old.Status, old.Response}, nil
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
	t := Transaction{Kind: "refund", ID: id, Ref: "psp-" + fingerprint("refund:" + id)[:24], Original: original.ID, Account: original.Account, Amount: n, Currency: c, Capture: "AUTOMATIC", Funding: original.Funding, State: "CONFIRMED", Hash: hash, Status: 201}
	result, err := a.Adapter.Refund(ctx, PaymentCommand{ID: id, Account: original.Account, Currency: c, FundingMethod: original.Funding, ProviderKey: fingerprint(a.Config.ClientID + ":refund:" + id), AmountE5: n})
	if err != nil {
		return Reply{Status: 400}, nil
	}
	t.State = result.State
	t.Response = encode(Object{"status": t.State, "merchant_reference": t.Ref})
	if e = insertTransaction(ctx, tx, a.Config.ClientID, t); e != nil {
		return Reply{}, e
	}
	if t.State == "CONFIRMED" {
		e = adjust(ctx, tx, a.Config.ClientID, t.Account, c, n, n)
	} else {
		e = queue(ctx, tx, a.Config.ClientID, "refund", id, "COMPLETION", "", Object{"state": "CONFIRMED"}, "PENDING", a.Config.AsyncDelay)
	}
	return Reply{201, t.Response}, e
}
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
	if !strings.HasPrefix(auth, "Bearer ") {
		return errors.New("missing bearer")
	}
	token := strings.TrimPrefix(auth, "Bearer ")
	var account, scopes string
	e := tx.QueryRow(ctx, "SELECT account_id,scope FROM tokens WHERE token_hash=$1 AND client_id=$2 AND kind='ACCESS' AND NOT consumed AND expires_at>now()", fingerprint(token), a.Config.ClientID).Scan(&account, &scopes)
	if e != nil {
		return e
	}
	if !containsScope(scopes, scope) {
		return errors.New("wrong scope")
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
		return errors.New("account binding mismatch")
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
