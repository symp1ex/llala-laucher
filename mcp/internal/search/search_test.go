package search

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
	"unicode/utf8"
)

func TestSearchRequestParametersEncodingAndNormalization(t *testing.T) {
	var received url.Values
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received = r.URL.Query()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"results":[{"title":"  A title ","url":"https://example.com/a b","content":" some\n snippet ","engines":["bing","yandex"],"score":1.5,"publishedDate":"2026-08-10"}]}`))
	}))
	defer server.Close()
	client, err := New(server.URL, server.Client(), 8)
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.Search(context.Background(), Params{
		Query: "cats & dogs", MaxResults: 3, Language: "ru-RU", Page: 2, TimeRange: "day", Category: "news",
	})
	if err != nil {
		t.Fatal(err)
	}
	if received.Get("q") != "cats & dogs" || received.Get("format") != "json" ||
		received.Get("pageno") != "2" || received.Get("language") != "ru-RU" ||
		received.Get("time_range") != "day" || received.Get("categories") != "news" {
		t.Fatalf("unexpected query values: %v", received)
	}
	if len(result.Results) != 1 || result.Results[0].Title != "A title" ||
		result.Results[0].Snippet != "some snippet" || len(result.Results[0].Engines) != 2 ||
		result.Results[0].Score == nil || result.Results[0].PublishedDate == "" ||
		result.Results[0].PublishedAt != "2026-08-10T00:00:00Z" || result.Results[0].DateVerified ||
		result.Results[0].PublishedAtSource != "searxng_index" || result.Results[0].SourceDomain != "example.com" {
		t.Fatalf("unexpected normalized result: %+v", result)
	}
}

func TestSearchResultCountAndOutputSizeLimits(t *testing.T) {
	results := make([]map[string]any, 5)
	for i := range results {
		results[i] = map[string]any{"title": "result", "url": fmt.Sprintf("https://example.com/%d", i), "content": strings.Repeat("x", 100)}
	}
	payload, _ := json.Marshal(map[string]any{"results": results})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write(payload) }))
	defer server.Close()
	client, _ := New(server.URL, server.Client(), 8)
	limited, err := client.Search(context.Background(), Params{Query: "q", MaxResults: 2})
	if err != nil || len(limited.Results) != 2 || !limited.Truncated {
		t.Fatalf("count limit failed: %+v, %v", limited, err)
	}
	client.SetMaxOutputSize(450)
	sized, err := client.Search(context.Background(), Params{Query: "q", MaxResults: 5})
	if err != nil || len(sized.Results) >= 5 || !sized.Truncated {
		t.Fatalf("size limit failed: %+v, %v", sized, err)
	}
}

func TestSearchCompactsSnippetsAndDropsRawFields(t *testing.T) {
	payload := `{"results":[{"title":"Useful &amp; current","url":"https://example.com/a","content":"<b>Lead</b> ` + strings.Repeat("данные ", 200) + `","engines":["z","z","a"],"score":2,"publishedDate":"2026-08-11","category":"news","positions":[1],"thumbnail":"huge"}]}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(payload)) }))
	defer server.Close()
	client, _ := New(server.URL, server.Client(), 8)
	result, err := client.Search(context.Background(), Params{Query: "q"})
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Results) != 1 || result.Results[0].Rank != 1 || !strings.HasPrefix(result.Results[0].Snippet, "Lead данные") {
		t.Fatalf("unexpected compact result: %+v", result)
	}
	if len([]rune(result.Results[0].Snippet)) > maxSnippetChars || !utf8.ValidString(result.Results[0].Snippet) {
		t.Fatalf("snippet limit/UTF-8 failure: %q", result.Results[0].Snippet)
	}
	encoded, _ := json.Marshal(result)
	for _, forbidden := range []string{"positions", "thumbnail", "category", "<b>"} {
		if strings.Contains(string(encoded), forbidden) {
			t.Fatalf("raw field/markup %q leaked: %s", forbidden, encoded)
		}
	}
	if result.ResultCount != 1 || result.StructuredResultCount != 1 || result.ReturnedCharacters == 0 || result.ApproximateTokens == 0 {
		t.Fatalf("missing budget metadata: %+v", result)
	}
}

func TestSearchDeduplicatesURLsButClustersMatchingTitles(t *testing.T) {
	results := []map[string]any{
		{"title": "Canonical long article title", "url": "https://Example.com/story/?utm_source=x&b=2&a=1", "content": "one"},
		{"title": "Tracking duplicate title", "url": "https://example.com/story?a=1&b=2&fbclid=abc", "content": "two"},
		{"title": "Canonical long article title", "url": "https://aggregator.example/copy", "content": "copy"},
		{"title": "Independent analysis", "url": "https://independent.example/story", "content": "three"},
	}
	payload, _ := json.Marshal(map[string]any{"results": results})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write(payload) }))
	defer server.Close()
	client, _ := New(server.URL, server.Client(), 8)
	result, err := client.Search(context.Background(), Params{Query: "q"})
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Results) != 3 || result.Results[0].Rank != 1 ||
		result.Results[0].URL != "https://example.com/story?a=1&b=2" ||
		result.Results[1].URL != "https://aggregator.example/copy" ||
		result.Results[2].URL != "https://independent.example/story" {
		t.Fatalf("unexpected deduplication: %+v", result.Results)
	}
	if result.Results[0].TitleClusterID == "" || result.Results[0].TitleClusterID != result.Results[1].TitleClusterID ||
		result.Results[0].TitleClusterID == result.Results[2].TitleClusterID {
		t.Fatalf("unexpected title clusters: %+v", result.Results)
	}
}

