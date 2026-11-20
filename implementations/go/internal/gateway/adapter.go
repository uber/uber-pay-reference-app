package gateway

import (
	"context"
	"errors"
)

// PspAdapter is the native processor seam. The gateway owns Uber wire formats,
// durable identity and replay; an adopter supplies processor idempotency and
// reconciliation before enabling any nonlocal mode (deliberately unsupported).
type PspAdapter interface {
	LocalOnly() bool
	GetBalance(context.Context, BalanceQuery) (BalanceResult, error)
	LookupOperation(context.Context, OperationQuery) (ProviderResult, error)
	Charge(context.Context, PaymentCommand) (ProviderResult, error)
	Payout(context.Context, PaymentCommand) (ProviderResult, error)
	Capture(context.Context, CaptureCommand) (ProviderResult, error)
	Cancel(context.Context, string) error
	Refund(context.Context, PaymentCommand) (ProviderResult, error)
}
type PaymentCommand struct {
	Request                                                          Object
	ID, Account, Currency, FundingMethod, CaptureMethod, ProviderKey string
	AmountE5, AvailableE5                                            int64
}
type CaptureCommand struct {
	ID, ProviderKey, Currency, AuthorizedCurrency, FundingMethod string
	AmountE5, AuthorizedE5                                       int64
}
type ProviderResult struct{ State, Detail, FinalState, FinalDetail string }
type DemoPspAdapter struct{}

func demoDecision(c PaymentCommand, payout bool) (ProviderResult, error) {
	if e := validatePaymentData(c.Request, c.AmountE5); e != nil {
		return ProviderResult{}, e
	}
	f, e := LoadFeatures()
	if e != nil {
		return ProviderResult{}, e
	}
	p, e := f.Method(c.FundingMethod, str(c.Request, "country_iso2"), c.Currency)
	if e != nil {
		return ProviderResult{}, e
	}
	behavior, detail := str(p, "behavior"), str(p, "detail")
	if detail != "" {
		key := "charge_reasons"
		if payout {
			key = "payout_reasons"
		}
		valid := false
		for _, x := range f.Data[key].([]any) {
			if x == detail {
				valid = true
			}
		}
		if !valid {
			return ProviderResult{}, errors.New("reason not valid for operation")
		}
	}
	if c.CaptureMethod == "MANUAL" && (behavior == "pending" || behavior == "pending_reject" || behavior == "webview" || behavior == "qr") {
		return ProviderResult{}, errors.New("manual authorization must be synchronous")
	}
	if c.AvailableE5 < c.AmountE5 {
		return ProviderResult{State: "REJECTED", Detail: "INSUFFICIENT_BALANCE"}, nil
	}
	if behavior == "reject" {
		return ProviderResult{State: "REJECTED", Detail: detail}, nil
	}
	if behavior == "pending" || behavior == "pending_reject" || behavior == "webview" || behavior == "qr" {
		final := "CONFIRMED"
		if behavior == "pending_reject" {
			final = "REJECTED"
		}
		return ProviderResult{State: "PENDING", FinalState: final, FinalDetail: detail}, nil
	}
	if c.CaptureMethod == "MANUAL" {
		return ProviderResult{State: "AUTHORIZED"}, nil
	}
	return ProviderResult{State: "CONFIRMED"}, nil
}
func (DemoPspAdapter) Charge(_ context.Context, c PaymentCommand) (ProviderResult, error) {
	return demoDecision(c, false)
}
func (DemoPspAdapter) Payout(_ context.Context, c PaymentCommand) (ProviderResult, error) {
	c.CaptureMethod = "AUTOMATIC"
	return demoDecision(c, true)
}
func (DemoPspAdapter) Capture(_ context.Context, c CaptureCommand) (ProviderResult, error) {
	if c.Currency != c.AuthorizedCurrency || c.AmountE5 <= 0 || c.AmountE5 > c.AuthorizedE5 {
		return ProviderResult{}, errors.New("capture must not exceed the authorization")
	}
	f, e := LoadFeatures()
	if e != nil {
		return ProviderResult{}, e
	}
	p, e := f.Method(c.FundingMethod, "", "")
	if e != nil {
		return ProviderResult{}, e
	}
	if str(p, "behavior") == "capture_hard" || str(p, "behavior") == "capture_soft" {
		detail := "BANK_HARD_ERROR"
		if str(p, "behavior") == "capture_soft" {
			detail = "BANK_SOFT_ERROR"
		}
		return ProviderResult{State: "REJECTED", Detail: detail}, nil
	}
	return ProviderResult{State: "CONFIRMED"}, nil
}
func (DemoPspAdapter) Cancel(context.Context, string) error { return nil }
func (DemoPspAdapter) Refund(_ context.Context, c PaymentCommand) (ProviderResult, error) {
	f, e := LoadFeatures()
	if e != nil {
		return ProviderResult{}, e
	}
	p, e := f.Method(c.FundingMethod, "", "")
	if e != nil {
		return ProviderResult{}, e
	}
	behavior := str(p, "behavior")
	if behavior == "refund_sync_reject" {
		return ProviderResult{State: "REJECTED"}, nil
	}
	if behavior == "pending" || behavior == "pending_reject" || behavior == "refund_reversal" || behavior == "refund_reject" {
		final := "CONFIRMED"
		if behavior == "refund_reject" {
			final = "REJECTED"
		}
		return ProviderResult{State: "PENDING", FinalState: final, FinalDetail: "MERCHANT_REJECTED"}, nil
	}
	return ProviderResult{State: "CONFIRMED"}, nil
}

