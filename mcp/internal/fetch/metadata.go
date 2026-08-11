package fetch

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strings"

	"golang.org/x/net/html"
	"golang.org/x/net/html/charset"

	"llala-launcher/mcp/internal/dateutil"
)

type DateEvidence struct {
	Kind     string `json:"kind"`
	Source   string `json:"source"`
	Value    string `json:"value"`
	ParsedAt string `json:"parsedAt,omitempty"`
}

type pageMetadata struct {
	CanonicalURL   string
	Publisher      string
	Authors        []string
	PublishedAt    string
	ModifiedAt     string
	DateEvidence   []DateEvidence
	DateConfidence string
	DateConflict   bool
}

type dateCandidate struct {
	kind     string
	source   string
	value    string
	parsedAt string
	strength int
	order    int
}

type metadataCollector struct {
	base      *url.URL
	canonical string
	publisher string
	authors   []string
	dates     []dateCandidate
	order     int
}

func extractHTMLMetadata(body []byte, contentType string, base *url.URL) (pageMetadata, error) {
	reader, err := charset.NewReader(strings.NewReader(string(body)), contentType)
	if err != nil {
		return pageMetadata{}, err
	}
	document, err := html.Parse(reader)
	if err != nil {
		return pageMetadata{}, err
	}
	collector := metadataCollector{base: base}
	collector.visitHTML(document)
	return collector.result(), nil
}

func (c *metadataCollector) visitHTML(node *html.Node) {
	if node.Type == html.ElementNode {
		switch strings.ToLower(node.Data) {
		case "link":
			if c.canonical == "" && relContains(attribute(node, "rel"), "canonical") {
				if parsed, err := c.base.Parse(strings.TrimSpace(attribute(node, "href"))); err == nil &&
					(parsed.Scheme == "http" || parsed.Scheme == "https") && parsed.Hostname() != "" {
					c.canonical = parsed.String()
				}
			}
		case "meta":
			c.addMeta(node)
		case "time":
			value := strings.TrimSpace(attribute(node, "datetime"))
			if value != "" {
				kind := "published"
				marker := strings.ToLower(attribute(node, "itemprop") + " " + attribute(node, "class"))
				if strings.Contains(marker, "modified") || strings.Contains(marker, "updated") {
					kind = "modified"
				}
				c.addDate(kind, "time[datetime]", value, 1)
			}
		case "script":
			if strings.EqualFold(strings.TrimSpace(attribute(node, "type")), "application/ld+json") {
				c.addJSONLD(rawNodeText(node))
			}
		}
	}
	for child := node.FirstChild; child != nil; child = child.NextSibling {
		c.visitHTML(child)
	}
}

func (c *metadataCollector) addMeta(node *html.Node) {
	property := strings.ToLower(strings.TrimSpace(attribute(node, "property")))
	name := strings.ToLower(strings.TrimSpace(attribute(node, "name")))
	content := strings.TrimSpace(attribute(node, "content"))
	if content == "" {
		return
	}
	switch property {
	case "article:published_time":
		c.addDate("published", "meta[property=article:published_time]", content, 3)
	case "article:modified_time":
		c.addDate("modified", "meta[property=article:modified_time]", content, 3)
	case "og:site_name":
		if c.publisher == "" {
			c.publisher = content
		}
	}
	switch name {
	case "author", "article:author", "byl", "dc.creator", "dcterms.creator", "parsely-author":
		c.addAuthors(content)
	case "date", "pubdate", "publishdate", "publish_date", "datepublished", "dc.date", "dcterms.date", "parsely-pub-date":
		c.addDate("published", fmt.Sprintf("meta[name=%s]", name), content, 2)
	case "last-modified", "date.modified", "dcterms.modified", "modified", "datemodified":
		c.addDate("modified", fmt.Sprintf("meta[name=%s]", name), content, 2)
	}
}

func (c *metadataCollector) addJSONLD(value string) {
	var decoded any
	if err := json.Unmarshal([]byte(value), &decoded); err != nil {
		return
	}
	c.visitJSONLD(decoded)
}

func (c *metadataCollector) visitJSONLD(value any) {
	switch typed := value.(type) {
	case []any:
		for _, item := range typed {
			c.visitJSONLD(item)
		}
	case map[string]any:
		if graph, ok := typed["@graph"]; ok {
			c.visitJSONLD(graph)
		}
		articleType := jsonLDArticleType(typed["@type"])
		if articleType == "" {
			return
		}
		prefix := "jsonld." + articleType + "."
		if raw := scalarString(typed["datePublished"]); raw != "" {
			c.addDate("published", prefix+"datePublished", raw, 3)
		}
		if raw := scalarString(typed["dateModified"]); raw != "" {
			c.addDate("modified", prefix+"dateModified", raw, 3)
		}
		c.addAuthors(authorNames(typed["author"])...)
		if c.publisher == "" {
			c.publisher = firstName(typed["publisher"])
		}
	}
}

