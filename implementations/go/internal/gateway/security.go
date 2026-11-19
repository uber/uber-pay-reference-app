package gateway

import (
	"crypto"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"time"
)

var signaturePart = regexp.MustCompile(`^\s*([A-Za-z][A-Za-z0-9_-]*)="([^"\r\n]*)"\s*$`)

func ParsePublicKey(b []byte) (*rsa.PublicKey, error) {
	block, _ := pem.Decode(b)
	if block == nil {
		return nil, errors.New("invalid public key PEM")
	}
	if k, e := x509.ParsePKIXPublicKey(block.Bytes); e == nil {
		if key, ok := k.(*rsa.PublicKey); ok && key.N.BitLen() >= 2048 {
			return key, nil
		}
	}
	if k, e := x509.ParsePKCS1PublicKey(block.Bytes); e == nil && k.N.BitLen() >= 2048 {
		return k, nil
	}
	return nil, errors.New("an RSA public key of at least 2048 bits is required")
}
func VerifySignature(r *http.Request, raw []byte, keys map[string]*rsa.PublicKey, now time.Time, maxSkew time.Duration) error {
	if len(r.Header.Values("Signature")) != 1 || len(r.Header.Values("Digest")) != 1 || len(r.Header.Values("Date")) != 1 {
		return errors.New("missing or repeated signature headers")
	}
	header := r.Header.Get("Signature")
	if len(header) > 8192 {
		return errors.New("signature too large")
	}
	parts := map[string]string{}
	for _, p := range strings.Split(header, ",") {
		m := signaturePart.FindStringSubmatch(p)
		if m == nil {
			return errors.New("malformed signature")
		}
		if _, ok := parts[m[1]]; ok {
			return errors.New("duplicate signature parameter")
		}
		parts[m[1]] = m[2]
	}
	if len(parts) != 4 || parts["algorithm"] != "rsa-sha256" || parts["headers"] != "(request-target) host date digest" {
		return errors.New("unsupported signature parameters")
	}
	key := keys[parts["keyId"]]
	if key == nil {
		return errors.New("unknown signing key")
	}
	date, err := http.ParseTime(r.Header.Get("Date"))
	if err != nil || now.Sub(date) > maxSkew || date.Sub(now) > maxSkew {
		return errors.New("signature outside freshness window")
	}
	digest := sha256.Sum256(raw)
	want := "SHA-256=" + base64.StdEncoding.EncodeToString(digest[:])
	if subtle.ConstantTimeCompare([]byte(want), []byte(r.Header.Get("Digest"))) != 1 {
		return errors.New("body digest mismatch")
	}
	target := r.RequestURI
	if target == "" {
		target = r.URL.RequestURI()
	}
	if !strings.HasPrefix(target, "/") {
		return errors.New("invalid request target")
	}
	payload := fmt.Sprintf("(request-target): %s %s\nhost: %s\ndate: %s\ndigest: %s", strings.ToLower(r.Method), target, r.Host, r.Header.Get("Date"), want)
	signature, err := base64.StdEncoding.DecodeString(parts["signature"])
	if err != nil {
		return errors.New("invalid signature encoding")
	}
	hashed := sha256.Sum256([]byte(payload))
	if rsa.VerifyPKCS1v15(key, crypto.SHA256, hashed[:], signature) != nil {
		return errors.New("signature verification failed")
	}
	return nil
}
func fingerprint(s string) string { h := sha256.Sum256([]byte(s)); return hex.EncodeToString(h[:]) }
