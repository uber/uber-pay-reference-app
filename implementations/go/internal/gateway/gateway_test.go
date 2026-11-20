package gateway

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func testContract(t *testing.T) Contract {
	t.Helper()
	b, e := os.ReadFile("../../../../contracts/runtime.json")
	if e != nil {
		t.Fatal(e)
	}
	v, e := DecodeJSON(b)
	if e != nil {
		t.Fatal(e)
	}
	return Contract{Spec: object(v)}
}
func TestDecodeJSONExactAndBounded(t *testing.T) {
	for _, tc := range []struct {
		name, raw string
		valid     bool
	}{{"int64 precision", `{"value":9007199254740993}`, true}, {"duplicate keys", `{"a":1,"a":2}`, false}, {"trailing", `{} {}`, false}, {"truncated", `{"a":`, false}, {"scalar", `null`, true}, {"nested limit", strings.Repeat("[", 100) + "0" + strings.Repeat("]", 100), false}} {
		t.Run(tc.name, func(t *testing.T) {
			v, e := DecodeJSON([]byte(tc.raw))
			if (e == nil) != tc.valid {
				t.Fatalf("valid=%v err=%v", tc.valid, e)
			}
			if tc.name == "int64 precision" && encode(v) != tc.raw {
				t.Fatalf("precision changed: %s", encode(v))
			}
		})
	}
}
func TestContractCompositionAndBounds(t *testing.T) {
	c := testContract(t)
	for _, tc := range []struct {
		name, ref, raw string
		valid          bool
	}{{"exact E5", "CurrencyAmount", `{"value":9007199254740993,"currency":"BRL"}`, true}, {"overflow", "CurrencyAmount", `{"value":9223372036854775808,"currency":"BRL"}`, false}, {"fractional", "CurrencyAmount", `{"value":1.5,"currency":"BRL"}`, false}, {"missing amount", "CurrencyAmount", `{"currency":"BRL"}`, false}, {"short currency", "CurrencyAmount", `{"value":1,"currency":"US"}`, false}, {"valid authorization", "Authorization", `{"type":"access_token_authorization","account_id":"demo-account"}`, true}, {"unknown type", "Authorization", `{"type":"unknown"}`, false}, {"callback", "PayoutCallbackState", `{"value":"REJECTED"}`, true}} {
		t.Run(tc.name, func(t *testing.T) {
			v, e := DecodeJSON([]byte(tc.raw))
			if e != nil {
				t.Fatal(e)
			}
			e = c.Validate(Object{"$ref": "#/components/schemas/" + tc.ref}, v)
			if (e == nil) != tc.valid {
				t.Fatalf("valid=%v err=%v", tc.valid, e)
			}
		})
	}
}
func signedRequest(t *testing.T, key *rsa.PrivateKey, raw string, now time.Time) *http.Request {
	t.Helper()
	r := httptest.NewRequest("POST", "http://localhost:8080/uberpay/v1/charge?x=%2F&y=1", strings.NewReader(raw))
	r.RequestURI = "/uberpay/v1/charge?x=%2F&y=1"
	r.Header.Set("Date", now.UTC().Format(http.TimeFormat))
	d := sha256.Sum256([]byte(raw))
	r.Header.Set("Digest", "SHA-256="+base64.StdEncoding.EncodeToString(d[:]))
	payload := fmt.Sprintf("(request-target): post %s\nhost: %s\ndate: %s\ndigest: %s", r.RequestURI, r.Host, r.Header.Get("Date"), r.Header.Get("Digest"))
	h := sha256.Sum256([]byte(payload))
	sig, e := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, h[:])
	if e != nil {
		t.Fatal(e)
	}
	r.Header.Set("Signature", `keyId="test-key",algorithm="rsa-sha256",headers="(request-target) host date digest",signature="`+base64.StdEncoding.EncodeToString(sig)+`"`)
	return r
}
func TestSignatureCanonicalizationAndFailures(t *testing.T) {
	key, e := rsa.GenerateKey(rand.Reader, 2048)
	if e != nil {
		t.Fatal(e)
	}
	now := time.Now().Truncate(time.Second)
	raw := `{"value":9007199254740993}`
	for _, tc := range []struct {
		name   string
		change func(*http.Request)
		valid  bool
	}{{"valid", func(*http.Request) {}, true}, {"query encoding", func(r *http.Request) { r.RequestURI = "/uberpay/v1/charge?x=/&y=1" }, false}, {"host", func(r *http.Request) { r.Host = "evil.example" }, false}, {"digest", func(r *http.Request) { r.Header.Set("Digest", "SHA-256=invalid") }, false}, {"missing", func(r *http.Request) { r.Header.Del("Signature") }, false}, {"unterminated", func(r *http.Request) { r.Header.Set("Signature", `keyId="unterminated`) }, false}, {"wrong algorithm", func(r *http.Request) {
		r.Header.Set("Signature", strings.Replace(r.Header.Get("Signature"), "rsa-sha256", "hmac-sha256", 1))
	}, false}, {"unknown key", func(r *http.Request) {
		r.Header.Set("Signature", strings.Replace(r.Header.Get("Signature"), "test-key", "unknown", 1))
	}, false}, {"repeated header", func(r *http.Request) { r.Header.Add("Digest", r.Header.Get("Digest")) }, false}, {"stale", func(r *http.Request) { r.Header.Set("Date", now.Add(-time.Hour).Format(http.TimeFormat)) }, false}, {"future", func(r *http.Request) { r.Header.Set("Date", now.Add(time.Hour).Format(http.TimeFormat)) }, false}} {
		t.Run(tc.name, func(t *testing.T) {
			r := signedRequest(t, key, raw, now)
			tc.change(r)
			err := VerifySignature(r, []byte(raw), map[string]*rsa.PublicKey{"test-key": &key.PublicKey}, now, 5*time.Minute)
			if (err == nil) != tc.valid {
				t.Fatalf("valid=%v err=%v", tc.valid, err)
			}
		})
	}
	public := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: mustPKIX(t, &key.PublicKey)})
	if _, e = ParsePublicKey(public); e != nil {
		t.Fatal(e)
	}
	if _, e = ParsePublicKey([]byte("not a key")); e == nil {
		t.Fatal("invalid key accepted")
	}
}
func mustPKIX(t *testing.T, k *rsa.PublicKey) []byte {
	t.Helper()
	b, e := x509.MarshalPKIXPublicKey(k)
	if e != nil {
		t.Fatal(e)
	}
	return b
}
func TestCallbackBuildersAndContracts(t *testing.T) {
	c := testContract(t)
	for name, payload := range map[string]Object{"chargeCallback": {"state": Object{"value": "CONFIRMED"}, "psp_reference": "ref"}, "payoutCallback": {"state": Object{"value": "REJECTED", "detail": "MERCHANT_REJECTED"}, "psp_reference": "ref"}, "updateRefund": {"status": "CONFIRMED"}, "confirmDeposit": {"merchant_reference": "ref"}, "cancelDeposit": {"cancel_reason": "USER_CANCELLED"}, "updateDeposit": {"merchant_reference": "ref"}} {
		t.Run(name, func(t *testing.T) {
			path, scope, idem, e := BuildCallback(name, "id/encoded", payload)
			if e != nil || !strings.Contains(path, "id%2Fencoded") {
				t.Fatalf("bad path %s %v", path, e)
			}
			if scope == "" || idem == (name == "updateRefund") {
				t.Fatal("invalid callback metadata")
			}
			if e = c.Callback(name, payload); e != nil {
				t.Fatal(e)
			}
		})
	}
	if _, _, _, e := BuildCallback("unknown", "id", Object{}); e == nil {
		t.Fatal("unknown callback accepted")
	}
	if c.Callback("chargeCallback", Object{"state": Object{"value": "CONFIRMED"}}) == nil {
		t.Fatal("missing PSP reference accepted")
	}
}
func TestDemoAdapterStates(t *testing.T) {
	adapter := DemoPspAdapter{}
	for _, tc := range []struct {
		funding, capture, state string
		bad                     bool
	}{{"DEMO_SYNC", "AUTOMATIC", "CONFIRMED", false}, {"DEMO_SYNC", "MANUAL", "AUTHORIZED", false}, {"DEMO_REJECT", "AUTOMATIC", "REJECTED", false}, {"DEMO_PENDING", "AUTOMATIC", "PENDING", false}, {"DEMO_WEBVIEW", "MANUAL", "", true}, {"UNKNOWN", "AUTOMATIC", "", true}} {
		t.Run(tc.funding+tc.capture, func(t *testing.T) {
			r, e := adapter.Charge(context.Background(), PaymentCommand{FundingMethod: tc.funding, CaptureMethod: tc.capture, AvailableE5: 100, AmountE5: 10})
			if (e != nil) != tc.bad || r.State != tc.state {
				t.Fatalf("%+v %v", r, e)
			}
		})
	}
	if result, e := adapter.Capture(context.Background(), CaptureCommand{AmountE5: 1, AuthorizedE5: 2, Currency: "BRL", AuthorizedCurrency: "BRL", FundingMethod: "DEMO_SYNC"}); e != nil || result.State != "CONFIRMED" {
		t.Fatal("valid partial capture rejected")
	}
}
func TestLocalSafetyConfiguration(t *testing.T) {
	for _, s := range []string{"https://api.uber.com", "http://attacker.invalid", "http://user:pass@localhost", "http://127.0.0.1/?credential=x"} {
		if LocalURL(s) == nil {
			t.Fatalf("unsafe URL accepted: %s", s)
		}
	}
	for _, s := range []string{"http://localhost:9090", "http://127.0.0.1:9090/token", "http://mock-uber:9090"} {
		if e := LocalURL(s); e != nil {
			t.Fatal(e)
		}
	}
	t.Setenv("MODE", "production")
	if _, e := LoadConfig(); e == nil {
		t.Fatal("production mode accepted")
	}
}
func TestTokenProviderCoalescesAndRefreshes(t *testing.T) {
	var calls atomic.Int64
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		_ = r.ParseForm()
		if r.PostForm.Get("scope") != "payments.deposits" {
			t.Error("wrong token scope")
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"access_token":"temporary","expires_in":30}`)
	}))
	defer s.Close()
	p := &UberTokenProvider{entries: map[string]cachedToken{}}
	a := &App{Config: Config{TokenURL: s.URL}, Client: httpClient()}
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			v, e := p.Get(context.Background(), a, "payments.deposits")
			if e != nil || v != "temporary" {
				t.Errorf("token=%q err=%v", v, e)
			}
		}()
	}
	wg.Wait()
	if calls.Load() != 1 {
		t.Fatalf("requests not coalesced: %d", calls.Load())
	}
	p.mu.Lock()
	p.entries["payments.deposits"] = cachedToken{"expired", time.Now().Add(-time.Second)}
	p.mu.Unlock()
	if _, e := p.Get(context.Background(), a, "payments.deposits"); e != nil {
		t.Fatal(e)
	}
	if calls.Load() != 2 {
		t.Fatal("expired token was not refreshed")
	}
}

func TestRoutePreservesOpaqueEncodedIDs(t *testing.T) {
	p, op, id := route("/charge/opaque%2Fid%20with%3Fchars/capture", "POST")
	if p != "/charge/{id}/capture" || op != "capture" || id != "opaque/id with?chars" {
		t.Fatalf("%q %q %q", p, op, id)
	}
}
