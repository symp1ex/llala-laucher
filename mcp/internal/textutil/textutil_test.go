package textutil

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestTruncateBoundaryPreferenceAndUnicodeSafety(t *testing.T) {
	value := strings.Repeat("Первое предложение. Второе предложение.\n\n", 20)
	truncated, changed := TruncateBoundary(value, 180)
	if !changed || RuneCount(truncated) > 180 || !utf8.ValidString(truncated) ||
		!strings.HasSuffix(truncated, ".") {
		t.Fatalf("unexpected boundary truncation: %q", truncated)
	}
}

func TestApproximateTokenEstimateDocumentsUnicodeHeuristic(t *testing.T) {
	if got := EstimateTokens("abcdefgh"); got != 2 {
		t.Fatalf("ASCII estimate = %d, want 2", got)
	}
	if got := EstimateTokens("абвг"); got != 2 {
		t.Fatalf("non-ASCII estimate = %d, want 2", got)
	}
}
