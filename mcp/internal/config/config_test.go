package config

import (
	"bytes"
	"testing"
	"time"
)

func TestParseDefaultsAndCheck(t *testing.T) {
	cfg, check, err := Parse([]string{"--check"}, &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	if !check || cfg.SearXNGURL != DefaultSearXNGURL || cfg.MaxResults != 8 || cfg.Timeout != 15*time.Second {
		t.Fatalf("unexpected config: %+v check=%v", cfg, check)
	}
}

func TestParseValidatesValues(t *testing.T) {
	tests := [][]string{
		{"--searxng-url", "file:///tmp"},
		{"--searxng-url", "http://user:pass@example.com"},
		{"--max-results", "21"},
		{"--timeout", "0.5"},
		{"unexpected"},
	}
	for _, args := range tests {
		if _, _, err := Parse(args, &bytes.Buffer{}); err == nil {
			t.Fatalf("expected parse error for %v", args)
		}
	}
}
