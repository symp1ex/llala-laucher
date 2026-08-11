package dateutil

import (
	"strings"
	"time"
)

var layouts = []string{
	time.RFC3339Nano,
	time.RFC3339,
	"2006-01-02",
	"2006-01-02 15:04:05Z07:00",
	"2006-01-02 15:04:05 -0700",
	time.RFC1123Z,
	time.RFC1123,
	time.RFC822Z,
	time.RFC822,
}

// Parse normalizes machine-readable dates to UTC. It deliberately does not
// attempt to infer dates from prose or incomplete natural-language values.
func Parse(value string) (time.Time, bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return time.Time{}, false
	}
	for _, layout := range layouts {
		parsed, err := time.Parse(layout, value)
		if err == nil {
			return parsed.UTC(), true
		}
	}
	return time.Time{}, false
}

func Format(value time.Time) string {
	return value.UTC().Format(time.RFC3339)
}
