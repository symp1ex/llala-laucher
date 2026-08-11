package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"llala-launcher/mcp/internal/fetch"
	"llala-launcher/mcp/internal/search"
	"llala-launcher/mcp/internal/textutil"
)

type fakeSearch struct {
	params search.Params
	err    error
}

func (f *fakeSearch) Search(_ context.Context, params search.Params) (search.Response, error) {
	f.params = params
	if f.err != nil {
		return search.Response{}, f.err
	}
	return search.Response{Query: params.Query, Results: []search.Result{{Title: "Result", URL: "https://example.com"}}}, nil
}

type fakeFetch struct {
	url   string
	query string
	err   error
}

func (f *fakeFetch) Fetch(_ context.Context, value string, query ...string) (fetch.Result, error) {
	f.url = value
	if len(query) > 0 {
		f.query = query[0]
	}
	if f.err != nil {
		return fetch.Result{}, f.err
	}
	return fetch.Result{SourceURL: value, FinalURL: value, ContentType: "text/plain", Content: "page"}, nil
}

func connect(t *testing.T, searcher Searcher, fetcher Fetcher) (*mcp.ClientSession, func()) {
	t.Helper()
	ctx := context.Background()
	clientTransport, serverTransport := mcp.NewInMemoryTransports()
	serverSession, err := New(searcher, fetcher).Connect(ctx, serverTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	client := mcp.NewClient(&mcp.Implementation{Name: "test-client", Version: "1"}, nil)
	clientSession, err := client.Connect(ctx, clientTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	return clientSession, func() {
		_ = clientSession.Close()
		_ = serverSession.Wait()
	}
}

func TestInitializeListPingAndCalls(t *testing.T) {
	searcher := &fakeSearch{}
	fetcher := &fakeFetch{}
	session, closeSession := connect(t, searcher, fetcher)
	defer closeSession()
	if session.InitializeResult() == nil || session.InitializeResult().ServerInfo.Name != "llala-web-mcp" {
		t.Fatalf("unexpected initialize result: %+v", session.InitializeResult())
	}
	if err := session.Ping(context.Background(), nil); err != nil {
		t.Fatalf("ping failed: %v", err)
	}
	list, err := session.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(list.Tools) != 3 {
		t.Fatalf("unexpected tools: %+v", list.Tools)
	}
	var searchDescription, fetchDescription, researchDescription string
	for _, tool := range list.Tools {
		switch tool.Name {
		case "web_search":
			searchDescription = strings.ToLower(tool.Description)
		case "web_fetch":
			fetchDescription = strings.ToLower(tool.Description)
		case "web_news_research":
			researchDescription = strings.ToLower(tool.Description)
		}
	}
	for _, required := range []string{"snippets are unverified", "dateverified=false", "web_news_research", "not automatically independent", "unresponsiveengines"} {
		if !strings.Contains(searchDescription, required) {
			t.Fatalf("web_search description lacks %q: %q", required, searchDescription)
		}
	}
	for _, required := range []string{"dateevidence", "dateconfidence", "unverified", "external/untrusted", "do not prove"} {
		if !strings.Contains(fetchDescription, required) {
			t.Fatalf("web_fetch description lacks %q: %q", required, fetchDescription)
		}
	}
	for _, required := range []string{"last n hours", "bounded", "date conflicts", "distinctdomains", "not a claim", "not a generated summary"} {
		if !strings.Contains(researchDescription, required) {
			t.Fatalf("web_news_research description lacks %q: %q", required, researchDescription)
		}
	}
	searchResult, err := session.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "web_search", Arguments: map[string]any{
			"query": "current news", "max_results": 3, "category": "news",
			"published_after": "2026-08-10T00:00:00Z", "require_published_date": true,
		},
	})
	if err != nil || searchResult.IsError {
		t.Fatalf("web_search failed: %+v %v", searchResult, err)
	}
	if searcher.params.Query != "current news" || searcher.params.MaxResults != 3 || searcher.params.Category != "news" ||
		searcher.params.PublishedAfter != "2026-08-10T00:00:00Z" || !searcher.params.RequirePublishedDate {
		t.Fatalf("unexpected search params: %+v", searcher.params)
	}
	fetchResult, err := session.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "web_fetch", Arguments: map[string]any{"url": "https://example.com/article", "query": "specific fact"},
	})
	if err != nil || fetchResult.IsError || fetcher.url != "https://example.com/article" || fetcher.query != "specific fact" {
		t.Fatalf("web_fetch failed: %+v %v", fetchResult, err)
	}
	text := fetchResult.Content[0].(*mcp.TextContent).Text
	var decoded fetch.Result
	if err := json.Unmarshal([]byte(text), &decoded); err != nil || decoded.Content != "page" {
		t.Fatalf("invalid fetch JSON: %q, %v", text, err)
	}
	researchResult, err := session.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "web_news_research", Arguments: map[string]any{
			"queries": []any{"current news"}, "freshness_hours": 24, "max_candidates": 3,
		},
	})
	if err != nil || researchResult.IsError || !strings.Contains(researchResult.Content[0].(*mcp.TextContent).Text, "missing_verified_date") ||
		searcher.params.Category != "news" || searcher.params.TimeRange != "day" {
		t.Fatalf("web_news_research failed: %+v %v params=%+v", researchResult, err, searcher.params)
	}
}

