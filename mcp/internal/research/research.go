package research

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/publicsuffix"

	"llala-launcher/mcp/internal/dateutil"
	"llala-launcher/mcp/internal/fetch"
	"llala-launcher/mcp/internal/search"
	"llala-launcher/mcp/internal/textutil"
)

const (
	defaultMaxStories    = 5
	maxCandidatesLimit   = 40
	maxConcurrency       = 4
	standardTargetBytes  = 24 << 10
	hardOutputLimitBytes = 32 << 10
)

type Searcher interface {
	Search(context.Context, search.Params) (search.Response, error)
}

type Fetcher interface {
	Fetch(context.Context, string, ...string) (fetch.Result, error)
}

type Params struct {
	Queries            []string
	FreshnessHours     int
	MaxStories         int
	MaxCandidates      int
	Language           string
	MinDistinctDomains int
	ResponseMode       string
}

type Runner struct {
	searcher Searcher
	fetcher  Fetcher
	now      func() time.Time
}

type Response struct {
	Notice            string              `json:"notice"`
	RetrievedAt       string              `json:"retrievedAt"`
	Cutoff            string              `json:"cutoff"`
	FreshnessHours    int                 `json:"freshnessHours"`
	ApproximateRange  string              `json:"approximateTimeRange"`
	ElapsedMS         int64               `json:"elapsedMs"`
	QueryLog          []QueryLog          `json:"queryLog"`
	Errors            []ResearchError     `json:"errors"`
	Stories           []Story             `json:"stories"`
	Rejected          []RejectedCandidate `json:"rejected"`
	DistinctDomains   int                 `json:"distinctDomains"`
	CandidateCount    int                 `json:"candidateCount"`
	FetchedCount      int                 `json:"fetchedCount"`
	Empty             bool                `json:"empty"`
	Truncated         bool                `json:"truncated"`
	ResponseMode      string              `json:"responseMode"`
	AppliedLimits     AppliedLimits       `json:"appliedLimits"`
	OutputBudget      OutputBudget        `json:"outputBudget"`
	RejectedCounts    map[string]int      `json:"rejectedCounts"`
	RejectedCount     int                 `json:"rejectedCount"`
	RejectedTruncated bool                `json:"rejectedTruncated"`
}

type AppliedLimits struct {
	MaxStories             int `json:"maxStories"`
	MaxCandidates          int `json:"maxCandidates"`
	ExcerptCharacters      int `json:"excerptCharacters"`
	ExcerptSourcesPerStory int `json:"excerptSourcesPerStory"`
	AuthorsPerSource       int `json:"authorsPerSource"`
	DateEvidencePerSource  int `json:"dateEvidencePerSource"`
	RejectedExamples       int `json:"rejectedExamples"`
}

type OutputBudget struct {
	Mode          string `json:"mode"`
	LimitBytes    int    `json:"limitBytes"`
	TargetBytes   int    `json:"targetBytes,omitempty"`
	ReturnedBytes int    `json:"returnedBytes"`
	Truncated     bool   `json:"truncated"`
}

type QueryLog struct {
	Query               string                    `json:"query"`
	RawResultCount      int                       `json:"rawResultCount"`
	ResultCount         int                       `json:"resultCount"`
	Empty               bool                      `json:"empty"`
	ElapsedMS           int64                     `json:"elapsedMs"`
	UnresponsiveEngines []search.EngineDiagnostic `json:"unresponsiveEngines"`
	Error               string                    `json:"error,omitempty"`
}

type ResearchError struct {
	Stage string `json:"stage"`
	Query string `json:"query,omitempty"`
	URL   string `json:"url,omitempty"`
	Error string `json:"error"`
}

type Story struct {
	Title              string        `json:"title"`
	TitleClusterID     string        `json:"titleClusterId"`
	PublishedAt        string        `json:"publishedAt"`
	Sources            []StorySource `json:"sources"`
	DistinctDomains    int           `json:"distinctDomains"`
	SourceCount        int           `json:"sourceCount"`
	ExcerptSourceCount int           `json:"excerptSourceCount"`
	ExcerptsTruncated  bool          `json:"excerptsTruncated"`
}

