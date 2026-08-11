package textutil

import (
	"strings"
	"unicode"
	"unicode/utf8"
)

// EstimateTokens is a tokenizer-independent budget estimate, not an exact
// model token count. ASCII is estimated at four characters per token and
// non-ASCII text at two characters per token.
func EstimateTokens(value string) int {
	units := 0
	for _, r := range value {
		if r <= unicode.MaxASCII {
			units++
		} else {
			units += 2
		}
	}
	return (units + 3) / 4
}

func RuneCount(value string) int {
	return utf8.RuneCountInString(value)
}

// TruncateBoundary trims to a useful paragraph, sentence, or word boundary.
// The returned string never contains more than limit Unicode code points.
func TruncateBoundary(value string, limit int) (string, bool) {
	if limit <= 0 || RuneCount(value) <= limit {
		return value, false
	}
	runes := []rune(value)
	cut := limit
	minimum := limit * 3 / 5
	if boundary := lastParagraphBoundary(runes[:cut], minimum); boundary > 0 {
		cut = boundary
	} else if boundary := lastSentenceBoundary(runes[:cut], minimum); boundary > 0 {
		cut = boundary
	} else if boundary := lastWordBoundary(runes[:cut], minimum); boundary > 0 {
		cut = boundary
	}
	return strings.TrimSpace(string(runes[:cut])), true
}

func lastParagraphBoundary(value []rune, minimum int) int {
	text := string(value)
	index := strings.LastIndex(text, "\n\n")
	if index < 0 {
		return 0
	}
	return byteIndexToRuneIndex(text, index+2, minimum)
}

func lastSentenceBoundary(value []rune, minimum int) int {
	for index := len(value) - 1; index >= minimum; index-- {
		if strings.ContainsRune(".!?。！？", value[index]) &&
			(index+1 == len(value) || unicode.IsSpace(value[index+1])) {
			return index + 1
		}
	}
	return 0
}

func lastWordBoundary(value []rune, minimum int) int {
	for index := len(value) - 1; index >= minimum; index-- {
		if unicode.IsSpace(value[index]) {
			return index
		}
	}
	return 0
}

func byteIndexToRuneIndex(value string, byteIndex, minimum int) int {
	runeIndex := utf8.RuneCountInString(value[:byteIndex])
	if runeIndex < minimum {
		return 0
	}
	return runeIndex
}
