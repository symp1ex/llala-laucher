package search

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	stdhtml "html"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode"

	"llala-launcher/mcp/internal/dateutil"
	"llala-launcher/mcp/internal/textutil"
)

const (
	maxResponseBody  = 4 << 20
	defaultMaxOutput = 32 << 10
	maxSnippetChars  = 700
	maxPage          = 50
)

type Client struct {
	baseURL       *url.URL
	httpClient    *http.Client
	defaultLimit  int
	maxOutputSize int
}

type Params struct {
	Query                string
	MaxResults           int
	Language             string
	Page                 int
	TimeRange            string
	Category             string
	PublishedAfter       string
	PublishedBefore      string
	RequirePublishedDate bool
}

type Result struct {
	Rank              int      `json:"rank"`
	Title             string   `json:"title"`
	URL               string   `json:"url"`
	Snippet           string   `json:"snippet,omitempty"`
	Engines           []string `json:"engines,omitempty"`
	Score             *float64 `json:"score,omitempty"`
	PublishedDate     string   `json:"publishedDate,omitempty"`
	SourceDomain      string   `json:"sourceDomain"`
	PublishedAt       string   `json:"publishedAt,omitempty"`
	PublishedAtSource string   `json:"publishedAtSource,omitempty"`
	DateVerified      bool     `json:"dateVerified"`
	TitleClusterID    string   `json:"titleClusterId"`
}

type Response struct {
	Notice                string             `json:"notice"`
	Query                 string             `json:"query"`
	Results               []Result           `json:"results"`
	RawResultCount        int                `json:"rawResultCount"`
	ResultCount           int                `json:"result_count"`
	StructuredResultCount int                `json:"resultCount"`
	Empty                 bool               `json:"empty"`
	UnresponsiveEngines   []EngineDiagnostic `json:"unresponsiveEngines"`
	ElapsedMS             int64              `json:"elapsedMs"`
	RetrievedAt           string             `json:"retrievedAt"`
	Freshness             AppliedFreshness   `json:"freshness"`
	Truncated             bool               `json:"truncated"`
	ReturnedCharacters    int                `json:"returned_characters"`
	ApproximateTokens     int                `json:"approximate_tokens"`
}

type EngineDiagnostic struct {
	Engine string `json:"engine"`
	Error  string `json:"error,omitempty"`
}

type AppliedFreshness struct {
	TimeRange            string `json:"timeRange,omitempty"`
	PublishedAfter       string `json:"publishedAfter,omitempty"`
	PublishedBefore      string `json:"publishedBefore,omitempty"`
	RequirePublishedDate bool   `json:"requirePublishedDate"`
}

type rawResponse struct {
	Results             []rawResult     `json:"results"`
	UnresponsiveEngines json.RawMessage `json:"unresponsive_engines"`
}

type rawResult struct {
	Title         string   `json:"title"`
	URL           string   `json:"url"`
	Content       string   `json:"content"`
	Engines       []string `json:"engines"`
	Score         *float64 `json:"score"`
	PublishedDate string   `json:"publishedDate"`
	Category      string   `json:"category"`
}

func New(base string, httpClient *http.Client, defaultLimit int) (*Client, error) {
	parsed, err := url.Parse(strings.TrimRight(base, "/"))
	if err != nil || parsed.Hostname() == "" || parsed.User != nil ||
		(parsed.Scheme != "http" && parsed.Scheme != "https") {
		return nil, errors.New("invalid SearXNG base URL")
	}
	if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, errors.New("SearXNG base URL must not contain credentials, query, or fragment")
	}
	if defaultLimit < 1 || defaultLimit > 20 {
		return nil, errors.New("default max results must be between 1 and 20")
	}
	if httpClient == nil {
		return nil, errors.New("HTTP client is required")
	}
	return &Client{parsed, httpClient, defaultLimit, defaultMaxOutput}, nil
}

func (c *Client) SetMaxOutputSize(size int) {
	c.maxOutputSize = size
}

