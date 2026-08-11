package research

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"llala-launcher/mcp/internal/fetch"
	"llala-launcher/mcp/internal/search"
	"llala-launcher/mcp/internal/textutil"
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
		if result.RejectedCounts[want] != 1 {
			t.Fatalf("missing complete rejection count %q: %+v", want, result.RejectedCounts)
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

func TestResearchDefaultsExplicitCandidateLimitAndModes(t *testing.T) {
	tests := []struct {
		params         Params
		wantStories    int
		wantCandidates int
		wantMode       string
	}{
		{Params{Queries: []string{"q"}, FreshnessHours: 24}, 5, 10, "standard"},
		{Params{Queries: []string{"q"}, FreshnessHours: 24, MaxStories: 3}, 3, 6, "standard"},
		{Params{Queries: []string{"q"}, FreshnessHours: 24, MaxStories: 10}, 10, 12, "standard"},
		{Params{Queries: []string{"q"}, FreshnessHours: 24, MaxStories: 3, MaxCandidates: 17, ResponseMode: "deep"}, 3, 17, "deep"},
	}
	for _, test := range tests {
		validated, err := validateParams(test.params)
		if err != nil {
			t.Fatal(err)
		}
		if validated.MaxStories != test.wantStories || validated.MaxCandidates != test.wantCandidates || validated.ResponseMode != test.wantMode {
			t.Fatalf("unexpected defaults for %+v: %+v", test.params, validated)
		}
	}
	if _, err := validateParams(Params{Queries: []string{"q"}, FreshnessHours: 24, ResponseMode: "compact"}); err == nil {
		t.Fatal("unknown response mode was accepted")
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"results": []any{}})
	}))
	defer server.Close()
	searcher, _ := search.New(server.URL, server.Client(), 8)
	runner := New(searcher, fetch.NewForTest(server.Client(), nil, true))
	result, err := runner.Research(context.Background(), Params{Queries: []string{"q"}, FreshnessHours: 24, MaxStories: 3})
	if err != nil || result.AppliedLimits.MaxCandidates != 6 || result.ResponseMode != "standard" {
		t.Fatalf("applied defaults were not reported: %+v, %v", result, err)
	}
}

func TestRoundRobinCandidatesIsFairDeduplicatedAndStable(t *testing.T) {
	perQuery := [][]search.Result{
		{
			{Title: "q1-0", URL: "https://example.com/shared?utm_source=one"},
			{Title: "q1-1", URL: "https://example.com/q1/1"},
			{Title: "q1-2", URL: "https://example.com/q1/2"},
		},
		{
			{Title: "duplicate", URL: "https://EXAMPLE.com/shared"},
			{Title: "q2-1", URL: "https://example.com/q2/1"},
			{Title: "q2-2", URL: "https://example.com/q2/2"},
		},
		{{Title: "q3-0", URL: "https://example.com/q3/0"}},
	}
	want := []string{"q1-0", "q3-0", "q1-1", "q2-1"}
	first, truncated := roundRobinCandidates(perQuery, len(want))
	second, secondTruncated := roundRobinCandidates(perQuery, len(want))
	if !truncated || !secondTruncated || len(first) != len(want) || !reflect.DeepEqual(first, second) {
		t.Fatalf("round-robin was not deterministic: first=%+v second=%+v", first, second)
	}
	for index, item := range first {
		if item.result.Title != want[index] {
			t.Fatalf("round-robin order[%d]=%q, want %q: %+v", index, item.result.Title, want[index], first)
		}
	}
}

func TestResearchRoundRobinGivesEachQueryAFirstCandidate(t *testing.T) {
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/search" {
			query := r.URL.Query().Get("q")
			count := 3
			if query == "third" {
				count = 1
			}
			results := make([]map[string]any, count)
			for index := range results {
				results[index] = map[string]any{
					"title": fmt.Sprintf("%s-%d", query, index),
					"url":   fmt.Sprintf("%s/%s/%d", server.URL, query, index),
				}
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"results": results})
			return
		}
		writeArticle(w, "2026-08-11T08:00:00Z")
	}))
	defer server.Close()
	searcher, _ := search.New(server.URL, server.Client(), 8)
	runner := New(searcher, fetch.NewForTest(server.Client(), nil, true))
	runner.now = func() time.Time { return time.Date(2026, 8, 11, 12, 0, 0, 0, time.UTC) }
	result, err := runner.Research(context.Background(), Params{
		Queries: []string{"first", "second", "third"}, FreshnessHours: 24,
		MaxCandidates: 3, MaxStories: 3,
	})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"first-0", "second-0", "third-0"}
	if len(result.QueryLog) != 3 || len(result.Stories) != len(want) || !result.Truncated {
		t.Fatalf("unexpected round-robin response: %+v", result)
	}
	for index, title := range want {
		if result.Stories[index].Title != title {
			t.Fatalf("story order[%d]=%q, want %q", index, result.Stories[index].Title, title)
		}
	}
}

