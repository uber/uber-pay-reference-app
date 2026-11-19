package gateway

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type Object = map[string]any

func DecodeJSON(raw []byte) (any, error) {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	var parse func() (any, error)
	depth := 0
	parse = func() (any, error) {
		depth++
		defer func() { depth-- }()
		if depth > 80 {
			return nil, errors.New("JSON nesting limit")
		}
		t, e := d.Token()
		if e != nil {
			return nil, e
		}
		switch v := t.(type) {
		case json.Delim:
			if v == '{' {
				o := Object{}
				for d.More() {
					key, e := d.Token()
					if e != nil {
						return nil, e
					}
					k, ok := key.(string)
					if !ok {
						return nil, errors.New("invalid object key")
					}
					if _, ok = o[k]; ok {
						return nil, errors.New("duplicate JSON property")
					}
					val, e := parse()
					if e != nil {
						return nil, e
					}
					o[k] = val
				}
				_, e = d.Token()
				return o, e
			}
			if v == '[' {
				a := []any{}
				for d.More() {
					val, e := parse()
					if e != nil {
						return nil, e
					}
					a = append(a, val)
				}
				_, e = d.Token()
				return a, e
			}
			return nil, errors.New("invalid JSON delimiter")
		default:
			return t, nil
		}
	}
	v, e := parse()
	if e != nil {
		return nil, e
	}
	if _, e = d.Token(); e != io.EOF {
		return nil, errors.New("trailing JSON content")
	}
	return v, nil
}
func object(x any) Object {
	if o, ok := x.(map[string]any); ok {
		return o
	}
	return Object{}
}
func str(o Object, k string) string { s, _ := o[k].(string); return s }
func number(x any) (int64, error) {
	switch n := x.(type) {
	case json.Number:
		return strconv.ParseInt(n.String(), 10, 64)
	case int32:
		return int64(n), nil
	case int64:
		return n, nil
	case int:
		return int64(n), nil
	}
	return 0, errors.New("expected exact int64")
}
func encode(x any) string { b, _ := json.Marshal(x); return string(b) }

type Contract struct{ Spec Object }

func (c Contract) resolve(ref string) Object {
	var n any = c.Spec
	for _, p := range strings.Split(strings.TrimPrefix(ref, "#/"), "/") {
		p = strings.ReplaceAll(strings.ReplaceAll(p, "~1", "/"), "~0", "~")
		n = object(n)[p]
	}
	return object(n)
}
func (c Contract) Validate(s Object, v any) error { return c.validate(s, v, "$", 0) }
func (c Contract) validate(s Object, v any, path string, depth int) error {
	if depth > 80 {
		return errors.New("schema recursion limit")
	}
	if ref := str(s, "$ref"); ref != "" {
		resolved := c.resolve(ref)
		if len(resolved) == 0 {
			return fmt.Errorf("%s: unresolved schema reference", path)
		}
		return c.validate(resolved, v, path, depth+1)
	}
	for _, op := range []string{"allOf", "oneOf", "anyOf"} {
		if choices, ok := s[op].([]any); ok {
			valid := 0
			for _, choice := range choices {
				if c.validate(object(choice), v, path, depth+1) == nil {
					valid++
				}
			}
			if op == "allOf" && valid != len(choices) || op == "oneOf" && valid != 1 || op == "anyOf" && valid == 0 {
				return fmt.Errorf("%s: %s mismatch", path, op)
			}
		}
	}
	if enums, ok := s["enum"].([]any); ok {
		match := false
		for _, candidate := range enums {
			if encode(candidate) == encode(v) {
				match = true
			}
		}
		if !match {
			return fmt.Errorf("%s: invalid enum", path)
		}
	}
	typ := str(s, "type")
	if v == nil && s["nullable"] == true {
		return nil
	}
	switch typ {
	case "object":
		o, ok := v.(map[string]any)
		if !ok {
			return fmt.Errorf("%s: expected object", path)
		}
		if req, ok := s["required"].([]any); ok {
			for _, k := range req {
				if _, ok := o[k.(string)]; !ok {
					return fmt.Errorf("%s: missing %s", path, k)
				}
			}
		}
		properties := object(s["properties"])
		for k, val := range o {
			if schema, ok := properties[k]; ok {
				if e := c.validate(object(schema), val, path+"."+k, depth+1); e != nil {
					return e
				}
			} else if s["additionalProperties"] == false {
				return fmt.Errorf("%s: extra property", path)
			} else if extra, ok := s["additionalProperties"].(map[string]any); ok {
				if e := c.validate(extra, val, path+"."+k, depth+1); e != nil {
					return e
				}
			}
		}
	case "array":
		a, ok := v.([]any)
		if !ok {
			return fmt.Errorf("%s: expected array", path)
		}
		for _, item := range a {
			if e := c.validate(object(s["items"]), item, path+"[]", depth+1); e != nil {
				return e
			}
		}
	case "string":
		text, ok := v.(string)
		if !ok {
			return fmt.Errorf("%s: expected string", path)
		}
		for _, key := range []string{"minLength", "maxLength"} {
			if n, e := number(s[key]); e == nil && (key == "minLength" && int64(len([]rune(text))) < n || key == "maxLength" && int64(len([]rune(text))) > n) {
				return fmt.Errorf("%s: invalid length", path)
			}
		}
		if pattern := str(s, "pattern"); pattern != "" {
			if ok, e := regexp.MatchString(pattern, text); e != nil || !ok {
				return fmt.Errorf("%s: invalid pattern", path)
			}
		}
		switch str(s, "format") {
		case "date-time":
			if _, e := time.Parse(time.RFC3339Nano, text); e != nil {
				return fmt.Errorf("%s: invalid date-time", path)
			}
		case "uri":
			if u, e := url.Parse(text); e != nil || u.Scheme == "" {
				return fmt.Errorf("%s: invalid URI", path)
			}
		}
	case "integer":
		if _, e := number(v); e != nil {
			return fmt.Errorf("%s: invalid int64", path)
		}
	case "number":
		if _, ok := v.(json.Number); !ok {
			return fmt.Errorf("%s: expected number", path)
		}
	case "boolean":
		if _, ok := v.(bool); !ok {
			return fmt.Errorf("%s: expected boolean", path)
		}
	}
	return nil
}
func (c Contract) operation(path, method string) Object {
	return object(object(object(c.Spec["paths"])[path])[method])
}
func (c Contract) Request(path, method string, v any) error {
	s := object(object(object(object(c.operation(path, method)["requestBody"])["content"])["application/json"])["schema"])
	return c.Validate(s, v)
}
func (c Contract) Response(path, method string, status int, v any) error {
	r := object(object(c.operation(path, method)["responses"])[strconv.Itoa(status)])
	if ref := str(r, "$ref"); ref != "" {
		r = c.resolve(ref)
	}
	s := object(object(object(r["content"])["application/json"])["schema"])
	if len(s) == 0 {
		return nil
	}
	return c.Validate(s, v)
}
