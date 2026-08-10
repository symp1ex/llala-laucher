package search

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
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
		result.Results[0].Score == nil || result.Results[0].PublishedDate == "" {
		t.Fatalf("unexpected normalized result: %+v", result)
	}
}

func TestSearchResultCountAndOutputSizeLimits(t *testing.T) {
	results := make([]map[string]any, 5)
	for i := range results {
		results[i] = map[string]any{"title": "result", "url": "https://example.com", "content": strings.Repeat("x", 100)}
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

func TestSearchFailures(t *testing.T) {
	tests := []struct {
		name          string
		handler       http.HandlerFunc
		clientTimeout time.Duration
		want          string
	}{
		{"HTTP error", func(w http.ResponseWriter, r *http.Request) { http.Error(w, "no", http.StatusBadGateway) }, time.Second, "HTTP 502"},
		{"invalid JSON", func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("{")) }, time.Second, "invalid JSON"},
		{"empty", func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(`{"results":[]}`)) }, time.Second, "no results"},
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