func (c *metadataCollector) addDate(kind, source, value string, strength int) {
	value = strings.TrimSpace(value)
	if value == "" {
		return
	}
	parsedAt := ""
	if parsed, ok := dateutil.Parse(value); ok {
		parsedAt = dateutil.Format(parsed)
	}
	c.dates = append(c.dates, dateCandidate{
		kind: kind, source: source, value: value, parsedAt: parsedAt, strength: strength, order: c.order,
	})
	c.order++
}

func (c *metadataCollector) addAuthors(values ...string) {
	seen := make(map[string]bool, len(c.authors))
	for _, author := range c.authors {
		seen[strings.ToLower(author)] = true
	}
	for _, value := range values {
		author := strings.TrimSpace(value)
		key := strings.ToLower(author)
		if author != "" && !seen[key] {
			seen[key] = true
			c.authors = append(c.authors, author)
		}
	}
}

func (c *metadataCollector) result() pageMetadata {
	result := pageMetadata{
		CanonicalURL: c.canonical, Publisher: c.publisher, Authors: c.authors, DateConfidence: "none",
	}
	for _, candidate := range c.dates {
		result.DateEvidence = append(result.DateEvidence, DateEvidence{
			Kind: candidate.kind, Source: candidate.source, Value: candidate.value, ParsedAt: candidate.parsedAt,
		})
	}
	result.PublishedAt, result.DateConfidence, result.DateConflict = selectPublishedDate(c.dates)
	result.ModifiedAt = selectDate(c.dates, "modified")
	return result
}

// Confidence is deterministic: JSON-LD and article:* metadata are high,
// conventional named meta fields are medium, and time[datetime] is low.
// Disagreement between parsed high-confidence publication values is surfaced.
func selectPublishedDate(candidates []dateCandidate) (string, string, bool) {
	selected := selectCandidate(candidates, "published")
	if selected == nil {
		return "", "none", false
	}
	strongValues := make(map[string]bool)
	for _, candidate := range candidates {
		if candidate.kind == "published" && candidate.strength >= 3 && candidate.parsedAt != "" {
			strongValues[candidate.parsedAt] = true
		}
	}
	if len(strongValues) > 1 {
		return selected.parsedAt, "conflicting", true
	}
	confidence := "low"
	if selected.strength >= 3 {
		confidence = "high"
	} else if selected.strength == 2 {
		confidence = "medium"
	}
	return selected.parsedAt, confidence, false
}

func selectDate(candidates []dateCandidate, kind string) string {
	if selected := selectCandidate(candidates, kind); selected != nil {
		return selected.parsedAt
	}
	return ""
}

func selectCandidate(candidates []dateCandidate, kind string) *dateCandidate {
	var selected *dateCandidate
	for index := range candidates {
		candidate := &candidates[index]
		if candidate.kind != kind || candidate.parsedAt == "" {
			continue
		}
		if selected == nil || candidate.strength > selected.strength ||
			(candidate.strength == selected.strength && candidate.order < selected.order) {
			selected = candidate
		}
	}
	return selected
}

func jsonLDArticleType(value any) string {
	for _, candidate := range stringValues(value) {
		candidate = strings.TrimSpace(candidate)
		if slash := strings.LastIndexAny(candidate, "/#"); slash >= 0 {
			candidate = candidate[slash+1:]
		}
		switch strings.ToLower(candidate) {
		case "newsarticle":
			return "NewsArticle"
		case "article":
			return "Article"
		case "blogposting":
			return "BlogPosting"
		case "report":
			return "Report"
		}
	}
	return ""
}

func stringValues(value any) []string {
	switch typed := value.(type) {
	case string:
		return []string{typed}
	case []any:
		var result []string
		for _, item := range typed {
			result = append(result, stringValues(item)...)
		}
		return result
	default:
		return nil
	}
}

func scalarString(value any) string {
	values := stringValues(value)
	if len(values) == 0 {
		return ""
	}
	return values[0]
}

func authorNames(value any) []string {
	switch typed := value.(type) {
	case string:
		return []string{typed}
	case map[string]any:
		if name := scalarString(typed["name"]); name != "" {
			return []string{name}
		}
	case []any:
		var result []string
		for _, item := range typed {
			result = append(result, authorNames(item)...)
		}
		return result
	}
	return nil
}

func firstName(value any) string {
	names := authorNames(value)
	if len(names) > 0 {
		return strings.TrimSpace(names[0])
	}
	return ""
}

func relContains(value, token string) bool {
	for _, part := range strings.Fields(strings.ToLower(value)) {
		if part == token {
			return true
		}
	}
	return false
}

func rawNodeText(node *html.Node) string {
	var builder strings.Builder
	for child := node.FirstChild; child != nil; child = child.NextSibling {
		if child.Type == html.TextNode {
			builder.WriteString(child.Data)
		}
	}
	return builder.String()
}