func TestResearchResponseModesLimitExcerptsAuthorsAndDateEvidence(t *testing.T) {
	server := newEvidenceResearchServer(t, 5, false)
	defer server.Close()
	searcher, _ := search.New(server.URL, server.Client(), 8)
	runner := New(searcher, fetch.NewForTest(server.Client(), nil, true))
	runner.now = func() time.Time { return time.Date(2026, 8, 11, 12, 0, 0, 0, time.UTC) }

	run := func(mode string) Response {
		result, err := runner.Research(context.Background(), Params{
			Queries: []string{"evidence"}, FreshnessHours: 24, MaxCandidates: 5, MaxStories: 5, ResponseMode: mode,
		})
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	standard := run("")
	deep := run("deep")
	for _, test := range []struct {
		name         string
		response     Response
		wantExcerpts int
		wantAuthors  int
		wantEvidence int
	}{
		{"standard", standard, 2, 4, 4},
		{"deep", deep, 3, 10, 10},
	} {
		if test.response.ResponseMode != test.name || len(test.response.Stories) != 1 {
			t.Fatalf("unexpected %s response: %+v", test.name, test.response)
		}
		story := test.response.Stories[0]
		if story.SourceCount != 5 || len(story.Sources) != 5 || story.ExcerptSourceCount != test.wantExcerpts || !story.ExcerptsTruncated {
			t.Fatalf("unexpected %s story counts: %+v", test.name, story)
		}
		for index, source := range story.Sources {
			if source.URL == "" || source.SourceDomain == "" || source.PublishedAt == "" || source.DateConfidence != "high" {
				t.Fatalf("source metadata %d was lost in %s: %+v", index, test.name, source)
			}
			if (index < test.wantExcerpts) != (source.Excerpt != "") {
				t.Fatalf("unexpected excerpt at source %d in %s: %+v", index, test.name, source)
			}
			if textutil.RuneCount(source.Excerpt) > test.response.AppliedLimits.ExcerptCharacters {
				t.Fatalf("excerpt character limit exceeded in %s: %d", test.name, textutil.RuneCount(source.Excerpt))
			}
			if source.AuthorsCount != 12 || len(source.Authors) != test.wantAuthors || !source.AuthorsTruncated ||
				source.DateEvidenceCount != 12 || len(source.DateEvidence) != test.wantEvidence || !source.DateEvidenceTruncated {
				t.Fatalf("unexpected evidence limits in %s: %+v", test.name, source)
			}
		}
	}
}

func TestResearchRejectedAggregationAndExampleLimits(t *testing.T) {
	server := newEvidenceResearchServer(t, 20, true)
	defer server.Close()
	searcher, _ := search.New(server.URL, server.Client(), 8)
	runner := New(searcher, fetch.NewForTest(server.Client(), nil, true))
	runner.now = func() time.Time { return time.Date(2026, 8, 11, 12, 0, 0, 0, time.UTC) }
	for _, test := range []struct {
		mode string
		want int
	}{{"standard", 8}, {"deep", 16}} {
		result, err := runner.Research(context.Background(), Params{
			Queries: []string{"old"}, FreshnessHours: 24, MaxCandidates: 20, ResponseMode: test.mode,
		})
		if err != nil {
			t.Fatal(err)
		}
		if result.RejectedCount != 20 || result.RejectedCounts["outside_freshness_window"] != 20 ||
			len(result.Rejected) != test.want || !result.RejectedTruncated || !result.Truncated {
			t.Fatalf("unexpected %s rejection aggregation: %+v", test.mode, result)
		}
	}
}

func TestResearchHardOutputBudgetStandardAndDeepIsDeterministic(t *testing.T) {
	server := newLargeBudgetResearchServer(t)
	defer server.Close()
	searcher, _ := search.New(server.URL, server.Client(), 20)
	runner := New(searcher, fetch.NewForTest(server.Client(), nil, true))
	runner.now = func() time.Time { return time.Date(2026, 8, 11, 12, 0, 0, 0, time.UTC) }

	run := func(mode string) Response {
		result, err := runner.Research(context.Background(), Params{
			Queries: []string{"first", "second"}, FreshnessHours: 24,
			MaxStories: 5, MaxCandidates: 40, ResponseMode: mode,
		})
		if err != nil {
			t.Fatal(err)
		}
		encoded, err := json.Marshal(result)
		if err != nil || !json.Valid(encoded) || len(encoded) > hardOutputLimitBytes ||
			result.OutputBudget.ReturnedBytes != len(encoded) || !result.Truncated || !result.OutputBudget.Truncated {
			t.Fatalf("invalid %s budget: bytes=%d budget=%+v truncated=%v err=%v", mode, len(encoded), result.OutputBudget, result.Truncated, err)
		}
		t.Logf("%s research response: %d bytes", mode, len(encoded))
		if len(result.Stories) != 1 || len(result.Stories[0].Sources) != 40 {
			t.Fatalf("mandatory sources were removed in %s: %+v", mode, result.Stories)
		}
		for _, source := range result.Stories[0].Sources {
			if source.URL == "" || source.SourceDomain == "" || source.PublishedAt == "" || source.DateConfidence != "high" {
				t.Fatalf("mandatory source metadata was removed in %s: %+v", mode, source)
			}
		}
		return result
	}
	standard := run("standard")
	deep := run("deep")
	standardJSON, _ := json.Marshal(standard)
	deepJSON, _ := json.Marshal(deep)
	if len(standardJSON) > len(deepJSON) || standard.Stories[0].ExcerptSourceCount > deep.Stories[0].ExcerptSourceCount ||
		totalEvidence(standard.Stories[0]) > totalEvidence(deep.Stories[0]) {
		t.Fatalf("standard returned more optional data than deep: standard=%d deep=%d", len(standardJSON), len(deepJSON))
	}
	again := run("standard")
	if !reflect.DeepEqual(standard.Stories, again.Stories) || !reflect.DeepEqual(standard.Rejected, again.Rejected) ||
		!reflect.DeepEqual(standard.RejectedCounts, again.RejectedCounts) {
		t.Fatal("identical research input produced a different deterministic subset or order")
	}
}

func TestResearchBudgetErrorsWhenMandatoryDataCannotFit(t *testing.T) {
	response := Response{
		QueryLog: []QueryLog{{Query: "q", Error: strings.Repeat("mandatory diagnostic ", 2_000)}},
		Errors:   []ResearchError{{Stage: "search", Error: strings.Repeat("mandatory diagnostic ", 2_000)}},
		Stories:  make([]Story, 0), Rejected: make([]RejectedCandidate, 0), RejectedCounts: make(map[string]int),
		OutputBudget: OutputBudget{Mode: "standard", LimitBytes: hardOutputLimitBytes, TargetBytes: standardTargetBytes},
	}
	if err := applyOutputBudget(&response, standardTargetBytes); err == nil || !strings.Contains(err.Error(), "hard limit") {
		t.Fatalf("oversized mandatory response did not fail explicitly: %v", err)
	}
}

func totalEvidence(story Story) int {
	total := 0
	for _, source := range story.Sources {
		total += len(source.DateEvidence)
	}
	return total
}

func newEvidenceResearchServer(t *testing.T, count int, old bool) *httptest.Server {
	t.Helper()
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/search" {
			results := make([]map[string]any, count)
			for index := range results {
				results[index] = map[string]any{
					"title": "Shared evidence story", "url": fmt.Sprintf("%s/article/%d", server.URL, index),
				}
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"results": results})
			return
		}
		publishedAt := "2026-08-11T08:00:00Z"
		if old {
			publishedAt = "2026-08-01T08:00:00Z"
		}
		writeEvidenceArticle(w, publishedAt)
	}))
	return server
}

