package fetch

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

type staticResolver map[string][]net.IPAddr

func (r staticResolver) LookupIPAddr(_ context.Context, host string) ([]net.IPAddr, error) {
	addresses, ok := r[host]
	if !ok {
		return nil, fmt.Errorf("unknown host %s", host)
	}
	return addresses, nil
}

func localFetcher(server *httptest.Server) *Fetcher {
	return NewForTest(server.Client(), staticResolver{}, true)
}

func TestHTMLToMarkdownRemovesBoilerplateAndPreservesStructure(t *testing.T) {
	html := `<!doctype html><html><head><title> Example title </title><style>bad</style></head><body>
	<header>menu</header><nav>links</nav><main><h1>Heading</h1><p>Hello <a href="/article">world</a>.</p>
	<ul><li>First</li><li>Second</li></ul><script>ignore()</script><noscript>ignore</noscript><svg><text>ignore</text></svg></main>
	<footer>legal</footer></body></html>`
	title, markdown, err := htmlToMarkdown([]byte(html), "text/html; charset=utf-8", mustURL(t, "https://example.com/base"))
	if err != nil {
		t.Fatal(err)
	}
	if title != "Example title" || !strings.Contains(markdown, "# Heading") ||
		!strings.Contains(markdown, "[world](https://example.com/article)") ||
		!strings.Contains(markdown, "- First") {
		t.Fatalf("unexpected markdown: title=%q body=%q", title, markdown)
	}
	for _, forbidden := range []string{"menu", "ignore", "legal"} {
		if strings.Contains(markdown, forbidden) {
			t.Fatalf("boilerplate %q remained in %q", forbidden, markdown)
		}
	}
}

