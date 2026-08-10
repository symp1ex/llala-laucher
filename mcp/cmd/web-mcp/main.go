package main

import (
	"context"
	"fmt"
	"net/http"
	"os"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"llala-launcher/mcp/internal/config"
	"llala-launcher/mcp/internal/fetch"
	"llala-launcher/mcp/internal/search"
	webserver "llala-launcher/mcp/internal/server"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "web-mcp: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, check, err := config.Parse(os.Args[1:], os.Stderr)
	if err != nil {
		return err
	}
	if check {
		return nil
	}
	searcher, err := search.New(
		cfg.SearXNGURL,
		&http.Client{Timeout: cfg.Timeout},
		cfg.MaxResults,
	)
	if err != nil {
		return err
	}
	server := webserver.New(searcher, fetch.New(cfg.Timeout))
	if err := server.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		return fmt.Errorf("MCP stdio server: %w", err)
	}
	return nil
}