func newLargeBudgetResearchServer(t *testing.T) *httptest.Server {
	t.Helper()
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/search" {
			prefix := r.URL.Query().Get("q")
			results := make([]map[string]any, 20)
			for index := range results {
				results[index] = map[string]any{
					"title": "One large clustered story",
					"url":   fmt.Sprintf("%s/article/%s/%02d", server.URL, prefix, index),
				}
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"results": results})
			return
		}
		writeEvidenceArticle(w, "2026-08-11T08:00:00Z")
	}))
	return server
}

func writeEvidenceArticle(w http.ResponseWriter, publishedAt string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	var metadata strings.Builder
	for index := 0; index < 12; index++ {
		fmt.Fprintf(&metadata, `<meta name="author" content="Author %02d"><meta property="article:published_time" content="%s">`, index, publishedAt)
	}
	_, _ = io.WriteString(w, "<html><head><meta property=\"og:site_name\" content=\"Example Publisher\">"+
		metadata.String()+"</head><body><main><p>"+strings.Repeat("Verified evidence sentence with context. ", 100)+"</p></main></body></html>")
}

func writeArticle(w http.ResponseWriter, publishedAt string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = io.WriteString(w, `<html><head><script type="application/ld+json">{"@type":"NewsArticle","datePublished":"`+
		publishedAt+`"}</script></head><body><main><p>Verified article excerpt with source facts.</p></main></body></html>`)
}
