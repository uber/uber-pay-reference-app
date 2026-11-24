package gateway

import (
	"net/url"
	"strings"
)

func (c Config) allowedReturn(value string) bool {
	if strings.ContainsAny(value, "\r\n\\") {
		return false
	}
	u, e := url.Parse(value)
	if e != nil || u.User != nil || u.Fragment != "" || u.Host == "" {
		return false
	}
	if u.Scheme != "https" && u.Scheme != "http" && u.Scheme != "uber" {
		return false
	}
	for _, raw := range c.ReturnURLs {
		r, e := url.Parse(strings.TrimSpace(raw))
		if e != nil {
			continue
		}
		if u.Scheme == r.Scheme && strings.EqualFold(u.Host, r.Host) && (u.Path == r.Path || strings.HasSuffix(r.Path, "/") && strings.HasPrefix(u.Path, r.Path)) && r.RawQuery == "" && r.Fragment == "" && !strings.Contains(u.Path, "..") {
			return true
		}
	}
	return false
}
