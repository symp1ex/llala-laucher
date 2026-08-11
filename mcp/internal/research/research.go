package research

import (
	"context"
	"errors"
	"fmt"
	"net"
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
	defaultMaxCandidates = 20
	maxCandidatesLimit   = 40
	maxConcurrency       = 4
	maxExcerptCharacters = 1_200
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
}

type Runner struct {
	searcher Searcher
	fetcher  Fetcher
	now      func() time.Time
}

type Response struct {
	Notice           string              `json:"notice"`
	RetrievedAt      string              `json:"retrievedAt"`
	Cutoff           string              `json:"cutoff"`
	FreshnessHours   int                 `json:"freshnessHours"`
	ApproximateRange string              `json:"approximateTimeRange"`
	ElapsedMS        int64               `json:"elapsedMs"`
	QueryLog         []QueryLog          `json:"queryLog"`
	Errors           []ResearchError     `json:"errors"`
	Stories          []Story             `json:"stories"`
	Rejected         []RejectedCandidate `json:"rejected"`
	DistinctDomains  int                 `json:"distinctDomains"`
	CandidateCount   int                 `json:"candidateCount"`
	FetchedCount     int                 `json:"fetchedCount"`
	Empty            bool                `json:"empty"`
	Truncated        bool                `json:"truncated"`
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
	Title           string        `json:"title"`
	TitleClusterID  string        `json:"titleClusterId"`
	PublishedAt     string        `json:"publishedAt"`
	Sources         []StorySource `json:"sources"`
	DistinctDomains int           `json:"distinctDomains"`
}

type StorySource struct {
	URL               string               `json:"url"`
	CanonicalURL      string               `json:"canonicalUrl"`
	SourceDomain      string               `json:"sourceDomain"`
	RegistrableDomain string               `json:"registrableDomain"`
	Publisher         string               `json:"publisher"`
	Authors           []string             `json:"authors"`
	PublishedAt       string               `json:"publishedAt"`
	ModifiedAt        string               `json:"modifiedAt"`
	DateEvidence      []fetch.DateEvidence `json:"dateEvidence"`
	DateConfidence    string               `json:"dateConfidence"`
	Excerpt           string               `json:"excerpt"`
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

func New(searcher Searcher, fetcher Fetcher) *Runner {
	return &Runner{searcher: searcher, fetcher: fetcher, now: time.Now}
}

func (r *Runner) Research(ctx context.Context, params Params) (Response, error) {
	started := time.Now()
	params, err := validateParams(params)
	if err != nil {
		return Response{}, err
	}
	retrievedAt := r.now().UTC()
	cutoff := retrievedAt.Add(-time.Duration(params.FreshnessHours) * time.Hour)
	timeRange := approximateTimeRange(params.FreshnessHours)
	response := Response{
		Notice:      "Freshness is verified only from machine-readable page metadata. Snippets and index dates are not evidence; distinct domains are not necessarily independent sources.",
		RetrievedAt: dateutil.Format(retrievedAt), Cutoff: dateutil.Format(cutoff),
		FreshnessHours: params.FreshnessHours, ApproximateRange: timeRange,
		QueryLog: make([]QueryLog, 0, len(params.Queries)), Errors: make([]ResearchError, 0),
		Stories: make([]Story, 0), Rejected: make([]RejectedCandidate, 0),
	}

	allCandidates := make([]candidate, 0, params.MaxCandidates)
	seenURLs := make(map[string]bool)
	for _, query := range params.Queries {
		if err := ctx.Err(); err != nil {
			return Response{}, err
		}
		searched, searchErr := r.searcher.Search(ctx, search.Params{
			Query: query, MaxResults: min(20, params.MaxCandidates), Language: params.Language,
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
		for _, item := range searched.Results {
			if seenURLs[item.URL] {
				continue
			}
			if len(allCandidates) >= params.MaxCandidates {
				response.Truncated = true
				continue
			}
			seenURLs[item.URL] = true
			allCandidates = append(allCandidates, candidate{result: item})
		}
	}
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
		excerpt, _ := textutil.TruncateBoundary(page.Content, maxExcerptCharacters)
		story.Sources = append(story.Sources, StorySource{
			URL: item.URL, CanonicalURL: page.CanonicalURL, SourceDomain: page.SourceDomain,
			RegistrableDomain: registrableDomain(page.SourceDomain), Publisher: page.Publisher,
			Authors: page.Authors, PublishedAt: page.PublishedAt, ModifiedAt: page.ModifiedAt,
			DateEvidence: page.DateEvidence, DateConfidence: page.DateConfidence, Excerpt: excerpt,
		})
	}

	for _, clusterID := range storyOrder {
		story := storiesByCluster[clusterID]
		story.DistinctDomains = countStoryDomains(story.Sources)
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
		params.MaxCandidates = defaultMaxCandidates
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
	return params, nil
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