type StorySource struct {
	URL                   string               `json:"url"`
	CanonicalURL          string               `json:"canonicalUrl"`
	SourceDomain          string               `json:"sourceDomain"`
	RegistrableDomain     string               `json:"registrableDomain"`
	Publisher             string               `json:"publisher"`
	Authors               []string             `json:"authors"`
	AuthorsCount          int                  `json:"authorsCount"`
	AuthorsTruncated      bool                 `json:"authorsTruncated"`
	PublishedAt           string               `json:"publishedAt"`
	ModifiedAt            string               `json:"modifiedAt"`
	DateEvidence          []fetch.DateEvidence `json:"dateEvidence"`
	DateEvidenceCount     int                  `json:"dateEvidenceCount"`
	DateEvidenceTruncated bool                 `json:"dateEvidenceTruncated"`
	DateConfidence        string               `json:"dateConfidence"`
	DateConflict          bool                 `json:"dateConflict"`
	Excerpt               string               `json:"excerpt"`
}

type RejectedCandidate struct {
	Title        string `json:"title"`
	URL          string `json:"url"`
	SourceDomain string `json:"sourceDomain"`
	PublishedAt  string `json:"publishedAt,omitempty"`
	Reason       string `json:"reason"`
	Detail       string `json:"detail,omitempty"`
}

type candidate struct {
	result search.Result
}

type fetchOutcome struct {
	result fetch.Result
	err    error
}

type modeLimits struct {
	excerptCharacters      int
	excerptSourcesPerStory int
	authorsPerSource       int
	dateEvidencePerSource  int
	rejectedExamples       int
	targetBytes            int
}

func New(searcher Searcher, fetcher Fetcher) *Runner {
	return &Runner{searcher: searcher, fetcher: fetcher, now: time.Now}
}

