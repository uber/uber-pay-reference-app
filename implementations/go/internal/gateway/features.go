package gateway

import (
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

type ReferenceFeatures struct{ Data, Aliases Object }

func LoadFeatures() (ReferenceFeatures, error) {
	file := env("REFERENCE_FEATURES_FILE", "")
	if file == "" {
		dir, _ := os.Getwd()
		for {
			candidate := filepath.Join(dir, "contracts/reference-features.json")
			if _, err := os.Stat(candidate); err == nil {
				file = candidate
				break
			}
			up := filepath.Dir(dir)
			if up == dir {
				return ReferenceFeatures{}, errors.New("missing reference features")
			}
			dir = up
		}
	}
	b, e := os.ReadFile(file)
	if e != nil {
		return ReferenceFeatures{}, e
	}
	v, e := DecodeJSON(b)
	if e != nil {
		return ReferenceFeatures{}, e
	}
	f := ReferenceFeatures{Data: object(v), Aliases: Object{}}
	if file := env("FUNDING_METHODS_FILE", ""); file != "" {
		b, e = os.ReadFile(file)
		if e != nil {
			return f, e
		}
		v, e = DecodeJSON(b)
		if e != nil {
			return f, e
		}
		f.Aliases = object(v)
	}
	for code, value := range f.Aliases {
		a := object(value)
		for key := range a {
			if key != "method" && key != "countries" && key != "currencies" && key != "callback_mode" {
				return f, errors.New("unsupported funding policy field")
			}
		}
		if mode, exists := a["callback_mode"]; exists {
			if mode != "charge" && mode != "deposit" {
				return f, errors.New("invalid callback policy")
			}
			if mode == "deposit" {
				behavior := str(object(object(f.Data["methods"])[str(a, "method")]), "behavior")
				if behavior != "webview" && behavior != "qr" && behavior != "pending" && behavior != "pending_reject" {
					return f, errors.New("deposit callback policy requires asynchronous profile")
				}
			}
		}
		if code == "" || object(f.Data["methods"])[str(a, "method")] == nil {
			return f, errors.New("invalid funding alias")
		}
		for _, key := range []string{"countries", "currencies"} {
			list, ok := a[key].([]any)
			if !ok || len(list) == 0 {
				return f, errors.New("invalid funding profile")
			}
			for _, s := range list {
				pattern := `^[A-Z]{2}$`
				if key == "currencies" {
					pattern = `^[A-Z]{3}$`
				}
				text, ok := s.(string)
				if !ok || !regexp.MustCompile(pattern).MatchString(text) {
					return f, errors.New("invalid funding profile")
				}
			}
		}
	}
	return f, nil
}
func (f ReferenceFeatures) Method(code, country, currency string) (Object, error) {
	a := object(f.Aliases[code])
	key := code
	if str(a, "method") != "" {
		key = str(a, "method")
	}
	raw, ok := object(f.Data["methods"])[key]
	if !ok {
		return nil, errors.New("unknown funding method")
	}
	p := Object{}
	for k, v := range object(raw) {
		p[k] = v
	}
	for k, v := range a {
		p[k] = v
	}
	p["code"] = code
	p["method"] = key
	for k, wanted := range map[string]string{"countries": country, "currencies": currency} {
		if wanted != "" {
			found := false
			for _, x := range p[k].([]any) {
				if x == wanted {
					found = true
				}
			}
			if !found {
				return nil, errors.New("funding country/currency mismatch")
			}
		}
	}
	return p, nil
}
func locale(value string) string {
	if strings.HasPrefix(strings.ToLower(strings.TrimSpace(value)), "pt") {
		return "pt-BR"
	}
	return "en-US"
}
func (f ReferenceFeatures) Text(language, key string) string {
	return str(object(object(f.Data["translations"])[locale(language)]), key)
}
