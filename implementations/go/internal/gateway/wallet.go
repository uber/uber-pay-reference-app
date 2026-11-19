package gateway

import (
	"context"
	"fmt"
	"html"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

func urlQueryEscape(s string) string { return url.QueryEscape(s) }
func validScopes(s string) bool {
	if s == "" {
		return false
	}
	seen := map[string]bool{}
	for _, v := range strings.Fields(s) {
		if seen[v] {
			return false
		}
		seen[v] = true
		switch v {
		case "charge", "refund", "get_balance", "offline_access":
		default:
			return false
		}
	}
	return true
}
func (a *App) putToken(ctx context.Context, tx pgx.Tx, kind, account, scope, redirect, state string, ttl time.Duration) (string, error) {
	v := randomToken()
	_, e := tx.Exec(ctx, "INSERT INTO tokens(token_hash,kind,client_id,account_id,scope,redirect_uri,state,expires_at,family_id) VALUES($1,$2,$3,$4,$5,$6,$7,now()+($8*interval '1 second'),$9)", fingerprint(v), kind, a.Config.ClientID, account, scope, redirect, state, int64(ttl/time.Second), randomToken())
	return v, e
}
func (a *App) authorize(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	for _, v := range q {
		if len(v) != 1 {
			w.WriteHeader(400)
			return
		}
	}
	if q.Get("client_id") != a.Config.ClientID || q.Get("response_type") != "code" || !a.Config.Redirects[q.Get("redirect_uri")] || q.Get("state") == "" || len(q.Get("state")) > 2048 || !validScopes(q.Get("scope")) {
		w.WriteHeader(400)
		return
	}
	details, e := DecodeJSON([]byte(q.Get("authorization_details")))
	if e != nil || a.Contract.Validate(Object{"type": "array", "items": Object{"$ref": "#/components/schemas/AuthorizationDetail"}}, details) != nil {
		w.WriteHeader(400)
		return
	}
	arr := details.([]any)
	if len(arr) == 0 {
		w.WriteHeader(400)
		return
	}
	for _, x := range arr {
		b := object(x)
		if !knownFunding(str(b, "funding_method")) || (str(b, "account_id") != "" && str(b, "account_id") != "demo-account") {
			w.WriteHeader(400)
			return
		}
	}
	ctx := r.Context()
	tx, e := a.Store.Begin(ctx)
	if e != nil {
		w.WriteHeader(500)
		return
	}
	defer tx.Rollback(ctx)
	identity, err := a.Wallet.ConsentIdentity(ctx, arr)
	if err != nil {
		w.WriteHeader(400)
		return
	}
	token, e := a.putToken(ctx, tx, "CONSENT", identity.AccountID, q.Get("scope"), q.Get("redirect_uri"), q.Get("state"), 5*time.Minute)
	if e != nil || tx.Commit(ctx) != nil {
		w.WriteHeader(500)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; form-action 'self'; frame-ancestors 'none'")
	fmt.Fprintf(w, `<!doctype html><html><body><h1>Local fake wallet consent</h1><p>No real funds or identity provider. Account: demo-account.</p><form method="post" action="/demo/consent"><input type="hidden" name="consent_token" value="%s"><input type="hidden" name="csrf" value="%s"><p>Requested scopes: %s</p><button name="decision" value="approve">Approve</button><button name="decision" value="deny">Deny</button></form></body></html>`, token, fingerprint("csrf:"+token), html.EscapeString(q.Get("scope")))
}
func (a *App) consent(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		w.WriteHeader(405)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 16384)
	if r.ParseForm() != nil {
		w.WriteHeader(400)
		return
	}
	token := r.PostForm.Get("consent_token")
	if token == "" || !secureEqual(r.PostForm.Get("csrf"), fingerprint("csrf:"+token)) {
		w.WriteHeader(400)
		return
	}
	ctx := r.Context()
	tx, e := a.Store.Begin(ctx)
	if e != nil {
		w.WriteHeader(500)
		return
	}
	defer tx.Rollback(ctx)
	var account, scope, redirect, state string
	e = tx.QueryRow(ctx, "SELECT account_id,scope,redirect_uri,state FROM tokens WHERE token_hash=$1 AND client_id=$2 AND kind='CONSENT' AND NOT consumed AND expires_at>now() FOR UPDATE", fingerprint(token), a.Config.ClientID).Scan(&account, &scope, &redirect, &state)
	if e != nil || !a.Config.Redirects[redirect] {
		w.WriteHeader(400)
		return
	}
	decision := r.PostForm.Get("decision")
	if decision != "approve" && decision != "deny" {
		w.WriteHeader(400)
		return
	}
	_, e = tx.Exec(ctx, "UPDATE tokens SET consumed=true WHERE token_hash=$1", fingerprint(token))
	if e != nil {
		w.WriteHeader(500)
		return
	}
	u, e := url.Parse(redirect)
	if e != nil {
		w.WriteHeader(400)
		return
	}
	q := u.Query()
	q.Set("state", state)
	if decision == "deny" {
		q.Set("error", "access_denied")
	} else {
		code, err := a.putToken(ctx, tx, "CODE", account, scope, redirect, "", 10*time.Minute)
		if err != nil {
			w.WriteHeader(500)
			return
		}
		q.Set("code", code)
	}
	u.RawQuery = q.Encode()
	if tx.Commit(ctx) != nil {
		w.WriteHeader(500)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Location", u.String())
	w.WriteHeader(302)
}
func (a *App) token(w http.ResponseWriter, r *http.Request, b Object) {
	bad := func(code string) { a.write(w, Reply{400, encode(Object{"error": code})}) }
	if !secureEqual(str(b, "client_id"), a.Config.ClientID) || !secureEqual(str(b, "client_secret"), a.Config.ClientSecret) {
		bad("invalid_client")
		return
	}
	ctx := r.Context()
	tx, e := a.Store.Begin(ctx)
	if e != nil {
		bad("server_error")
		return
	}
	defer tx.Rollback(ctx)
	account, scope, redirect := "demo-account", "", str(b, "redirect_uri")
	grant := str(b, "grant_type")
	out := Object{"token_type": "bearer"}
	responseSchema := "RefreshTokenResponse"
	switch grant {
	case "authorization_code":
		code := str(b, "code")
		var savedRedirect string
		e = tx.QueryRow(ctx, "SELECT account_id,scope,redirect_uri FROM tokens WHERE token_hash=$1 AND kind='CODE' AND client_id=$2 AND NOT consumed AND expires_at>now() FOR UPDATE", fingerprint(code), a.Config.ClientID).Scan(&account, &scope, &savedRedirect)
		if e != nil || redirect != savedRedirect {
			bad("invalid_grant")
			return
		}
		_, e = tx.Exec(ctx, "UPDATE tokens SET consumed=true WHERE token_hash=$1", fingerprint(code))
	case "refresh_token":
		old := str(b, "refresh_token")
		e = tx.QueryRow(ctx, "SELECT account_id,scope FROM tokens WHERE token_hash=$1 AND kind='REFRESH' AND client_id=$2 AND NOT consumed AND expires_at>now() FOR UPDATE", fingerprint(old), a.Config.ClientID).Scan(&account, &scope)
		if e != nil {
			bad("invalid_grant")
			return
		}
		_, e = tx.Exec(ctx, "UPDATE tokens SET consumed=true WHERE token_hash=$1", fingerprint(old))
		if e != nil {
			bad("server_error")
			return
		}
		access, err := a.putToken(ctx, tx, "ACCESS", account, scope, "", "", 10*time.Minute)
		if err != nil {
			bad("server_error")
			return
		}
		out["access_token"] = access
		out["expires_in"] = "600"
		responseSchema = "AccessTokenResponse"
	case "urn:uberpay:params:oauth:grant-type:wallet-grant":
		identity, err := a.Wallet.ExchangeWalletReference(ctx, object(b["wallet_reference_data"]))
		if err != nil {
			bad("invalid_grant")
			return
		}
		account = identity.AccountID
		scope = str(b, "scope")
		if account != "demo-account" || !validScopes(scope) || !knownFunding(str(b, "funding_method")) {
			bad("invalid_grant")
			return
		}
		if v := str(b, "account_id"); v != "" && v != account {
			bad("invalid_grant")
			return
		}
		responseSchema = "WalletGrantRefreshTokenResponse"
	default:
		bad("unsupported_grant_type")
		return
	}
	if e != nil {
		bad("server_error")
		return
	}
	refresh, e := a.putToken(ctx, tx, "REFRESH", account, scope, "", "", 30*24*time.Hour)
	if e != nil {
		bad("server_error")
		return
	}
	out["refresh_token"] = refresh
	if grant != "refresh_token" {
		out["scope"] = scope
		out["authorization_details"] = []any{Object{"type": "authorized_account", "account_id": account, "fingerprint": "demo-fingerprint", "display_name": "Demo wallet"}}
	}
	if a.Contract.Validate(Object{"$ref": "#/components/schemas/" + responseSchema}, out) != nil {
		bad("server_error")
		return
	}
	if tx.Commit(ctx) != nil {
		bad("server_error")
		return
	}
	a.write(w, Reply{200, encode(out)})
}
func (a *App) action(w http.ResponseWriter, r *http.Request) {
	if r.Method == "GET" {
		id := r.URL.Query().Get("id")
		kind := r.URL.Query().Get("kind")
		ctx := r.Context()
		tx, e := a.Store.Begin(ctx)
		if e != nil {
			w.WriteHeader(500)
			return
		}
		defer tx.Rollback(ctx)
		t, e := getTransaction(ctx, tx, a.Config.ClientID, kind, id)
		if e != nil || t.State != "PENDING" {
			w.WriteHeader(404)
			return
		}
		token, e := a.putToken(ctx, tx, "ACTION", "demo-account", "", "", encode(Object{"id": id, "kind": kind}), 5*time.Minute)
		if e != nil || tx.Commit(ctx) != nil {
			w.WriteHeader(500)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		fmt.Fprintf(w, `<!doctype html><html><body><h1>Complete local fake payment</h1><form method="post" action="/demo/action"><input type="hidden" name="action_token" value="%s"><input type="hidden" name="csrf" value="%s"><button name="decision" value="approve">Confirm</button><button name="decision" value="deny">Reject</button></form></body></html>`, token, fingerprint("csrf:"+token))
		return
	}
	if r.Method != "POST" {
		w.WriteHeader(405)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 16384)
	if r.ParseForm() != nil {
		w.WriteHeader(400)
		return
	}
	token := r.PostForm.Get("action_token")
	if token == "" || !secureEqual(r.PostForm.Get("csrf"), fingerprint("csrf:"+token)) {
		w.WriteHeader(400)
		return
	}
	ctx := r.Context()
	tx, e := a.Store.Begin(ctx)
	if e != nil {
		w.WriteHeader(500)
		return
	}
	defer tx.Rollback(ctx)
	var data string
	e = tx.QueryRow(ctx, "SELECT state FROM tokens WHERE token_hash=$1 AND client_id=$2 AND kind='ACTION' AND NOT consumed AND expires_at>now() FOR UPDATE", fingerprint(token), a.Config.ClientID).Scan(&data)
	if e != nil {
		w.WriteHeader(400)
		return
	}
	v, e := DecodeJSON([]byte(data))
	if e != nil {
		w.WriteHeader(500)
		return
	}
	state := "CONFIRMED"
	if r.PostForm.Get("decision") == "deny" {
		state = "REJECTED"
	} else if r.PostForm.Get("decision") != "approve" {
		w.WriteHeader(400)
		return
	}
	_, e = tx.Exec(ctx, "UPDATE tokens SET consumed=true WHERE token_hash=$1", fingerprint(token))
	if e == nil {
		_, e = tx.Exec(ctx, "UPDATE jobs SET payload=$4,status='PENDING',next_attempt_at=now() WHERE client_id=$1 AND tx_kind=$2 AND tx_id=$3 AND job_type='COMPLETION' AND status='WAITING'", a.Config.ClientID, str(object(v), "kind"), str(object(v), "id"), encode(Object{"state": state}))
	}
	if e != nil || tx.Commit(ctx) != nil {
		w.WriteHeader(500)
		return
	}
	w.Header().Set("Content-Type", "text/plain")
	_, _ = w.Write([]byte("Synthetic completion accepted; no real funds moved."))
}
