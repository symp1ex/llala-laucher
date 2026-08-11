package research

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"llala-launcher/mcp/internal/fetch"
	"llala-launcher/mcp/internal/search"
)

func TestResearchWorkflowClassifiesCandidatesAndPreservesDiagnostics(t *testing.T) {
	now := time.Date(2026, 8, 11, 12, 0, 0, 0, time.UTC)
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/search":
			if r.URL.Query().Get("q") == "broken query" {
				http.Error(w, "down", http.StatusBadGateway)
				return
			}
			localhostURL := strings.Replace(server.URL, "127.0.0.1", "localhost", 1)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"results": []map[string]any{
					{"title": "Shared fresh headline", "url": server.URL + "/fresh-a", "content": "lead A"},
					{"title": "Shared fresh headline", "url": localhostURL + "/fresh-b", "content": "lead B"},
					{"title": "Old story", "url": server.URL + "/old", "content": "old"},
					{"title": "Undated story", "url": server.URL + "/undated", "content": "unknown"},
					{"title": "Time-only story", "url": server.URL + "/time-only", "content": "low confidence"},
					{"title": "Conflicting dates", "url": server.URL + "/conflict", "content": "conflict"},
					{"title": "Broken page", "url": server.URL + "/failure", "content": "failure"},
				},
				"unresponsive_engines": [][]string{{"google", "timeout"}},
			})
		case "/failure":
			http.Error(w, "broken", http.StatusBadGateway)
		case "/fresh-a", "/fresh-b":
			writeArticle(w, "2026-08-11T06:00:00Z")
		case "/old":
			writeArticle(w, "2026-08-09T06:00:00Z")
		case "/undated":
			w.Header().Set("Content-Type", "text/html")
			_, _ = io.WriteString(w, `<html><body><main><p>No machine-readable date.</p></main></body></html>`)
		case "/conflict":
			w.Header().Set("Content-Type", "text/html")
			_, _ = io.WriteString(w, `<html><head><script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-08-11T06:00:00Z"}</script><meta property="article:published_time" content="2026-08-11T07:00:00Z"></head><body><main><p>Conflicting metadata.</p></main></body></html>`)
		case "/time-only":
			w.Header().Set("Content-Type", "text/html")
			_, _ = io.WriteString(w, `<html><body><main><time datetime="2026-08-11T06:00:00Z">Event time</time><p>Ambiguous time.</p></main></body></html>`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	searcher, err := search.New(server.URL, server.Client(), 8)
	if err != nil {
		t.Fatal(err)
	}
	runner := New(searcher, fetch.NewForTest(server.Client(), nil, true))
	runner.now = func() time.Time { return now }
	result, err := runner.Research(context.Background(), Params{
		Queries: []string{"workflow", "broken query"}, FreshnessHours: 24,
		MaxStories: 10, MaxCandidates: 10, MinDistinctDomains: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Cutoff != "2026-08-10T12:00:00Z" || result.ApproximateRange != "day" ||
		result.CandidateCount != 7 || result.FetchedCount != 7 || len(result.Stories) != 1 {
		t.Fatalf("unexpected workflow summary: %+v", result)
	}
	story := result.Stories[0]
	if story.Title != "Shared fresh headline" || len(story.Sources) != 2 || story.DistinctDomains != 2 ||
		result.DistinctDomains != 2 || story.Sources[0].Excerpt == "" || story.Sources[0].DateConfidence != "high" {
		t.Fatalf("fresh multi-domain story was not preserved: %+v", story)
	}
	if len(result.QueryLog) != 2 || len(result.QueryLog[0].UnresponsiveEngines) != 1 ||
		result.QueryLog[0].UnresponsiveEngines[0].Engine != "google" || result.QueryLog[1].Error == "" {
		t.Fatalf("query diagnostics were lost: %+v", result.QueryLog)
	}
	reasons := make(map[string]bool)
	for _, rejected := range result.Rejected {
		reasons[rejected.Reason] = true
	}
	for _, want := range []string{"outside_freshness_window", "missing_verified_date", "insufficient_date_confidence", "date_conflict", "fetch_failed"} {
		if !reasons[want] {
			t.Fatalf("missing rejection reason %q: %+v", want, result.Rejected)
		}
	}
	if len(result.Errors) != 2 || result.Errors[0].Stage != "search" && result.Errors[1].Stage != "search" {
		t.Fatalf("partial failures were not recorded: %+v", result.Errors)
	}
}

func TestResearchHonorsCandidateAndStoryLimits(t *testing.T) {
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/search" {
			results := make([]map[string]any, 4)
			for index := range results {
				results[index] = map[string]any{
					"title": fmt.Sprintf("Story %d", index), "url": fmt.Sprintf("%s/article/%d", server.URL, index),
				}
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"results": results, "unresponsive_engines": []any{}})
			return
		}
		writeArticle(w, "2026-08-11T10:00:00Z")
	}))
	defer server.Close()
	searcher, _ := search.New(server.URL, server.Client(), 8)
	runner := New(searcher, fetch.NewForTest(server.Client(), nil, true))
	runner.now = func() time.Time { return time.Date(2026, 8, 11, 12, 0, 0, 0, time.UTC) }
	result, err := runner.Research(context.Background(), Params{
		Queries: []string{"limits"}, FreshnessHours: 24, MaxCandidates: 2, MaxStories: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.CandidateCount != 2 || result.FetchedCount != 2 || len(result.Stories) != 1 || !result.Truncated {
		t.Fatalf("limits were not enforced: %+v", result)
	}
	foundStoryLimit := false
	for _, rejected := range result.Rejected {
		foundStoryLimit = foundStoryLimit || rejected.Reason == "story_limit"
	}
	if !foundStoryLimit {
		t.Fatalf("story-limit rejection missing: %+v", result.Rejected)
	}
}

func TestResearchCancellationStopsFetches(t *testing.T) {
	fetchStarted := make(chan struct{})
	var once sync.Once
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/search" {
			_ = json.NewEncoder(w).Encode(map[string]any{"results": []map[string]any{{"title": "Slow", "url": server.URL + "/slow"}}})
			return
		}
		once.Do(func() { close(fetchStarted) })
		<-r.Context().Done()
	}))
	defer server.Close()
	searcher, _ := search.New(server.URL, server.Client(), 8)
	runner := New(searcher, fetch.NewForTest(server.Client(), nil, true))
	runner.now = func() time.Time { return time.Date(2026, 8, 11, 12, 0, 0, 0, time.UTC) }
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		<-fetchStarted
		cancel()
	}()
	_, err := runner.Research(ctx, Params{Queries: []string{"cancel"}, FreshnessHours: 24})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("expected context cancellation, got %v", err)
	}
}

func TestResearchValidatesInput(t *testing.T) {
	runner := New(nil, nil)
	for _, params := range []Params{
		{},
		{Queries: []string{""}, FreshnessHours: 1},
		{Queries: []string{"q"}, FreshnessHours: 0},
		{Queries: []string{"q"}, FreshnessHours: 169},
		{Queries: []string{"q"}, FreshnessHours: 1, MaxCandidates: 41},
		{Queries: []string{"q"}, FreshnessHours: 1, MinDistinctDomains: 4},
	} {
		if _, err := runner.Research(context.Background(), params); err == nil {
			t.Fatalf("expected validation error for %+v", params)
		}
	}
}

func writeArticle(w http.ResponseWriter, publishedAt string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = io.WriteString(w, `<html><head><script type="application/ld+json">{"@type":"NewsArticle","datePublished":"`+
		publishedAt+`"}</script></head><body><main><p>Verified article excerpt with source facts.</p></main></body></html>`)
}