func (r *Runner) Research(ctx context.Context, params Params) (Response, error) {
	started := time.Now()
	params, err := validateParams(params)
	if err != nil {
		return Response{}, err
	}
	limits := limitsForMode(params.ResponseMode)
	retrievedAt := r.now().UTC()
	cutoff := retrievedAt.Add(-time.Duration(params.FreshnessHours) * time.Hour)
	timeRange := approximateTimeRange(params.FreshnessHours)
	response := Response{
		Notice:      "Freshness is verified only from machine-readable page metadata. Snippets and index dates are not evidence; distinct domains are not necessarily independent sources.",
		RetrievedAt: dateutil.Format(retrievedAt), Cutoff: dateutil.Format(cutoff),
		FreshnessHours: params.FreshnessHours, ApproximateRange: timeRange,
		QueryLog: make([]QueryLog, 0, len(params.Queries)), Errors: make([]ResearchError, 0),
		Stories: make([]Story, 0), Rejected: make([]RejectedCandidate, 0),
		ResponseMode: params.ResponseMode, RejectedCounts: make(map[string]int),
		AppliedLimits: AppliedLimits{
			MaxStories: params.MaxStories, MaxCandidates: params.MaxCandidates,
			ExcerptCharacters: limits.excerptCharacters, ExcerptSourcesPerStory: limits.excerptSourcesPerStory,
			AuthorsPerSource: limits.authorsPerSource, DateEvidencePerSource: limits.dateEvidencePerSource,
			RejectedExamples: limits.rejectedExamples,
		},
		OutputBudget: OutputBudget{Mode: params.ResponseMode, LimitBytes: hardOutputLimitBytes, TargetBytes: limits.targetBytes},
	}

	queryCandidates := make([][]search.Result, len(params.Queries))
	for queryIndex, query := range params.Queries {
		if err := ctx.Err(); err != nil {
			return Response{}, err
		}
		searched, searchErr := r.searcher.Search(ctx, search.Params{
			Query: query, MaxResults: 20, Language: params.Language,
			Category: "news", TimeRange: timeRange,
		})
		log := QueryLog{Query: query}
		if searchErr != nil {
			if err := ctx.Err(); err != nil {
				return Response{}, err
			}
			log.Error = searchErr.Error()
			response.Errors = append(response.Errors, ResearchError{Stage: "search", Query: query, Error: searchErr.Error()})
			response.QueryLog = append(response.QueryLog, log)
			continue
		}
		log.RawResultCount = searched.RawResultCount
		log.ResultCount = searched.ResultCount
		log.Empty = searched.Empty
		log.ElapsedMS = searched.ElapsedMS
		log.UnresponsiveEngines = searched.UnresponsiveEngines
		response.QueryLog = append(response.QueryLog, log)
		if searched.Truncated {
			response.Truncated = true
		}
		queryCandidates[queryIndex] = searched.Results
	}
	allCandidates, candidatesTruncated := roundRobinCandidates(queryCandidates, params.MaxCandidates)
	response.Truncated = response.Truncated || candidatesTruncated
	response.CandidateCount = len(allCandidates)

	outcomes, err := r.fetchCandidates(ctx, allCandidates)
	if err != nil {
		return Response{}, err
	}
	response.FetchedCount = len(outcomes)
	storyOrder := make([]string, 0)
	storiesByCluster := make(map[string]*Story)
	for index, outcome := range outcomes {
		item := allCandidates[index].result
		if outcome.err != nil {
			response.Errors = append(response.Errors, ResearchError{Stage: "fetch", URL: item.URL, Error: outcome.err.Error()})
			response.Rejected = append(response.Rejected, rejected(item, "fetch_failed", outcome.err.Error(), ""))
			continue
		}
		page := outcome.result
		publishedAt, parsed := dateutil.Parse(page.PublishedAt)
		switch {
		case page.DateConflict:
			response.Rejected = append(response.Rejected, rejected(item, "date_conflict", "strong publication-date metadata disagrees", page.PublishedAt))
			continue
		case !parsed || page.DateConfidence == "none":
			response.Rejected = append(response.Rejected, rejected(item, "missing_verified_date", "no parseable machine-readable publication date", page.PublishedAt))
			continue
		case page.DateConfidence == "low":
			response.Rejected = append(response.Rejected, rejected(item, "insufficient_date_confidence", "time[datetime] fallback alone does not prove publication time", page.PublishedAt))
			continue
		case publishedAt.Before(cutoff) || publishedAt.After(retrievedAt):
			response.Rejected = append(response.Rejected, rejected(item, "outside_freshness_window", "page publication date is outside cutoff and retrieval time", page.PublishedAt))
			continue
		}

		clusterID := item.TitleClusterID
		if clusterID == "" {
			clusterID = item.URL
		}
		story := storiesByCluster[clusterID]
		if story == nil {
			story = &Story{Title: item.Title, TitleClusterID: clusterID, PublishedAt: page.PublishedAt, Sources: make([]StorySource, 0, 1)}
			storiesByCluster[clusterID] = story
			storyOrder = append(storyOrder, clusterID)
		} else if storyPublished, ok := dateutil.Parse(story.PublishedAt); !ok || publishedAt.After(storyPublished) {
			story.PublishedAt = page.PublishedAt
		}
		sourceIndex := len(story.Sources)
		excerpt := ""
		if sourceIndex < limits.excerptSourcesPerStory {
			excerpt, _ = textutil.TruncateBoundary(page.Content, limits.excerptCharacters)
		}
		authorsCount := len(page.Authors)
		authors := limitedCopy(page.Authors, limits.authorsPerSource)
		dateEvidenceCount := len(page.DateEvidence)
		dateEvidence := limitedCopy(page.DateEvidence, limits.dateEvidencePerSource)
		story.Sources = append(story.Sources, StorySource{
			URL: item.URL, CanonicalURL: page.CanonicalURL, SourceDomain: page.SourceDomain,
			RegistrableDomain: registrableDomain(page.SourceDomain), Publisher: page.Publisher,
			Authors: authors, AuthorsCount: authorsCount, AuthorsTruncated: authorsCount > len(authors),
			PublishedAt: page.PublishedAt, ModifiedAt: page.ModifiedAt,
			DateEvidence: dateEvidence, DateEvidenceCount: dateEvidenceCount,
			DateEvidenceTruncated: dateEvidenceCount > len(dateEvidence),
			DateConfidence:        page.DateConfidence, DateConflict: page.DateConflict, Excerpt: excerpt,
		})
	}

	for _, clusterID := range storyOrder {
		story := storiesByCluster[clusterID]
		story.DistinctDomains = countStoryDomains(story.Sources)
		updateStoryCounts(story)
		if story.DistinctDomains < params.MinDistinctDomains {
			for _, source := range story.Sources {
				response.Rejected = append(response.Rejected, RejectedCandidate{
					Title: story.Title, URL: source.URL, SourceDomain: source.SourceDomain,
					PublishedAt: source.PublishedAt, Reason: "insufficient_distinct_domains",
					Detail: fmt.Sprintf("story has %d distinct domains; %d required", story.DistinctDomains, params.MinDistinctDomains),
				})
			}
			continue
		}
		if len(response.Stories) >= params.MaxStories {
			response.Truncated = true
			for _, source := range story.Sources {
				response.Rejected = append(response.Rejected, RejectedCandidate{
					Title: story.Title, URL: source.URL, SourceDomain: source.SourceDomain,
					PublishedAt: source.PublishedAt, Reason: "story_limit", Detail: "max_stories reached",
				})
			}
			continue
		}
		response.Stories = append(response.Stories, *story)
	}
	response.DistinctDomains = countResponseDomains(response.Stories)
	response.Empty = len(response.Stories) == 0
	response.ElapsedMS = time.Since(started).Milliseconds()
	finalizeRejected(&response, limits.rejectedExamples)
	if err := applyOutputBudget(&response, limits.targetBytes); err != nil {
		return Response{}, err
	}
	return response, nil
}

