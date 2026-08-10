package config

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"time"
)

const (
	DefaultSearXNGURL = "http://127.0.0.1:8080"
	DefaultMaxResults = 8
	DefaultTimeout    = 15 * time.Second
)

type Config struct {
	SearXNGURL string
	MaxResults int
	Timeout    time.Duration
}

func Parse(args []string, stderr io.Writer) (Config, bool, error) {
	fs := flag.NewFlagSet("web-mcp", flag.ContinueOnError)
	fs.SetOutput(stderr)
	searxngURL := fs.String("searxng-url", DefaultSearXNGURL, "SearXNG base URL")
	maxResults := fs.Int("max-results", DefaultMaxResults, "default search result count (1-20)")
	timeoutSeconds := fs.Float64("timeout", DefaultTimeout.Seconds(), "HTTP timeout in seconds")
	check := fs.Bool("check", false, "validate startup without running MCP")
	if err := fs.Parse(args); err != nil {
		return Config{}, false, err
	}
	if fs.NArg() != 0 {
		return Config{}, false, fmt.Errorf("unexpected arguments: %v", fs.Args())
	}
	cfg := Config{
		SearXNGURL: *searxngURL,
		MaxResults: *maxResults,
		Timeout:    time.Duration(*timeoutSeconds * float64(time.Second)),
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, false, err
	}
	return cfg, *check, nil
}

func (c Config) Validate() error {
	parsed, err := url.Parse(c.SearXNGURL)
	if err != nil {
		return fmt.Errorf("invalid SearXNG URL: %w", err)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return errors.New("SearXNG URL must use http or https")
	}
	if parsed.Hostname() == "" {
		return errors.New("SearXNG URL must include a host")
	}
	if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return errors.New("SearXNG URL must not contain credentials, query, or fragment")
	}
	if c.MaxResults < 1 || c.MaxResults > 20 {
		return errors.New("max-results must be between 1 and 20")
	}
	if c.Timeout < time.Second || c.Timeout > 120*time.Second {
		return errors.New("timeout must be between 1 and 120 seconds")
	}
	return nil
}