func (c *Client) Search(ctx context.Context, params Params) (Response, error) {
	started := time.Now()
	params.Query = strings.TrimSpace(params.Query)
	if params.Query == "" {
		return Response{}, errors.New("query must not be empty")
	}
	if params.MaxResults == 0 {
		params.MaxResults = c.defaultLimit
	}
	if params.MaxResults < 1 || params.MaxResults > 20 {
		return Response{}, errors.New("max_results must be between 1 and 20")
	}
	if params.Page == 0 {
		params.Page = 1
	}
	if params.Page < 1 || params.Page > maxPage {
		return Response{}, fmt.Errorf("page must be between 1 and %d", maxPage)
	}
	if params.TimeRange != "" && params.TimeRange != "day" && params.TimeRange != "month" && params.TimeRange != "year" {
		return Response{}, errors.New("time_range must be day, month, or year")
	}
	if params.Category != "" && params.Category != "general" && params.Category != "news" {
		return Response{}, errors.New("category must be general or news")
	}
	publishedAfter, publishedBefore, normalizedAfter, normalizedBefore, err := validateDateBounds(params)
	if err != nil {
		return Response{}, err
	}

	target := *c.baseURL
	target.Path = strings.TrimRight(target.Path, "/") + "/search"
	values := target.Query()
	values.Set("q", params.Query)
	values.Set("format", "json")
	values.Set("pageno", fmt.Sprint(params.Page))
	if params.Language != "" {
		values.Set("language", params.Language)
	}
	if params.TimeRange != "" {
		values.Set("time_range", params.TimeRange)
	}
	if params.Category != "" {
		values.Set("categories", params.Category)
	}
	target.RawQuery = values.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
	if err != nil {
		return Response{}, fmt.Errorf("create SearXNG request: %w", err)
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "llala-web-mcp/1")
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return Response{}, fmt.Errorf("SearXNG request failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return Response{}, fmt.Errorf("SearXNG returned HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBody+1))
	if err != nil {
		return Response{}, fmt.Errorf("read SearXNG response: %w", err)
	}
	if len(body) > maxResponseBody {
		return Response{}, errors.New("SearXNG response is too large")
	}
	var raw rawResponse
	if err := json.Unmarshal(body, &raw); err != nil {
		return Response{}, fmt.Errorf("SearXNG returned invalid JSON: %w", err)
	}
	output := Response{
		Notice: "Search snippets and SearXNG publishedDate values are unverified leads, not page evidence. Fetch the page or use web_news_research to verify freshness.",
		Query:  params.Query, Results: make([]Result, 0), RawResultCount: len(raw.Results),
		UnresponsiveEngines: parseUnresponsiveEngines(raw.UnresponsiveEngines),
		Freshness: AppliedFreshness{
			TimeRange: params.TimeRange, PublishedAfter: normalizedAfter, PublishedBefore: normalizedBefore,
			RequirePublishedDate: params.RequirePublishedDate,
		},
	}
	seenURLs := make(map[string]struct{})
	for _, item := range raw.Results {
		itemURL := strings.TrimSpace(item.URL)
		normalizedURL := normalizeURL(itemURL)
		if itemURL == "" || normalizedURL == "" {
			continue
		}
		title := cleanText(item.Title)
		if title == "" {
			continue
		}
		if _, exists := seenURLs[normalizedURL]; exists {
			continue
		}
		publishedDate := strings.TrimSpace(item.PublishedDate)
		parsedPublishedAt, dateParsed := dateutil.Parse(publishedDate)
		if dateParsed && publishedAfter != nil && !parsedPublishedAt.After(*publishedAfter) {
			continue
		}
		if dateParsed && publishedBefore != nil && !parsedPublishedAt.Before(*publishedBefore) {
			continue
		}
		if !dateParsed && params.RequirePublishedDate {
			continue
		}
		if len(output.Results) >= params.MaxResults {
			output.Truncated = true
			break
		}
		result := Result{
			Rank: len(output.Results) + 1, Title: title, URL: normalizedURL,
			Snippet: trimSnippet(cleanText(item.Content)), Engines: compactStrings(item.Engines), Score: item.Score,
			PublishedDate: publishedDate, SourceDomain: sourceDomain(normalizedURL),
			DateVerified: false, TitleClusterID: titleClusterID(title),
		}
		if dateParsed {
			result.PublishedAt = dateutil.Format(parsedPublishedAt)
			result.PublishedAtSource = "searxng_index"
		}
		candidate := output
		candidate.Results = append(append([]Result(nil), output.Results...), result)
		populateMetadata(&candidate)
		finishResponse(&candidate, started)
		encoded, _ := json.Marshal(candidate)
		if len(encoded) > c.maxOutputSize {
			output.Truncated = true
			break
		}
		output.Results = append(output.Results, result)
		seenURLs[normalizedURL] = struct{}{}
	}
	populateMetadata(&output)
	finishResponse(&output, started)
	encoded, _ := json.Marshal(output)
	for len(encoded) > c.maxOutputSize && len(output.Results) > 0 {
		output.Results = output.Results[:len(output.Results)-1]
		output.Truncated = true
		populateMetadata(&output)
		finishResponse(&output, started)
		encoded, _ = json.Marshal(output)
	}
	if len(encoded) > c.maxOutputSize {
		return Response{}, errors.New("SearXNG results exceeded the output limit")
	}
	return output, nil
}