func (r *Runner) fetchCandidates(ctx context.Context, candidates []candidate) ([]fetchOutcome, error) {
	outcomes := make([]fetchOutcome, len(candidates))
	if len(candidates) == 0 {
		return outcomes, nil
	}
	jobs := make(chan int)
	var workers sync.WaitGroup
	workerCount := min(maxConcurrency, len(candidates))
	workers.Add(workerCount)
	for range workerCount {
		go func() {
			defer workers.Done()
			for index := range jobs {
				if ctx.Err() != nil {
					outcomes[index].err = ctx.Err()
					continue
				}
				outcomes[index].result, outcomes[index].err = r.fetcher.Fetch(ctx, candidates[index].result.URL, candidates[index].result.Title)
			}
		}()
	}
	for index := range candidates {
		select {
		case jobs <- index:
		case <-ctx.Done():
			close(jobs)
			workers.Wait()
			return nil, ctx.Err()
		}
	}
	close(jobs)
	workers.Wait()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return outcomes, nil
}

func validateParams(params Params) (Params, error) {
	if len(params.Queries) < 1 || len(params.Queries) > 4 {
		return Params{}, errors.New("queries must contain between 1 and 4 items")
	}
	for index := range params.Queries {
		params.Queries[index] = strings.TrimSpace(params.Queries[index])
		if params.Queries[index] == "" {
			return Params{}, errors.New("queries must not contain empty items")
		}
	}
	if params.FreshnessHours < 1 || params.FreshnessHours > 168 {
		return Params{}, errors.New("freshness_hours must be between 1 and 168")
	}
	if params.MaxStories == 0 {
		params.MaxStories = defaultMaxStories
	}
	if params.MaxStories < 1 || params.MaxStories > 10 {
		return Params{}, errors.New("max_stories must be between 1 and 10")
	}
	if params.MaxCandidates == 0 {
		params.MaxCandidates = min(12, params.MaxStories*2)
	}
	if params.MaxCandidates < 1 || params.MaxCandidates > maxCandidatesLimit {
		return Params{}, fmt.Errorf("max_candidates must be between 1 and %d", maxCandidatesLimit)
	}
	if params.MinDistinctDomains == 0 {
		params.MinDistinctDomains = 1
	}
	if params.MinDistinctDomains < 1 || params.MinDistinctDomains > 3 {
		return Params{}, errors.New("min_distinct_domains must be between 1 and 3")
	}
	if params.ResponseMode == "" {
		params.ResponseMode = "standard"
	}
	if params.ResponseMode != "standard" && params.ResponseMode != "deep" {
		return Params{}, errors.New("response_mode must be standard or deep")
	}
	return params, nil
}