func TestFetchWithoutQueryRemainsBackwardCompatible(t *testing.T) {
	fetcher := &fakeFetch{}
	session, closeSession := connect(t, &fakeSearch{}, fetcher)
	defer closeSession()
	result, err := session.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "web_fetch", Arguments: map[string]any{"url": "https://example.com/article"},
	})
	if err != nil || result.IsError || fetcher.query != "" {
		t.Fatalf("legacy web_fetch(url) failed: %+v %v", result, err)
	}
}

func TestToolFailuresDoNotTerminateSession(t *testing.T) {
	searcher := &fakeSearch{err: errors.New("offline")}
	session, closeSession := connect(t, searcher, &fakeFetch{})
	defer closeSession()
	result, err := session.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "web_search", Arguments: map[string]any{"query": "q"},
	})
	if err != nil || !result.IsError || !strings.Contains(result.Content[0].(*mcp.TextContent).Text, "temporarily failed") {
		t.Fatalf("unexpected tool error: %+v %v", result, err)
	}
	if err := session.Ping(context.Background(), nil); err != nil {
		t.Fatalf("session died after tool error: %v", err)
	}
}

func TestStrictInputSchemaRejectsInvalidCalls(t *testing.T) {
	session, closeSession := connect(t, &fakeSearch{}, &fakeFetch{})
	defer closeSession()
	for _, arguments := range []map[string]any{
		{},
		{"query": "q", "max_results": 21},
		{"query": "q", "category": "images"},
		{"query": "q", "unknown": true},
	} {
		result, err := session.CallTool(context.Background(), &mcp.CallToolParams{Name: "web_search", Arguments: arguments})
		if err == nil && (result == nil || !result.IsError) {
			t.Fatalf("invalid arguments accepted: %v => %+v, %v", arguments, result, err)
		}
	}
	for _, arguments := range []map[string]any{
		{},
		{"queries": []any{}, "freshness_hours": 24},
		{"queries": []any{"q"}, "freshness_hours": 0},
		{"queries": []any{"q"}, "freshness_hours": 169},
		{"queries": []any{"q"}, "freshness_hours": 24, "max_candidates": 41},
		{"queries": []any{"q"}, "freshness_hours": 24, "unknown": true},
	} {
		result, err := session.CallTool(context.Background(), &mcp.CallToolParams{Name: "web_news_research", Arguments: arguments})
		if err == nil && (result == nil || !result.IsError) {
			t.Fatalf("invalid research arguments accepted: %v => %+v, %v", arguments, result, err)
		}
	}
	for _, arguments := range []map[string]any{
		{},
		{"url": "https://example.com", "query": ""},
		{"url": "https://example.com", "query": 42},
		{"url": "https://example.com", "unknown": true},
	} {
		result, err := session.CallTool(context.Background(), &mcp.CallToolParams{Name: "web_fetch", Arguments: arguments})
		if err == nil && (result == nil || !result.IsError) {
			t.Fatalf("invalid fetch arguments accepted: %v => %+v, %v", arguments, result, err)
		}
	}
	if err := session.Ping(context.Background(), nil); err != nil {
		t.Fatalf("session died after invalid calls: %v", err)
	}
}