func validateDateBounds(params Params) (*time.Time, *time.Time, string, string, error) {
	parse := func(name, value string) (*time.Time, string, error) {
		value = strings.TrimSpace(value)
		if value == "" {
			return nil, "", nil
		}
		parsed, err := time.Parse(time.RFC3339, value)
		if err != nil {
			return nil, "", fmt.Errorf("%s must be RFC3339: %w", name, err)
		}
		parsed = parsed.UTC()
		return &parsed, parsed.Format(time.RFC3339), nil
	}
	after, normalizedAfter, err := parse("published_after", params.PublishedAfter)
	if err != nil {
		return nil, nil, "", "", err
	}
	before, normalizedBefore, err := parse("published_before", params.PublishedBefore)
	if err != nil {
		return nil, nil, "", "", err
	}
	if after != nil && before != nil && !after.Before(*before) {
		return nil, nil, "", "", errors.New("published_after must be before published_before")
	}
	return after, before, normalizedAfter, normalizedBefore, nil
}

func parseUnresponsiveEngines(raw json.RawMessage) []EngineDiagnostic {
	if raw == nil || strings.EqualFold(strings.TrimSpace(string(raw)), "null") {
		return nil
	}
	result := make([]EngineDiagnostic, 0)
	var values []any
	if err := json.Unmarshal(raw, &values); err != nil {
		return []EngineDiagnostic{{Error: "unrecognized SearXNG diagnostic format"}}
	}
	for _, value := range values {
		diagnostic := EngineDiagnostic{}
		switch typed := value.(type) {
		case []any:
			if len(typed) > 0 {
				diagnostic.Engine, _ = typed[0].(string)
			}
			if len(typed) > 1 {
				diagnostic.Error, _ = typed[1].(string)
			}
		case map[string]any:
			diagnostic.Engine, _ = typed["engine"].(string)
			diagnostic.Error, _ = typed["error"].(string)
		case string:
			diagnostic.Engine = typed
		}
		diagnostic.Engine = strings.TrimSpace(diagnostic.Engine)
		diagnostic.Error = strings.TrimSpace(diagnostic.Error)
		if diagnostic.Engine != "" || diagnostic.Error != "" {
			result = append(result, diagnostic)
		}
	}
	return result
}

func finishResponse(response *Response, started time.Time) {
	response.Empty = len(response.Results) == 0
	response.ElapsedMS = time.Since(started).Milliseconds()
	response.RetrievedAt = time.Now().UTC().Format(time.RFC3339)
}

func cleanText(value string) string {
	value = snippetTags.ReplaceAllString(value, " ")
	return strings.Join(strings.Fields(stdhtml.UnescapeString(value)), " ")
}

var snippetTags = regexp.MustCompile(`<[^>]*>`)

func trimSnippet(value string) string {
	trimmed, _ := textutil.TruncateBoundary(value, maxSnippetChars)
	return trimmed
}

func normalizeURL(raw string) string {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Hostname() == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return ""
	}
	parsed.Scheme = strings.ToLower(parsed.Scheme)
	parsed.Host = strings.ToLower(parsed.Host)
	parsed.Fragment = ""
	query := parsed.Query()
	for key := range query {
		lower := strings.ToLower(key)
		if strings.HasPrefix(lower, "utm_") || trackingParameters[lower] {
			query.Del(key)
		}
	}
	parsed.RawQuery = query.Encode()
	if parsed.Path != "/" {
		parsed.Path = strings.TrimRight(parsed.Path, "/")
	}
	return parsed.String()
}

var trackingParameters = map[string]bool{
	"fbclid": true, "gclid": true, "dclid": true, "msclkid": true,
	"mc_cid": true, "mc_eid": true, "igshid": true, "yclid": true,
}

func normalizeTitle(value string) string {
	return strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || unicode.IsNumber(r) {
			return unicode.ToLower(r)
		}
		if unicode.IsSpace(r) {
			return ' '
		}
		return -1
	}, strings.Join(strings.Fields(value), " "))
}

func titleClusterID(title string) string {
	hash := sha256.Sum256([]byte(normalizeTitle(title)))
	return fmt.Sprintf("title-%x", hash[:8])
}

func sourceDomain(rawURL string) string {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return ""
	}
	return strings.ToLower(parsed.Hostname())
}

func compactStrings(values []string) []string {
	seen := make(map[string]struct{})
	result := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		if _, exists := seen[value]; exists {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	sort.Strings(result)
	return result
}

func populateMetadata(response *Response) {
	response.ResultCount = len(response.Results)
	response.StructuredResultCount = response.ResultCount
	characters := 0
	var text strings.Builder
	for _, result := range response.Results {
		characters += textutil.RuneCount(result.Title) + textutil.RuneCount(result.Snippet)
		text.WriteString(result.Title)
		text.WriteByte('\n')
		text.WriteString(result.Snippet)
		text.WriteByte('\n')
	}
	response.ReturnedCharacters = characters
	response.ApproximateTokens = textutil.EstimateTokens(text.String())
}
