package server

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"llala-launcher/mcp/internal/fetch"
	"llala-launcher/mcp/internal/search"
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
	url string
	err error
}

func (f *fakeFetch) Fetch(_ context.Context, value string) (fetch.Result, error) {
	f.url = value
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
	if len(list.Tools) != 2 || list.Tools[0].Name != "web_fetch" && list.Tools[1].Name != "web_fetch" {
		t.Fatalf("unexpected tools: %+v", list.Tools)
	}
	searchResult, err := session.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "web_search", Arguments: map[string]any{"query": "current news", "max_results": 3, "category": "news"},
	})
	if err != nil || searchResult.IsError {
		t.Fatalf("web_search failed: %+v %v", searchResult, err)
	}
	if searcher.params.Query != "current news" || searcher.params.MaxResults != 3 || searcher.params.Category != "news" {
		t.Fatalf("unexpected search params: %+v", searcher.params)
	}
	fetchResult, err := session.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "web_fetch", Arguments: map[string]any{"url": "https://example.com/article"},
	})
	if err != nil || fetchResult.IsError || fetcher.url != "https://example.com/article" {
		t.Fatalf("web_fetch failed: %+v %v", fetchResult, err)
	}
	text := fetchResult.Content[0].(*mcp.TextContent).Text
	var decoded fetch.Result
	if err := json.Unmarshal([]byte(text), &decoded); err != nil || decoded.Content != "page" {
		t.Fatalf("invalid fetch JSON: %q, %v", text, err)
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