func TestMCPIntegrationWithRealSearchAndFetchClients(t *testing.T) {
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/search" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"results":[{"title":"Article","url":"https://example.com/article","content":"lead","engines":["mock"]}]}`)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = io.WriteString(w, `<html><head><title>Article</title></head><body><main><h1>Source</h1><p>Integrated content.</p></main></body></html>`)
	}))
	defer mock.Close()
	searcher, err := search.New(mock.URL, mock.Client(), 8)
	if err != nil {
		t.Fatal(err)
	}
	fetcher := fetch.NewForTest(mock.Client(), nil, true)
	session, closeSession := connect(t, searcher, fetcher)
	defer closeSession()
	searched, err := session.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "web_search", Arguments: map[string]any{"query": "integration"},
	})
	if err != nil || searched.IsError || !strings.Contains(searched.Content[0].(*mcp.TextContent).Text, "Article") {
		t.Fatalf("integrated search failed: %+v %v", searched, err)
	}
	fetched, err := session.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "web_fetch", Arguments: map[string]any{"url": mock.URL + "/article"},
	})
	if err != nil || fetched.IsError || !strings.Contains(fetched.Content[0].(*mcp.TextContent).Text, "Integrated content") {
		t.Fatalf("integrated fetch failed: %+v %v", fetched, err)
	}
	if err := session.Ping(context.Background(), nil); err != nil {
		t.Fatalf("integrated session ping failed: %v", err)
	}
}

func TestTypicalResearchWorkflowContextBudget(t *testing.T) {
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/search" {
			results := make([]map[string]any, 8)
			for index := range results {
				results[index] = map[string]any{
					"title":   fmt.Sprintf("Authoritative source %d", index+1),
					"url":     fmt.Sprintf("http://research.invalid/article/%d", index+1),
					"content": "Informative current snippet with enough context to assess relevance and authority.",
					"engines": []string{"mock"},
				}
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"results": results})
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = io.WriteString(w, "<html><body><main><h1>Technical report</h1><p>"+
			strings.Repeat("Background evidence and methodological detail. ", 700)+
			"</p><h2>CUDA change in version 1.8</h2><p>Version 1.8 enabled CUDA graph execution after validation.</p><p>Independent measurements and caveats follow this finding.</p><h2>Appendix</h2><p>"+
			strings.Repeat("Additional source material and tabulated observations. ", 700)+"</p></main></body></html>")
	}))
	defer mock.Close()
	searcher, _ := search.New(mock.URL, mock.Client(), 8)
	fetcher := fetch.NewForTest(mock.Client(), nil, true)
	session, closeSession := connect(t, searcher, fetcher)
	defer closeSession()

	var combined strings.Builder
	call := func(name string, arguments map[string]any) {
		t.Helper()
		result, err := session.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: arguments})
		if err != nil || result.IsError {
			t.Fatalf("%s failed: %+v %v", name, result, err)
		}
		combined.WriteString(result.Content[0].(*mcp.TextContent).Text)
		combined.WriteByte('\n')
	}
	call("web_search", map[string]any{"query": "CUDA release current facts"})
	call("web_search", map[string]any{"query": "CUDA 1.8 independent verification"})
	for index := 0; index < 4; index++ {
		arguments := map[string]any{"url": mock.URL + fmt.Sprintf("/article/%d", index)}
		if index >= 2 {
			arguments["query"] = "What changed in version 1.8 for CUDA?"
		}
		call("web_fetch", arguments)
	}
	characters := textutil.RuneCount(combined.String())
	tokens := textutil.EstimateTokens(combined.String())
	t.Logf("2 searches + 4 fetches: %d bytes, %d characters, approximately %d tokens", combined.Len(), characters, tokens)
	if tokens < 10_000 || tokens > 50_000 {
		t.Fatalf("workflow budget outside quality/regression envelope: bytes=%d chars=%d tokens=%d", combined.Len(), characters, tokens)
	}
}
