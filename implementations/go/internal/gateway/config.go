package gateway

import (
	"crypto/rsa"
	"encoding/base64"
	"errors"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	ProcessorPauseAfterPrepare                                                                bool
	ProcessorMode, ProcessorURL, ProcessorToken, ProcessorKey                                 string
	ProcessorTimeout, ProcessorLease, ProcessorWait, ProcessorPoll                            time.Duration
	ProcessorMaxFailures                                                                      int
	Host, Port, BasePath, DatabaseURL, ClientID, ClientSecret, UberClientID, UberClientSecret string
	PublicBaseURL, CallbackBaseURL, TokenURL                                                  string
	Redirects                                                                                 map[string]bool
	Keys                                                                                      map[string]*rsa.PublicKey
	MaxSkew, AsyncDelay, Interval, Retry, ActionTTL                                           time.Duration
	MaxAttempts                                                                               int
	TestControl                                                                               bool
	TestSecret                                                                                string
	OAuthRequired                                                                             bool
	StrictAccountName                                                                         bool
	RefreshTTL                                                                                time.Duration
	ContractFile, MigrationFile                                                               string
}

func env(k, d string) string {
	if v, ok := os.LookupEnv(k); ok {
		return v
	}
	return d
}
func duration(k, d string, m time.Duration) (time.Duration, error) {
	n, e := strconv.Atoi(env(k, d))
	if e != nil || n < 1 {
		return 0, errors.New("invalid duration: " + k)
	}
	return time.Duration(n) * m, nil
}
func LocalURL(s string) error {
	u, e := url.Parse(s)
	if e != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Scheme != "http" {
		return errors.New("local URLs require HTTP with no credentials/query/fragment")
	}
	switch strings.ToLower(u.Hostname()) {
	case "localhost", "127.0.0.1", "::1", "mock-uber", "processor":
		return nil
	}
	return errors.New("nonlocal outbound host refused")
}
func LoadConfig() (Config, error) {
	c := Config{}
	if env("MODE", "local") != "local" {
		return c, errors.New("only synthetic local mode is implemented; sandbox/live requires a reviewed real adapter")
	}
	c.ProcessorMode = env("PROCESSOR_MODE", "local")
	if c.ProcessorMode != "local" && c.ProcessorMode != "http" {
		return c, errors.New("invalid PROCESSOR_MODE")
	}
	c.ProcessorURL = strings.TrimRight(env("PROCESSOR_URL", "http://127.0.0.1:9080"), "/")
	if e := LocalURL(c.ProcessorURL); e != nil {
		return c, e
	}
	c.ProcessorToken = env("PROCESSOR_AUTH_TOKEN", "")
	c.ProcessorKey = env("PROCESSOR_STATE_KEY", "")
	stateKey, ke := base64.StdEncoding.DecodeString(c.ProcessorKey)
	if c.ProcessorMode == "http" && (len(c.ProcessorToken) < 32 || ke != nil || len(stateKey) != 32) {
		return c, errors.New("HTTP processor requires explicit credentials and persistent 256-bit state key")
	}
	for _, item := range []struct {
		k, d string
		p    *time.Duration
	}{{"PROCESSOR_TIMEOUT_MS", "2000", &c.ProcessorTimeout}, {"PROCESSOR_LEASE_MS", "15000", &c.ProcessorLease}, {"PROCESSOR_WAIT_MS", "250", &c.ProcessorWait}, {"PROCESSOR_POLL_MS", "200", &c.ProcessorPoll}} {
		var e error
		*item.p, e = duration(item.k, item.d, time.Millisecond)
		if e != nil {
			return c, e
		}
	}
	var pe error
	c.ProcessorMaxFailures, pe = strconv.Atoi(env("PROCESSOR_MAX_FAILURES", "8"))
	if pe != nil || c.ProcessorMaxFailures < 1 {
		return c, errors.New("invalid processor budget")
	}
	c.Host = env("HOST", "127.0.0.1")
	c.Port = env("PORT", "8080")
	if n, e := strconv.Atoi(c.Port); e != nil || n < 1 || n > 65535 {
		return c, errors.New("invalid PORT")
	}
	c.BasePath = env("BASE_PATH", "/uberpay/v1")
	if !strings.HasPrefix(c.BasePath, "/") || strings.HasSuffix(c.BasePath, "/") || strings.ContainsAny(c.BasePath, "?#\\") || strings.Contains(c.BasePath, "..") {
		return c, errors.New("invalid BASE_PATH")
	}
	c.DatabaseURL = env("DATABASE_URL", "postgresql://uberpay:uberpay@127.0.0.1:5432/uberpay")
	database, e := url.Parse(c.DatabaseURL)
	if e != nil || (database.Scheme != "postgresql" && database.Scheme != "postgres") {
		return c, errors.New("invalid DATABASE_URL")
	}
	switch strings.ToLower(database.Hostname()) {
	case "localhost", "127.0.0.1", "::1", "db", "postgres":
	default:
		return c, errors.New("local mode refuses a nonlocal database")
	}

	c.ClientID = env("CLIENT_ID", "demo-client")
	c.ClientSecret = env("CLIENT_SECRET", "demo-secret")
	c.UberClientID = env("UBER_CLIENT_ID", "demo-uber-client")
	c.UberClientSecret = env("UBER_CLIENT_SECRET", "demo-uber-secret")
	if c.ClientID == "" || c.ClientSecret == "" || c.UberClientID == "" || c.UberClientSecret == "" {
		return c, errors.New("client identities and secrets must not be empty")
	}

	c.PublicBaseURL = env("PUBLIC_BASE_URL", "http://127.0.0.1:"+c.Port+c.BasePath)
	c.CallbackBaseURL = env("UBER_CALLBACK_BASE_URL", "http://127.0.0.1:9090")
	c.TokenURL = env("UBER_TOKEN_URL", "http://127.0.0.1:9090/oauth/token")
	for _, u := range []string{c.PublicBaseURL, c.CallbackBaseURL, c.TokenURL} {
		if e := LocalURL(u); e != nil {
			return c, e
		}
	}
	c.Redirects = map[string]bool{}
	for _, v := range strings.Split(env("ALLOWED_REDIRECT_URIS", "http://127.0.0.1:9090/return,uber://ubercash_topup"), ",") {
		c.Redirects[strings.TrimSpace(v)] = true
	}
	c.MaxSkew, e = duration("SIGNATURE_MAX_SKEW_SECONDS", "300", time.Second)
	if e != nil {
		return c, e
	}
	c.AsyncDelay, e = duration("ASYNC_DELAY_MS", "500", time.Millisecond)
	if e != nil {
		return c, e
	}
	c.Interval, e = duration("WORKER_INTERVAL_MS", "100", time.Millisecond)
	if e != nil {
		return c, e
	}
	c.Retry, e = duration("CALLBACK_RETRY_MS", "200", time.Millisecond)
	if e != nil {
		return c, e
	}
	c.MaxAttempts, e = strconv.Atoi(env("CALLBACK_MAX_ATTEMPTS", "5"))
	if e != nil || c.MaxAttempts < 1 || c.MaxAttempts > 100 {
		return c, errors.New("invalid callback attempt limit")
	}
	c.ProcessorPauseAfterPrepare = env("ENABLE_TEST_CONTROL", "false") == "true" && env("PROCESSOR_TEST_PAUSE_AFTER_PREPARE", "false") == "true"
	c.TestControl = env("ENABLE_TEST_CONTROL", "false") == "true"
	c.TestSecret = env("TEST_CONTROL_SECRET", "")
	if c.TestControl && len(c.TestSecret) < 16 {
		return c, errors.New("test control requires a random secret of at least 16 characters")
	}
	c.ActionTTL, e = duration("ACTION_TTL_MS", "600000", time.Millisecond)
	if e != nil {
		return c, e
	}
	c.RefreshTTL, e = duration("REFRESH_TOKEN_TTL_SECONDS", "2592000", time.Second)
	if e != nil {
		return c, e
	}
	c.StrictAccountName = env("STRICT_ACCOUNT_NAME", "false") == "true"
	c.OAuthRequired = env("OAUTH_REQUIRED", "false") == "true"
	c.ContractFile = env("CONTRACT_FILE", "contracts/runtime.json")
	c.MigrationFile = env("MIGRATION_FILE", "storage/migrations/001_init.sql")
	b, e := os.ReadFile(filepath.Clean(env("UBER_PUBLIC_KEY_FILE", ".local/uber-public.pem")))
	if e != nil {
		return c, e
	}
	key, e := ParsePublicKey(b)
	if e != nil {
		return c, e
	}
	c.Keys = map[string]*rsa.PublicKey{env("UBER_KEY_ID", "demo-uber-key"): key}
	return c, nil
}
func httpClient() *http.Client {
	return &http.Client{Timeout: 5 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("redirect refused") }}
}