func limitsForMode(mode string) modeLimits {
	if mode == "deep" {
		return modeLimits{
			excerptCharacters: 1_200, excerptSourcesPerStory: 3,
			authorsPerSource: 10, dateEvidencePerSource: 10,
			rejectedExamples: 16, targetBytes: hardOutputLimitBytes,
		}
	}
	return modeLimits{
		excerptCharacters: 700, excerptSourcesPerStory: 2,
		authorsPerSource: 4, dateEvidencePerSource: 4,
		rejectedExamples: 8, targetBytes: standardTargetBytes,
	}
}

func roundRobinCandidates(perQuery [][]search.Result, limit int) ([]candidate, bool) {
	result := make([]candidate, 0, limit)
	seen := make(map[string]bool)
	truncated := false
	for round := 0; ; round++ {
		found := false
		for _, queryResults := range perQuery {
			if round >= len(queryResults) {
				continue
			}
			found = true
			item := queryResults[round]
			key := normalizedURLKey(item.URL)
			if key == "" {
				key = item.URL
			}
			if seen[key] {
				continue
			}
			seen[key] = true
			if len(result) >= limit {
				truncated = true
				continue
			}
			result = append(result, candidate{result: item})
		}
		if !found {
			break
		}
	}
	return result, truncated
}

func normalizedURLKey(raw string) string {
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || parsed.Hostname() == "" {
		return ""
	}
	parsed.Scheme = strings.ToLower(parsed.Scheme)
	parsed.Host = strings.ToLower(parsed.Host)
	parsed.Fragment = ""
	query := parsed.Query()
	for key := range query {
		lower := strings.ToLower(key)
		if strings.HasPrefix(lower, "utm_") || researchTrackingParameters[lower] {
			query.Del(key)
		}
	}
	parsed.RawQuery = query.Encode()
	if parsed.Path != "/" {
		parsed.Path = strings.TrimRight(parsed.Path, "/")
	}
	return parsed.String()
}

var researchTrackingParameters = map[string]bool{
	"fbclid": true, "gclid": true, "dclid": true, "msclkid": true,
	"mc_cid": true, "mc_eid": true, "igshid": true, "yclid": true,
}

func limitedCopy[T any](values []T, limit int) []T {
	if len(values) > limit {
		values = values[:limit]
	}
	return append([]T(nil), values...)
}

func updateStoryCounts(story *Story) {
	story.SourceCount = len(story.Sources)
	story.ExcerptSourceCount = 0
	for _, source := range story.Sources {
		if source.Excerpt != "" {
			story.ExcerptSourceCount++
		}
	}
	story.ExcerptsTruncated = story.ExcerptSourceCount < story.SourceCount
}

func finalizeRejected(response *Response, exampleLimit int) {
	response.RejectedCount = len(response.Rejected)
	for _, item := range response.Rejected {
		response.RejectedCounts[item.Reason]++
	}
	if len(response.Rejected) > exampleLimit {
		response.Rejected = append([]RejectedCandidate(nil), response.Rejected[:exampleLimit]...)
		response.RejectedTruncated = true
		response.Truncated = true
	}
	for storyIndex := range response.Stories {
		story := &response.Stories[storyIndex]
		updateStoryCounts(story)
		if story.ExcerptsTruncated {
			response.Truncated = true
		}
		for _, source := range story.Sources {
			if source.AuthorsTruncated || source.DateEvidenceTruncated {
				response.Truncated = true
			}
		}
	}
}