func TestSearchPublishedBoundsAndMissingDates(t *testing.T) {
	payload := `{"results":[
		{"title":"old","url":"https://old.example/story","publishedDate":"2026-08-10T09:59:59Z"},
		{"title":"inside","url":"https://inside.example/story","publishedDate":"2026-08-10T12:00:00+02:00"},
		{"title":"boundary","url":"https://boundary.example/story","publishedDate":"2026-08-10T11:00:00Z"},
		{"title":"missing","url":"https://missing.example/story"},
		{"title":"invalid","url":"https://invalid.example/story","publishedDate":"yesterday"}
	]}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(payload)) }))
	defer server.Close()
	client, _ := New(server.URL, server.Client(), 8)

	result, err := client.Search(context.Background(), Params{
		Query: "q", PublishedAfter: "2026-08-10T09:59:59Z", PublishedBefore: "2026-08-10T11:00:00Z",
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Results) != 3 || result.Results[0].Title != "inside" || result.Results[1].Title != "missing" ||
		result.Results[2].Title != "invalid" {
		t.Fatalf("unexpected strict-bound filtering: %+v", result.Results)
	}
	if result.Freshness.PublishedAfter != "2026-08-10T09:59:59Z" || result.Freshness.PublishedBefore != "2026-08-10T11:00:00Z" {
		t.Fatalf("missing applied freshness: %+v", result.Freshness)
	}

	required, err := client.Search(context.Background(), Params{
		Query: "q", PublishedAfter: "2026-08-10T09:59:59Z", PublishedBefore: "2026-08-10T11:00:00Z",
		RequirePublishedDate: true,
	})
	if err != nil || len(required.Results) != 1 || required.Results[0].Title != "inside" {
		t.Fatalf("require_published_date failed: %+v, %v", required, err)
	}
}

func TestSearchRejectsInvalidDateBounds(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"results":[]}`))
	}))
	defer server.Close()
	client, _ := New(server.URL, server.Client(), 8)
	for _, params := range []Params{
		{Query: "q", PublishedAfter: "2026-08-10"},
		{Query: "q", PublishedAfter: "2026-08-11T00:00:00Z", PublishedBefore: "2026-08-10T00:00:00Z"},
		{Query: "q", PublishedAfter: "2026-08-10T00:00:00Z", PublishedBefore: "2026-08-10T00:00:00Z"},
	} {
		if _, err := client.Search(context.Background(), params); err == nil {
			t.Fatalf("expected invalid bounds error for %+v", params)
		}
	}
}

func TestSearchEmptyAndUnresponsiveEnginesAreStructured(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"results":[],"unresponsive_engines":[["google","timeout"],["bing","CAPTCHA"]]}`))
	}))
	defer server.Close()
	client, _ := New(server.URL, server.Client(), 8)
	result, err := client.Search(context.Background(), Params{Query: "q"})
	if err != nil {
		t.Fatal(err)
	}
	if !result.Empty || result.RawResultCount != 0 || result.ResultCount != 0 || result.StructuredResultCount != 0 || result.Results == nil ||
		len(result.UnresponsiveEngines) != 2 || result.UnresponsiveEngines[0].Engine != "google" ||
		result.UnresponsiveEngines[0].Error != "timeout" || result.RetrievedAt == "" || result.ElapsedMS < 0 {
		t.Fatalf("unexpected empty diagnostics: %+v", result)
	}
}

func TestSearchHardOutputLimitKeepsValidJSON(t *testing.T) {
	results := make([]map[string]any, 20)
	for index := range results {
		results[index] = map[string]any{
			"title": fmt.Sprintf("Result %d", index), "url": fmt.Sprintf("https://example.com/%d", index),
			"content": strings.Repeat("long snippet ", 200),
		}
	}
	payload, _ := json.Marshal(map[string]any{"results": results})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write(payload) }))
	defer server.Close()
	client, _ := New(server.URL, server.Client(), 8)
	client.SetMaxOutputSize(2_000)
	result, err := client.Search(context.Background(), Params{Query: "q", MaxResults: 20})
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(result)
	if err != nil || len(encoded) > 2_000 || !result.Truncated {
		t.Fatalf("hard limit failure: bytes=%d truncated=%v err=%v", len(encoded), result.Truncated, err)
	}
}

func TestSearchFailures(t *testing.T) {
	tests := []struct {
		name          string
		handler       http.HandlerFunc
		clientTimeout time.Duration
		want          string
	}{
		{"HTTP error", func(w http.ResponseWriter, r *http.Request) { http.Error(w, "no", http.StatusBadGateway) }, time.Second, "HTTP 502"},
		{"invalid JSON", func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("{")) }, time.Second, "invalid JSON"},
		{"timeout", func(w http.ResponseWriter, r *http.Request) {
			time.Sleep(100 * time.Millisecond)
			_, _ = w.Write([]byte(`{"results":[]}`))
		}, 10 * time.Millisecond, "deadline exceeded"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(test.handler)
			defer server.Close()
			client, _ := New(server.URL, &http.Client{Timeout: test.clientTimeout}, 8)
			_, err := client.Search(context.Background(), Params{Query: "query"})
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("error %v does not contain %q", err, test.want)
			}
		})
	}
}

func TestSearchConnectionErrorAndValidation(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	base := server.URL
	server.Close()
	client, _ := New(base, &http.Client{Timeout: time.Second}, 8)
	if _, err := client.Search(context.Background(), Params{Query: "q"}); err == nil || !strings.Contains(err.Error(), "request failed") {
		t.Fatalf("expected connection error, got %v", err)
	}
	for _, params := range []Params{{}, {Query: "q", MaxResults: 21}, {Query: "q", Page: 51}, {Query: "q", TimeRange: "week"}, {Query: "q", Category: "images"}} {
		if _, err := client.Search(context.Background(), params); err == nil {
			t.Fatalf("expected validation error for %+v", params)
		}
	}
}
