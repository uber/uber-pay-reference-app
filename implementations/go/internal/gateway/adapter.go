package gateway

import (
	"context"
	"errors"
	"strings"
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
	Capture(context.Context, CaptureCommand) error
	Cancel(context.Context, string) error
	Refund(context.Context, PaymentCommand) (ProviderResult, error)
}
type PaymentCommand struct {
	ID, Account, Currency, FundingMethod, CaptureMethod, ProviderKey string
	AmountE5, AvailableE5                                            int64
}
type CaptureCommand struct {
	ID, ProviderKey, Currency, AuthorizedCurrency string
	AmountE5, AuthorizedE5                        int64
}
type ProviderResult struct{ State, Detail string }
type DemoPspAdapter struct{}

func (DemoPspAdapter) Charge(_ context.Context, c PaymentCommand) (ProviderResult, error) {
	if !knownFunding(c.FundingMethod) {
		return ProviderResult{}, errors.New("unsupported demo funding method")
	}
	if c.CaptureMethod == "MANUAL" && c.FundingMethod != "DEMO_SYNC" && c.FundingMethod != "DEMO_REJECT" {
		return ProviderResult{}, errors.New("manual authorization must be synchronous")
	}
	if c.FundingMethod == "DEMO_REJECT" || c.AvailableE5 < c.AmountE5 {
		return ProviderResult{"REJECTED", "INSUFFICIENT_BALANCE"}, nil
	}
	if c.FundingMethod != "DEMO_SYNC" {
		return ProviderResult{State: "PENDING"}, nil
	}
	if c.CaptureMethod == "MANUAL" {
		return ProviderResult{State: "AUTHORIZED"}, nil
	}
	return ProviderResult{State: "CONFIRMED"}, nil
}
func (d DemoPspAdapter) Payout(ctx context.Context, c PaymentCommand) (ProviderResult, error) {
	c.CaptureMethod = "AUTOMATIC"
	return d.Charge(ctx, c)
}
func (DemoPspAdapter) Capture(_ context.Context, c CaptureCommand) error {
	if c.Currency != c.AuthorizedCurrency || c.AmountE5 != c.AuthorizedE5 {
		return errors.New("only exact full capture is available in this reference profile")
	}
	return nil
}
func (DemoPspAdapter) Cancel(context.Context, string) error { return nil }
func (DemoPspAdapter) Refund(_ context.Context, c PaymentCommand) (ProviderResult, error) {
	if strings.HasPrefix(c.FundingMethod, "DEMO_PENDING") {
		return ProviderResult{State: "PENDING"}, nil
	}
	return ProviderResult{State: "CONFIRMED"}, nil
}

// These inputs include the durable local fixture snapshot; a production port
// would require a separate intent/reconciliation boundary before network I/O.
type BalanceQuery struct {
	Account, Currency      string
	AvailableE5, BalanceE5 int64
}
type BalanceResult struct {
	AvailableE5, BalanceE5 int64
	Currency               string
}
type OperationQuery struct{ ProviderKey, Reference, State string }

func (DemoPspAdapter) GetBalance(_ context.Context, q BalanceQuery) (BalanceResult, error) {
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
