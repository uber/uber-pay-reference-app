package gateway

import (
	"io"
	"net/http"
	"strings"
	"time"
)

func (a *App) testControl(w http.ResponseWriter, r *http.Request) {
	if !a.Config.TestControl || !secureEqual(r.Header.Get("X-Test-Control-Secret"), a.Config.TestSecret) {
		w.WriteHeader(404)
		return
	}
	if r.Method == "GET" && r.URL.Path == "/_test/state" {
		out := Object{}
		for _, table := range []string{"accounts", "transactions", "jobs"} {
			rows, e := a.Store.Pool.Query(r.Context(), "SELECT row_to_json(t)::text FROM "+table+" t WHERE client_id=$1", a.Config.ClientID)
			if e != nil {
				w.WriteHeader(500)
				return
			}
			data := []any{}
			for rows.Next() {
				var s string
				if e = rows.Scan(&s); e != nil {
					rows.Close()
					w.WriteHeader(500)
					return
				}
				v, e := DecodeJSON([]byte(s))
				if e != nil {
					rows.Close()
					w.WriteHeader(500)
					return
				}
				data = append(data, v)
			}
			rows.Close()
			out[table] = data
		}
		a.write(w, Reply{200, encode(out), nil})
		return
	}
	if r.Method != "POST" || !strings.HasPrefix(r.Header.Get("Content-Type"), "application/json") {
		w.WriteHeader(400)
		return
	}
	raw, e := io.ReadAll(http.MaxBytesReader(w, r.Body, 1048576))
	if e != nil {
		w.WriteHeader(400)
		return
	}
	v, e := DecodeJSON(raw)
	if e != nil {
		w.WriteHeader(400)
		return
	}
	b := object(v)
	ctx := r.Context()
	tx, e := a.Store.Begin(ctx)
	if e != nil {
		w.WriteHeader(500)
		return
	}
	defer tx.Rollback(ctx)
	switch r.URL.Path {
	case "/_test/complete":
		state := str(b, "state")
		kind := str(b, "kind")
		if state != "CONFIRMED" && state != "REJECTED" || kind != "charge" && kind != "refund" && kind != "payout" {
			w.WriteHeader(400)
			return
		}
		t, err := getTransaction(ctx, tx, a.Config.ClientID, kind, str(b, "id"))
		if err != nil || t.State != "PENDING" {
			w.WriteHeader(404)
			return
		}
		_, e = tx.Exec(ctx, "UPDATE jobs SET payload=$4,status='PENDING',next_attempt_at=now() WHERE client_id=$1 AND tx_kind=$2 AND tx_id=$3 AND job_type='COMPLETION' AND status IN ('PENDING','WAITING')", a.Config.ClientID, kind, t.ID, encode(Object{"state": state}))
	case "/_test/reverse-refund":
		e = a.reverseRefund(ctx, tx, str(b, "id"))
	case "/_test/expire":
		t, err := getTransaction(ctx, tx, a.Config.ClientID, "charge", str(b, "id"))
		if err != nil || t.State != "AUTHORIZED" {
			w.WriteHeader(404)
			return
		}
		e = a.cancelAuthorization(ctx, tx, t)
	case "/_test/callback":
		name, id, payload := str(b, "name"), str(b, "id"), b["payload"]
		if _, _, _, err := BuildCallback(name, id, payload); err != nil || a.Contract.Callback(name, payload) != nil {
			w.WriteHeader(400)
			return
		}
		e = queue(ctx, tx, a.Config.ClientID, "test", id, "CALLBACK", name, payload, "PENDING", time.Duration(0))
	default:
		w.WriteHeader(404)
		return
	}
	if e != nil || tx.Commit(ctx) != nil {
		w.WriteHeader(500)
		return
	}
	a.write(w, Reply{200, `{"status":"accepted"}`, nil})
}
