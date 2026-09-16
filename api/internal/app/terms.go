package app

import (
	"strings"
	"unicode"
)

// Chinese bigrams + Latin/number words, shared with the indexer's versioned tokenizer.
func TextTerms(input string) string {
	seen := map[string]bool{}
	terms := []string{}
	word := []rune{}
	han := []rune{}
	add := func(value string) {
		if value != "" && !seen[value] {
			seen[value] = true
			terms = append(terms, value)
		}
	}
	flush := func() {
		if len(word) > 0 {
			add(string(word))
			word = nil
		}
		if len(han) == 1 {
			add(string(han))
		}
		for i := 0; i+1 < len(han); i++ {
			add(string(han[i : i+2]))
		}
		han = nil
	}
	for _, r := range strings.ToLower(input) {
		if unicode.Is(unicode.Han, r) {
			if len(word) > 0 {
				add(string(word))
				word = nil
			}
			han = append(han, r)
		} else if unicode.IsLetter(r) || unicode.IsDigit(r) {
			if len(han) > 0 {
				flush()
			}
			word = append(word, r)
		} else {
			flush()
		}
	}
	flush()
	if len(terms) > 64 {
		terms = terms[:64]
	}
	return strings.Join(terms, " | ")
}
