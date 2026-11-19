package gateway

import (
	"context"
	"fmt"
	"html"
	"net/http"
	"net/url"
	"strconv"
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
	if q.Get("client_id") != a.Config.ClientID || !a.Config.Redirects[q.Get("redirect_uri")] {
		w.WriteHeader(400)
		return
	}
	bad := func(code string) {
		u, _ := url.Parse(q.Get("redirect_uri"))
		p := u.Query()
		p.Set("error", code)
		p.Set("state", q.Get("state"))
		u.RawQuery = p.Encode()
		w.Header().Set("Location", u.String())
		w.WriteHeader(302)
	}
	if !validScopes(q.Get("scope")) {
		bad("invalid_scope")
		return
	}
	if q.Get("response_type") != "code" || q.Get("state") == "" || len(q.Get("state")) > 2048 {
		bad("invalid_request")
		return
	}
	details, e := DecodeJSON([]byte(q.Get("authorization_details")))
	if e != nil || a.Contract.Validate(Object{"type": "array", "items": Object{"$ref": "#/components/schemas/AuthorizationDetail"}}, details) != nil {
		bad("invalid_request")
		return
	}
	arr := details.([]any)
	if len(arr) == 0 {
		bad("invalid_request")
		return
	}
	for _, x := range arr {
		b := object(x)
		if !knownFunding(str(b, "funding_method")) || (str(b, "account_id") != "" && str(b, "account_id") != "demo-account") {
			bad("invalid_request")
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
	language := locale(str(object(arr[0]), "locale"))
	for _, entry := range arr {
		if _, err := a.Features.Method(str(object(entry), "funding_method"), str(object(entry), "country_iso2"), ""); err != nil {
			bad("invalid_request")
			return
		}
	}
	token, e := a.putToken(ctx, tx, "CONSENT", identity.AccountID, q.Get("scope"), q.Get("redirect_uri"), encode(Object{"state": q.Get("state"), "locale": language, "details": arr}), 5*time.Minute)
	if e != nil || tx.Commit(ctx) != nil {
		w.WriteHeader(500)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; form-action 'self'; frame-ancestors 'none'")
	w.Header().Set("Content-Language", language)
	methods := ""
	for _, entry := range arr {
		d := object(entry)
		methods += "<li>" + html.EscapeString(a.Features.Text(language, "method")) + ": " + html.EscapeString(str(d, "funding_method")) + " — " + html.EscapeString(a.Features.Text(language, "country")) + ": " + html.EscapeString(str(d, "country_iso2")) + "</li>"
	}
	fmt.Fprintf(w, `<!doctype html><html lang="%s"><body><h1>%s</h1><p>%s</p><ul>%s</ul><p>%s: %s</p><form method="post" action="/demo/consent"><input type="hidden" name="consent_token" value="%s"><input type="hidden" name="csrf" value="%s"><button name="decision" value="approve">%s</button><button name="decision" value="deny">%s</button></form></body></html>`, language, a.Features.Text(language, "consent_title"), a.Features.Text(language, "synthetic"), methods, a.Features.Text(language, "scopes"), html.EscapeString(q.Get("scope")), token, fingerprint("csrf:"+token), a.Features.Text(language, "approve"), a.Features.Text(language, "deny"))

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
	saved, _ := DecodeJSON([]byte(state))
	q.Set("state", str(object(saved), "state"))
	if decision == "deny" {
		q.Set("error", "access_denied")
	} else {
		code, err := a.putToken(ctx, tx, "CODE", account, scope, redirect, encode(Object{"details": object(saved)["details"], "locale": object(saved)["locale"]}), 10*time.Minute)
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
	bad := func(code string) { a.write(w, Reply{400, encode(Object{"error": code}), nil}) }
	if !secureEqual(str(b, "client_id"), a.Config.ClientID) || !secureEqual(str(b, "client_secret"), a.Config.ClientSecret) {
		bad("invalid_client")
		return
	}
	ctx := r.Context()
	tx, e := a.Store.Begin(ctx)
	if e != nil {
		a.write(w, Reply{Status: 500})
		return
	}
	defer tx.Rollback(ctx)
	account, scope, redirect := "demo-account", "", str(b, "redirect_uri")
	family := ""
	consentContext := ""
	grant := str(b, "grant_type")
	out := Object{"token_type": "bearer"}
	responseSchema := "RefreshTokenResponse"
	switch grant {
	case "authorization_code":
		code := str(b, "code")
		var savedRedirect string
		var consumed bool
		e = tx.QueryRow(ctx, "SELECT account_id,scope,redirect_uri,family_id,consumed,COALESCE(state,'') FROM tokens WHERE token_hash=$1 AND kind='CODE' AND client_id=$2 AND expires_at>now() FOR UPDATE", fingerprint(code), a.Config.ClientID).Scan(&account, &scope, &savedRedirect, &family, &consumed, &consentContext)
		if e == nil && consumed {
			_, e = tx.Exec(ctx, "UPDATE tokens SET consumed=true WHERE client_id=$1 AND family_id=$2", a.Config.ClientID, family)
			if e != nil || tx.Commit(ctx) != nil {
				a.write(w, Reply{Status: 500})
				return
			}
			bad("invalid_grant")
			return
		}
		if e != nil && !notFound(e) {
			a.write(w, Reply{Status: 500})
			return
		}
		if e != nil || redirect != savedRedirect {
			bad("invalid_grant")
			return
		}
		_, e = tx.Exec(ctx, "UPDATE tokens SET consumed=true WHERE token_hash=$1", fingerprint(code))
	case "refresh_token":
		old := str(b, "refresh_token")
		e = tx.QueryRow(ctx, "SELECT account_id,scope,COALESCE(state,'') FROM tokens WHERE token_hash=$1 AND kind='REFRESH' AND client_id=$2 AND NOT consumed AND expires_at>now() FOR UPDATE", fingerprint(old), a.Config.ClientID).Scan(&account, &scope, &consentContext)
		if e != nil {
			if !notFound(e) {
				a.write(w, Reply{Status: 500})
				return
			}
			bad("invalid_grant")
			return
		}
		access, err := a.putToken(ctx, tx, "ACCESS", account, scope, "", consentContext, 10*time.Minute)
		if err != nil {
			a.write(w, Reply{Status: 500})
			return
		}
		if _, e = tx.Exec(ctx, "UPDATE tokens SET family_id=(SELECT family_id FROM tokens WHERE token_hash=$1) WHERE token_hash=$2", fingerprint(old), fingerprint(access)); e != nil {
			a.write(w, Reply{Status: 500})
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
		if !validScopes(scope) {
			bad("invalid_scope")
			return
		}
		_, methodError := a.Features.Method(str(b, "funding_method"), str(b, "country_iso2"), "")
		if account != "demo-account" || methodError != nil {
			bad("invalid_grant")
			return
		}
		if v := str(b, "account_id"); v != "" && v != account {
			bad("invalid_grant")
			return
		}
		consentContext = encode(Object{"details": []any{Object{"funding_method": b["funding_method"], "country_iso2": b["country_iso2"]}}, "locale": "en-US"})
		responseSchema = "WalletGrantRefreshTokenResponse"
	default:
		bad("unsupported_grant_type")
		return
	}
	if e != nil {
		a.write(w, Reply{Status: 500})
		return
	}
	refresh := ""
	if grant != "refresh_token" {
		refresh, e = a.putToken(ctx, tx, "REFRESH", account, scope, "", consentContext, a.Config.RefreshTTL)
	}
	if e != nil {
		a.write(w, Reply{Status: 500})
		return
	}
	if grant != "refresh_token" {
		out["refresh_token"] = refresh
		out["scope"] = scope
		out["authorization_details"] = []any{Object{"type": "authorized_account", "account_id": account, "fingerprint": "demo-fingerprint", "display_name": "Demo wallet"}}
	}
	if family != "" && refresh != "" {
		if _, e = tx.Exec(ctx, "UPDATE tokens SET family_id=$1 WHERE token_hash=$2", family, fingerprint(refresh)); e != nil {
			a.write(w, Reply{Status: 500})
			return
		}
	}
	if a.Contract.Validate(Object{"$ref": "#/components/schemas/" + responseSchema}, out) != nil {
		a.write(w, Reply{Status: 500})
		return
	}
	if tx.Commit(ctx) != nil {
		a.write(w, Reply{Status: 500})
		return
	}
	a.write(w, Reply{200, encode(out), nil})
}
func (a *App) action(w http.ResponseWriter, r *http.Request) {
	token := r.URL.Query().Get("token")
	if r.Method == "POST" {
		r.Body = http.MaxBytesReader(w, r.Body, 16384)
		if r.ParseForm() != nil {
			w.WriteHeader(400)
			return
		}
		token = r.PostForm.Get("action_token")
		if !secureEqual(r.PostForm.Get("csrf"), fingerprint("csrf:"+token)) {
			w.WriteHeader(400)
			return
		}
	} else if r.Method != "GET" {
		w.WriteHeader(405)
		return
	}
	if token == "" {
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
	if r.Method == "GET" {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Referrer-Policy", "no-referrer")
		language := locale(str(object(v), "locale"))
		w.Header().Set("Content-Language", language)
		amount := ""
		if object(object(v)["profile"])["allow_amount_update"] == true {
			amount = `<label>` + a.Features.Text(language, "amount") + `<input name="amount_e5" inputmode="numeric"></label>`
		}
		fmt.Fprintf(w, `<!doctype html><html lang="%s"><body><h1>%s</h1><p>%s</p><form method="post" action="/demo/action"><input type="hidden" name="action_token" value="%s"><input type="hidden" name="csrf" value="%s">%s<button name="decision" value="approve">%s</button><button name="decision" value="deny">%s</button></form></body></html>`, language, a.Features.Text(language, "payment_title"), a.Features.Text(language, "synthetic"), html.EscapeString(token), fingerprint("csrf:"+token), amount, a.Features.Text(language, "confirm"), a.Features.Text(language, "reject"))

		return
	}
	var completion string
	if e = tx.QueryRow(ctx, "SELECT payload FROM jobs WHERE client_id=$1 AND tx_kind=$2 AND tx_id=$3 AND status='WAITING' AND job_type='COMPLETION'", a.Config.ClientID, str(object(v), "kind"), str(object(v), "id")).Scan(&completion); e != nil {
		w.WriteHeader(400)
		return
	}
	content, e := DecodeJSON([]byte(completion))
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
	packet := object(content)
	packet["state"] = state
	packet["detail"] = "USER_CANCELLED"
	if text := r.PostForm.Get("amount_e5"); text != "" {
		if object(packet["profile"])["allow_amount_update"] != true {
			w.WriteHeader(400)
			return
		}
		n, err := strconv.ParseInt(text, 10, 64)
		if err != nil || n <= 0 {
			w.WriteHeader(400)
			return
		}
		t, err := getTransaction(ctx, tx, a.Config.ClientID, "charge", str(object(v), "id"))
		if err != nil || n > t.Amount {
			w.WriteHeader(400)
			return
		}
		packet["amount_e5"] = n
	}
	result, e := tx.Exec(ctx, `UPDATE jobs SET payload=$4,status='PENDING',next_attempt_at=now() WHERE client_id=$1 AND tx_kind=$2 AND tx_id=$3 AND job_type='COMPLETION' AND status='WAITING'`, a.Config.ClientID, str(object(v), "kind"), str(object(v), "id"), encode(packet))
	if e != nil {
		w.WriteHeader(500)
		return
	}
	if result.RowsAffected() != 1 {
		w.WriteHeader(400)
		return
	}
	_, e = tx.Exec(ctx, "UPDATE tokens SET consumed=true WHERE token_hash=$1", fingerprint(token))
	if e != nil || tx.Commit(ctx) != nil {
		w.WriteHeader(500)
		return
	}
	if target := str(object(content), "return_url"); a.Config.Redirects[target] {
		w.Header().Set("Location", target)
		w.WriteHeader(302)
		return
	}
	w.WriteHeader(200)
	_, _ = w.Write([]byte("Local payment completion scheduled"))
}