func applyOutputBudget(response *Response, target int) error {
	for {
		response.OutputBudget.Truncated = response.Truncated
		size, err := responseJSONSize(response)
		if err != nil {
			return fmt.Errorf("encode research response: %w", err)
		}
		if size <= target || !trimOptionalOutput(response) {
			break
		}
		response.Truncated = true
	}
	response.OutputBudget.Truncated = response.Truncated
	size, err := responseJSONSize(response)
	if err != nil {
		return fmt.Errorf("encode research response: %w", err)
	}
	if size > hardOutputLimitBytes {
		return fmt.Errorf("research response requires %d bytes after optional data was removed; hard limit is %d bytes", size, hardOutputLimitBytes)
	}
	return nil
}

func responseJSONSize(response *Response) (int, error) {
	for range 16 {
		encoded, err := json.Marshal(response)
		if err != nil {
			return 0, err
		}
		size := len(encoded)
		if response.OutputBudget.ReturnedBytes == size {
			return size, nil
		}
		response.OutputBudget.ReturnedBytes = size
	}
	return 0, errors.New("research output size did not stabilize")
}

func trimOptionalOutput(response *Response) bool {
	if len(response.Rejected) > 0 {
		response.Rejected = response.Rejected[:len(response.Rejected)-1]
		response.RejectedTruncated = true
		return true
	}
	for storyIndex := len(response.Stories) - 1; storyIndex >= 0; storyIndex-- {
		story := &response.Stories[storyIndex]
		for sourceIndex := len(story.Sources) - 1; sourceIndex >= 0; sourceIndex-- {
			source := &story.Sources[sourceIndex]
			if len(source.DateEvidence) > 0 {
				source.DateEvidence = source.DateEvidence[:len(source.DateEvidence)-1]
				source.DateEvidenceTruncated = true
				return true
			}
		}
	}
	for storyIndex := len(response.Stories) - 1; storyIndex >= 0; storyIndex-- {
		story := &response.Stories[storyIndex]
		for sourceIndex := len(story.Sources) - 1; sourceIndex >= 0; sourceIndex-- {
			source := &story.Sources[sourceIndex]
			if len(source.Authors) > 0 {
				source.Authors = source.Authors[:len(source.Authors)-1]
				source.AuthorsTruncated = true
				return true
			}
		}
	}
	for storyIndex := len(response.Stories) - 1; storyIndex >= 0; storyIndex-- {
		story := &response.Stories[storyIndex]
		for sourceIndex := len(story.Sources) - 1; sourceIndex >= 0; sourceIndex-- {
			source := &story.Sources[sourceIndex]
			characters := textutil.RuneCount(source.Excerpt)
			if characters == 0 {
				continue
			}
			if characters <= 256 {
				source.Excerpt = ""
			} else {
				source.Excerpt, _ = textutil.TruncateBoundary(source.Excerpt, characters-256)
			}
			updateStoryCounts(story)
			return true
		}
	}
	return false
}

func approximateTimeRange(hours int) string {
	if hours <= 24 {
		return "day"
	}
	return "month"
}

func rejected(item search.Result, reason, detail, publishedAt string) RejectedCandidate {
	return RejectedCandidate{
		Title: item.Title, URL: item.URL, SourceDomain: item.SourceDomain,
		PublishedAt: publishedAt, Reason: reason, Detail: detail,
	}
}

func registrableDomain(host string) string {
	host = strings.ToLower(strings.TrimSuffix(strings.TrimSpace(host), "."))
	if host == "" {
		return ""
	}
	if net.ParseIP(host) != nil {
		return host
	}
	if domain, err := publicsuffix.EffectiveTLDPlusOne(host); err == nil {
		return domain
	}
	return host
}

func countStoryDomains(sources []StorySource) int {
	seen := make(map[string]bool)
	for _, source := range sources {
		domain := source.RegistrableDomain
		if domain == "" {
			domain = registrableDomain(source.SourceDomain)
		}
		if domain != "" {
			seen[domain] = true
		}
	}
	return len(seen)
}

func countResponseDomains(stories []Story) int {
	seen := make(map[string]bool)
	for _, story := range stories {
		for _, source := range story.Sources {
			if source.RegistrableDomain != "" {
				seen[source.RegistrableDomain] = true
			}
		}
	}
	return len(seen)
}
