package fetch

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
	"unicode/utf8"
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
	<header>menu</header><nav>links</nav><main><h1>Heading</h1><p>Hello <a href="/article">world</a>.</p><p>Hello <a href="/article">world</a>.</p>
	<ul><li>First</li><li>Second</li></ul><table><tr><th>Version</th><th>Status</th></tr><tr><td>1.8</td><td>Stable</td></tr></table><script>ignore()</script><noscript>ignore</noscript><svg><text>ignore</text></svg></main>
	<footer>legal</footer></body></html>`
	title, markdown, err := htmlToMarkdown([]byte(html), "text/html; charset=utf-8", mustURL(t, "https://example.com/base"))
	if err != nil {
		t.Fatal(err)
	}
	if title != "Example title" || !strings.Contains(markdown, "# Heading") ||
		!strings.Contains(markdown, "[world](https://example.com/article)") ||
		!strings.Contains(markdown, "- First") || !strings.Contains(markdown, "| Version | Status |") {
		t.Fatalf("unexpected markdown: title=%q body=%q", title, markdown)
	}
	if strings.Count(deduplicateBlocks(markdown), "Hello [world]") != 1 {
		t.Fatalf("duplicate article paragraph remained: %q", markdown)
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

func TestShortDocumentsAreFullAndLongDocumentsUseBoundaries(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		if r.URL.Path == "/short" {
			_, _ = io.WriteString(w, "Короткий полный документ.")
			return
		}
		_, _ = io.WriteString(w, strings.Repeat("Complete sentence with useful context. ", 200))
	}))
	defer server.Close()
	fetcher := localFetcher(server)
	short, err := fetcher.Fetch(context.Background(), server.URL+"/short")
	if err != nil || short.Truncated || short.SelectionMode != "full" || short.Content != "Короткий полный документ." {
		t.Fatalf("short document changed: %+v, %v", short, err)
	}
	fetcher.SetLimits(1<<20, 350)
	long, err := fetcher.Fetch(context.Background(), server.URL+"/long")
	if err != nil || !long.Truncated || long.SelectionMode != "leading" {
		t.Fatalf("long document metadata: %+v, %v", long, err)
	}
	if len([]rune(long.Content)) > 350 || !strings.HasSuffix(long.Content, ".") || !utf8.ValidString(long.Content) {
		t.Fatalf("poor truncation boundary: %q", long.Content)
	}
}

func TestQueryFocusedSelectionLatinCyrillicHeadingAndNeighbors(t *testing.T) {
	document := syntheticDocumentation()
	selected, mode, _, chunks, truncated := selectContent(document, "CUDA support version 1.8", 4_000, false)
	if mode != "query_relevant" || !truncated || chunks == 0 || !strings.Contains(selected, "CUDA Acceleration") {
		t.Fatalf("Latin query missed relevant heading: mode=%s chunks=%d content=%q", mode, chunks, selected)
	}
	if !strings.Contains(selected, "Context before CUDA") || !strings.Contains(selected, "Context after CUDA") {
		t.Fatalf("neighbor context missing: %q", selected)
	}
	cyrillic, mode, _, _, _ := selectContent(document, "поддержка кириллицы поиск", 4_000, false)
	if mode != "query_relevant" || !strings.Contains(cyrillic, "Кириллический поиск") {
		t.Fatalf("Cyrillic query missed relevant section: %q", cyrillic)
	}
	if strings.Count(selected, "CUDA support was added") != 1 {
		t.Fatalf("overlapping chunks duplicated content: %q", selected)
	}
}

func TestQuerySelectionFindsBeginningMiddleAndEnd(t *testing.T) {
	document := strings.Join([]string{
		"# Start marker\n\nstartneedle authoritative opening fact",
		strings.Repeat("ordinary background sentence. ", 250),
		"# Middle marker\n\nmiddleneedle important middle fact",
		strings.Repeat("additional background sentence. ", 250),
		"# End marker\n\nendneedle decisive final fact",
	}, "\n\n")
	for query, want := range map[string]string{
		"startneedle":  "authoritative opening fact",
		"middleneedle": "important middle fact",
		"endneedle":    "decisive final fact",
	} {
		selected, mode, _, _, _ := selectContent(document, query, 1_500, false)
		if mode != "query_relevant" || !strings.Contains(selected, want) {
			t.Fatalf("query %q missed %q: %q", query, want, selected)
		}
	}
}

func TestHeadingMatchGetsAdditionalWeight(t *testing.T) {
	document := "# Background\n\npriorityterm appears in an ordinary paragraph.\n\n" +
		strings.Repeat("filler material. ", 300) +
		"\n\n# Priorityterm Configuration\n\nThe selected heading section contains the decisive setting."
	selected, mode, _, _, _ := selectContent(document, "priorityterm", 600, false)
	if mode != "query_relevant" || !strings.Contains(selected, "decisive setting") {
		t.Fatalf("heading boost did not select heading section: %q", selected)
	}
}

func TestFetchQueryMetadataAndHardMaximum(t *testing.T) {
	document := syntheticDocumentation() + "\n\n" + strings.Repeat("Tail sentence. ", 20_000)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = io.WriteString(w, document)
	}))
	defer server.Close()
	fetcher := localFetcher(server)
	fetcher.SetLimits(defaultMaxResponse, 1_000_000)
	result, err := fetcher.Fetch(context.Background(), server.URL, "Кириллический поиск")
	if err != nil {
		t.Fatal(err)
	}
	if result.SelectionMode != "query_relevant" || result.Query == "" || !result.Truncated ||
		result.ReturnedCharacters > hardMaxText || result.ReturnedBytes != len(result.Content) || result.ApproximateTokens == 0 {
		t.Fatalf("query/hard-limit metadata failure: %+v", result)
	}
}

func TestLargeJSONAndPlainTextBudgets(t *testing.T) {
	largeJSON, _ := json.Marshal(map[string]any{
		"leading":  strings.Repeat("noise ", 8_000),
		"target":   "release 1.8 enables CUDA graph support",
		"trailing": strings.Repeat("noise ", 8_000),
	})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/json" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write(largeJSON)
			return
		}
		w.Header().Set("Content-Type", "text/plain")
		_, _ = io.WriteString(w, strings.Repeat("plain paragraph. ", 10_000))
	}))
	defer server.Close()
	fetcher := localFetcher(server)
	jsonResult, err := fetcher.Fetch(context.Background(), server.URL+"/json", "CUDA graph support")
	if err != nil || jsonResult.SelectionMode != "query_relevant" || !strings.Contains(jsonResult.Content, "CUDA graph support") {
		t.Fatalf("large JSON relevance failed: %+v, %v", jsonResult, err)
	}
	plainResult, err := fetcher.Fetch(context.Background(), server.URL+"/plain")
	if err != nil || !plainResult.Truncated || plainResult.ReturnedCharacters > defaultMaxText {
		t.Fatalf("plain text budget failed: %+v, %v", plainResult, err)
	}
}

func TestPDFQuerySelectionPreservesPageNumbers(t *testing.T) {
	body := multiPagePDF([]string{
		"Opening background material",
		"Middle page CUDA support version 1.8",
		"Ending unrelated appendix",
	})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/pdf")
		_, _ = w.Write(body)
	}))
	defer server.Close()
	fetcher := localFetcher(server)
	fetcher.SetLimits(defaultMaxResponse, 80)
	result, err := fetcher.Fetch(context.Background(), server.URL, "CUDA support")
	if err != nil {
		t.Fatal(err)
	}
	if result.SelectionMode != "query_relevant" || !strings.Contains(result.Content, "Page 2") ||
		!strings.Contains(result.Content, "CUDA support") || len(result.PDFPages) == 0 || result.PDFPages[0] != 2 {
		t.Fatalf("PDF selection/page metadata failed: %+v", result)
	}
}

func TestLongPDFLeadingAndLatePageQuery(t *testing.T) {
	pages := make([]string, 12)
	for index := range pages {
		pages[index] = fmt.Sprintf("Page material %d %s", index+1, strings.Repeat("detail ", 80))
	}
	pages[11] += " finalneedle decisive result"
	body := multiPagePDF(pages)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/pdf")
		_, _ = w.Write(body)
	}))
	defer server.Close()
	fetcher := localFetcher(server)
	fetcher.SetLimits(defaultMaxResponse, 800)
	leading, err := fetcher.Fetch(context.Background(), server.URL)
	if err != nil || leading.SelectionMode != "leading" || !leading.Truncated || len(leading.PDFPages) == 0 || leading.PDFPages[0] != 1 {
		t.Fatalf("long PDF leading budget failed: %+v, %v", leading, err)
	}
	focused, err := fetcher.Fetch(context.Background(), server.URL, "finalneedle")
	if err != nil || focused.SelectionMode != "query_relevant" || !strings.Contains(focused.Content, "finalneedle") ||
		len(focused.PDFPages) == 0 || focused.PDFPages[0] != 12 {
		t.Fatalf("late PDF page query failed: %+v, %v", focused, err)
	}
}

func TestInternalExtractionLimitIsSeparate(t *testing.T) {
	body := "needle opening evidence.\n\n" + strings.Repeat("large extracted source sentence. ", 25_000)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = io.WriteString(w, body)
	}))
	defer server.Close()
	fetcher := localFetcher(server)
	fetcher.SetLimits(2<<20, 1_000_000)
	result, err := fetcher.Fetch(context.Background(), server.URL, "needle")
	if err != nil {
		t.Fatal(err)
	}
	if result.ExtractedCharacters <= maxExtractedText || !result.Truncated || result.ReturnedCharacters > hardMaxText ||
		!strings.Contains(result.Content, "opening evidence") {
		t.Fatalf("internal extraction budget failure: %+v", result)
	}
}

func TestHTTPDownloadLimitIsIndependentFromOutputLimit(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = io.WriteString(w, strings.Repeat("x", 2_000))
	}))
	defer server.Close()
	fetcher := localFetcher(server)
	fetcher.SetLimits(1_000, 100)
	_, err := fetcher.Fetch(context.Background(), server.URL)
	if err == nil || !strings.Contains(err.Error(), "too large") && !strings.Contains(err.Error(), "exceeds") {
		t.Fatalf("download limit not enforced separately: %v", err)
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

func syntheticDocumentation() string {
	return strings.Join([]string{
		"# Introduction",
		strings.Repeat("General installation background with portable runtime details. ", 180),
		"# CUDA Acceleration",
		"Context before CUDA describes the compatibility assumptions.",
		"CUDA support was added in version 1.8 with graph execution enabled by default.",
		"Context after CUDA explains the fallback behavior on older devices.",
		"# Локализация",
		"Соседний контекст до важного изменения.",
		"Кириллический поиск получил поддержку нормализации запроса и заголовков.",
		"Соседний контекст после важного изменения.",
		"# Appendix",
		strings.Repeat("Unrelated reference material and ordinary examples. ", 180),
	}, "\n\n")
}

func multiPagePDF(texts []string) []byte {
	fontObject := 3 + len(texts)
	firstContentObject := fontObject + 1
	kids := make([]string, len(texts))
	objects := make([]string, 0, 3+len(texts)*2)
	objects = append(objects, "<< /Type /Catalog /Pages 2 0 R >>")
	for index := range texts {
		kids[index] = fmt.Sprintf("%d 0 R", 3+index)
	}
	objects = append(objects, fmt.Sprintf("<< /Type /Pages /Kids [%s] /Count %d >>", strings.Join(kids, " "), len(texts)))
	for index := range texts {
		objects = append(objects, fmt.Sprintf(
			"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>",
			fontObject, firstContentObject+index,
		))
	}
	objects = append(objects, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
	for _, text := range texts {
		stream := fmt.Sprintf("BT /F1 12 Tf 72 720 Td (%s) Tj ET", text)
		objects = append(objects, fmt.Sprintf("<< /Length %d >>\nstream\n%s\nendstream", len(stream), stream))
	}
	var output strings.Builder
	output.WriteString("%PDF-1.4\n")
	offsets := []int{0}
	for index, object := range objects {
		offsets = append(offsets, output.Len())
		fmt.Fprintf(&output, "%d 0 obj\n%s\nendobj\n", index+1, object)
	}
	xref := output.Len()
	fmt.Fprintf(&output, "xref\n0 %d\n0000000000 65535 f \n", len(objects)+1)
	for _, offset := range offsets[1:] {
		fmt.Fprintf(&output, "%010d 00000 n \n", offset)
	}
	fmt.Fprintf(&output, "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n", len(objects)+1, xref)
	return []byte(output.String())
}