func TestFetchPlainTextJSONAndHTML(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/plain":
			w.Header().Set("Content-Type", "text/plain; charset=utf-8")
			_, _ = io.WriteString(w, "plain content")
		case "/json":
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"ok":true}`)
		default:
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = io.WriteString(w, `<html><head><title>T</title></head><body><main><p>Readable page</p></main></body></html>`)
		}
	}))
	defer server.Close()
	fetcher := localFetcher(server)
	for path, want := range map[string]string{"/plain": "plain content", "/json": "\"ok\": true", "/html": "Readable page"} {
		result, err := fetcher.Fetch(context.Background(), server.URL+path)
		if err != nil || !strings.Contains(result.Content, want) || !strings.Contains(result.Notice, "UNTRUSTED") {
			t.Fatalf("%s: result=%+v err=%v", path, result, err)
		}
	}
}

func TestFetchPDFByPage(t *testing.T) {
	body := minimalPDF("Hello PDF")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/pdf")
		_, _ = w.Write(body)
	}))
	defer server.Close()
	result, err := localFetcher(server).Fetch(context.Background(), server.URL)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(result.Content, "## Page 1") || !strings.Contains(result.Content, "Hello PDF") {
		t.Fatalf("unexpected PDF output: %q", result.Content)
	}
}

func TestFetchRedirectFinalURLAndLimit(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/start" {
			http.Redirect(w, r, "/final", http.StatusFound)
			return
		}
		w.Header().Set("Content-Type", "text/plain")
		_, _ = io.WriteString(w, strings.Repeat("é", 100))
	}))
	defer server.Close()
	fetcher := localFetcher(server)
	fetcher.SetLimits(1<<20, 31)
	result, err := fetcher.Fetch(context.Background(), server.URL+"/start")
	if err != nil {
		t.Fatal(err)
	}
	if result.FinalURL != server.URL+"/final" || !result.Truncated || !strings.HasPrefix(result.SourceURL, server.URL+"/start") {
		t.Fatalf("unexpected redirect/truncation result: %+v", result)
	}
}

func TestFetchSizeContentTypeAndTimeoutFailures(t *testing.T) {
	tests := []struct {
		name      string
		handler   http.HandlerFunc
		configure func(*Fetcher)
		want      string
	}{
		{"size", func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/plain")
			_, _ = io.WriteString(w, "123456")
		}, func(f *Fetcher) { f.SetLimits(5, 100) }, "too large"},
		{"content type", func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "image/png")
			_, _ = w.Write([]byte("png"))
		}, func(*Fetcher) {}, "unsupported Content-Type"},
		{"invalid json", func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, "{")
		}, func(*Fetcher) {}, "not valid JSON"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(test.handler)
			defer server.Close()
			fetcher := localFetcher(server)
			test.configure(fetcher)
			_, err := fetcher.Fetch(context.Background(), server.URL)
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("error %v does not contain %q", err, test.want)
			}
		})
	}
	timeoutServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(100 * time.Millisecond)
		_, _ = io.WriteString(w, "late")
	}))
	defer timeoutServer.Close()
	client := timeoutServer.Client()
	client.Timeout = 10 * time.Millisecond
	_, err := NewForTest(client, staticResolver{}, true).Fetch(context.Background(), timeoutServer.URL)
	if err == nil || !strings.Contains(err.Error(), "Client.Timeout") {
		t.Fatalf("expected timeout, got %v", err)
	}
}

func TestURLAndSSRFValidation(t *testing.T) {
	resolver := staticResolver{
		"public.example":  {{IP: net.ParseIP("93.184.216.34")}},
		"private.example": {{IP: net.ParseIP("10.0.0.1")}},
		"mixed.example":   {{IP: net.ParseIP("93.184.216.34")}, {IP: net.ParseIP("127.0.0.1")}},
		"ipv6.example":    {{IP: net.ParseIP("::1")}},
		"127.0.0.1":       {{IP: net.ParseIP("127.0.0.1")}},
		"::1":             {{IP: net.ParseIP("::1")}},
	}
	fetcher := NewForTest(&http.Client{}, resolver, false)
	allowed, _ := url.Parse("https://public.example/page")
	if err := fetcher.validateTarget(context.Background(), allowed); err != nil {
		t.Fatalf("public URL rejected: %v", err)
	}
	blocked := []string{
		"file:///etc/passwd", "http://user:pass@public.example/", "http://127.0.0.1/",
		"http://private.example/", "http://mixed.example/", "http://[::1]/", "http://ipv6.example/",
	}
	for _, raw := range blocked {
		t.Run(raw, func(t *testing.T) {
			target, err := url.Parse(raw)
			if err != nil {
				t.Fatal(err)
			}
			if err := fetcher.validateTarget(context.Background(), target); err == nil {
				t.Fatalf("expected %s to be blocked", raw)
			} else if strings.HasPrefix(raw, "http") && !strings.Contains(raw, "user:pass") && !strings.Contains(err.Error(), "SSRF") {
				t.Fatalf("expected SSRF rejection for %s, got %v", raw, err)
			}
		})
	}
}

func TestRedirectToPrivateAddressIsRejected(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusFound,
			Header:     http.Header{"Location": []string{"http://127.0.0.1/private"}},
			Body:       io.NopCloser(strings.NewReader("")), Request: req,
		}, nil
	})}
	resolver := staticResolver{
		"public.example": {{IP: net.ParseIP("93.184.216.34")}},
		"127.0.0.1":      {{IP: net.ParseIP("127.0.0.1")}},
	}
	fetcher := NewForTest(client, resolver, false)
	_, err := fetcher.Fetch(context.Background(), "http://public.example/start")
	if err == nil || !strings.Contains(err.Error(), "redirect rejected") || !strings.Contains(err.Error(), "SSRF") {
		t.Fatalf("expected redirect SSRF rejection, got %v", err)
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func mustURL(t *testing.T, value string) *url.URL {
	t.Helper()
	parsed, err := url.Parse(value)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func minimalPDF(text string) []byte {
	objects := []string{
		"<< /Type /Catalog /Pages 2 0 R >>",
		"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
		"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
		"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
	}
	stream := fmt.Sprintf("BT /F1 12 Tf 72 720 Td (%s) Tj ET", text)
	objects = append(objects, fmt.Sprintf("<< /Length %d >>\nstream\n%s\nendstream", len(stream), stream))
	var output strings.Builder
	output.WriteString("%PDF-1.4\n")
	offsets := []int{0}
	for i, object := range objects {
		offsets = append(offsets, output.Len())
		fmt.Fprintf(&output, "%d 0 obj\n%s\nendobj\n", i+1, object)
	}
	xref := output.Len()
	fmt.Fprintf(&output, "xref\n0 %d\n0000000000 65535 f \n", len(objects)+1)
	for _, offset := range offsets[1:] {
		fmt.Fprintf(&output, "%010d 00000 n \n", offset)
	}
	fmt.Fprintf(&output, "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n", len(objects)+1, xref)
	return []byte(output.String())
}