// These inputs include the durable local fixture snapshot; a production port
// would require a separate intent/reconciliation boundary before network I/O.
type BalanceQuery struct {
	Request                Object
	Account, Currency      string
	AvailableE5, BalanceE5 int64
}
type BalanceResult struct {
	AvailableE5, BalanceE5 int64
	Currency               string
}
type OperationQuery struct{ ProviderKey, Reference, State string }

func (DemoPspAdapter) GetBalance(_ context.Context, q BalanceQuery) (BalanceResult, error) {
	if e := validatePaymentData(q.Request, 0); e != nil {
		return BalanceResult{}, e
	}
	return BalanceResult{q.AvailableE5, q.BalanceE5, q.Currency}, nil
}
func (DemoPspAdapter) LookupOperation(_ context.Context, q OperationQuery) (ProviderResult, error) {
	return ProviderResult{State: q.State}, nil
}
func (DemoPspAdapter) LocalOnly() bool { return true }

type WalletProvider interface {
	ConsentIdentity(context.Context, []any) (AuthorizedWallet, error)
	ExchangeWalletReference(context.Context, Object) (AuthorizedWallet, error)
	LocalOnly() bool
}
type AuthorizedWallet struct{ AccountID, Fingerprint, DisplayName string }
type DemoWalletProvider struct{}

func (DemoWalletProvider) ConsentIdentity(_ context.Context, details []any) (AuthorizedWallet, error) {
	for _, d := range details {
		if id := str(object(d), "account_id"); id != "" && id != "demo-account" {
			return AuthorizedWallet{}, errors.New("unknown local identity")
		}
	}
	return AuthorizedWallet{"demo-account", "demo-fingerprint", "Demo wallet"}, nil
}
func (DemoWalletProvider) ExchangeWalletReference(_ context.Context, b Object) (AuthorizedWallet, error) {
	if str(b, "payment_identifier") != "demo-account" {
		return AuthorizedWallet{}, errors.New("unknown synthetic wallet reference")
	}
	return AuthorizedWallet{"demo-account", "demo-fingerprint", "Demo wallet"}, nil
}
func (DemoWalletProvider) LocalOnly() bool { return true }
