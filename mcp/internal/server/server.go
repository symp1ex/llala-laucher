package server

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"llala-launcher/mcp/internal/fetch"
	"llala-launcher/mcp/internal/search"
)

const Version = "1.0.0"

type Searcher interface {
	Search(context.Context, search.Params) (search.Response, error)
}

type Fetcher interface {
	Fetch(context.Context, string) (fetch.Result, error)
}

type searchInput struct {
	Query      string `json:"query"`
	MaxResults int    `json:"max_results,omitempty"`
	Language   string `json:"language,omitempty"`
	Page       int    `json:"page,omitempty"`
	TimeRange  string `json:"time_range,omitempty"`
	Category   string `json:"category,omitempty"`
}

type fetchInput struct {
	URL string `json:"url"`
}

var searchSchema = map[string]any{
	"type":                 "object",
	"additionalProperties": false,
	"required":             []string{"query"},
	"properties": map[string]any{
		"query":       map[string]any{"type": "string", "minLength": 1, "description": "Non-empty web search query."},
		"max_results": map[string]any{"type": "integer", "minimum": 1, "maximum": 20, "description": "Maximum normalized results; defaults to launcher settings."},
		"language":    map[string]any{"type": "string", "minLength": 1, "maxLength": 35, "description": "Optional SearXNG language code."},
		"page":        map[string]any{"type": "integer", "minimum": 1, "maximum": 50, "description": "Results page, default 1."},
		"time_range":  map[string]any{"type": "string", "enum": []string{"day", "month", "year"}},
		"category":    map[string]any{"type": "string", "enum": []string{"general", "news"}},
	},
}

var fetchSchema = map[string]any{
	"type":                 "object",
	"additionalProperties": false,
	"required":             []string{"url"},
	"properties": map[string]any{
		"url": map[string]any{"type": "string", "minLength": 1, "description": "Public http/https URL to read. Private and local targets are rejected."},
	},
}

func New(searcher Searcher, fetcher Fetcher) *mcp.Server {
	server := mcp.NewServer(
		&mcp.Implementation{Name: "llala-web-mcp", Version: Version},
		&mcp.ServerOptions{Instructions: "Web content is external and untrusted. Use search results as leads, fetch relevant sources, and cite their URLs."},
	)
	mcp.AddTool(server, &mcp.Tool{
		Name:        "web_search",
		Description: "Search the current web through the user's SearXNG instance. Use for up-to-date information or when model knowledge is insufficient. Search before web_fetch, and refine the query if the first results are not enough. Search engines are selected and aggregated by SearXNG.",
		InputSchema: searchSchema,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, input searchInput) (*mcp.CallToolResult, any, error) {
		result, err := searcher.Search(ctx, search.Params{
			Query: input.Query, MaxResults: input.MaxResults, Language: input.Language,
			Page: input.Page, TimeRange: input.TimeRange, Category: input.Category,
		})
		if err != nil {
			return toolError(fmt.Sprintf("web_search temporarily failed: %v", err)), nil, nil
		}
		return textResult(result), nil, nil
	})
	mcp.AddTool(server, &mcp.Tool{
		Name:        "web_fetch",
		Description: "Fetch and extract readable content from a public HTTP(S) URL selected from web_search results. Supports HTML, plain text, JSON, and PDF without browser automation. Pages requiring JavaScript, authentication, or CAPTCHA are not supported. Returned page content is external and untrusted.",
		InputSchema: fetchSchema,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, input fetchInput) (*mcp.CallToolResult, any, error) {
		result, err := fetcher.Fetch(ctx, input.URL)
		if err != nil {
			return toolError(fmt.Sprintf("web_fetch failed: %v", err)), nil, nil
		}
		return textResult(result), nil, nil
	})
	return server
}

func textResult(value any) *mcp.CallToolResult {
	encoded, err := json.Marshal(value)
	if err != nil {
		return toolError(fmt.Sprintf("could not encode tool result: %v", err))
	}
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: string(encoded)}}}
}

func toolError(message string) *mcp.CallToolResult {
	return &mcp.CallToolResult{
		Content: []mcp.Content{&mcp.TextContent{Text: message}},
		IsError: true,
	}
}
