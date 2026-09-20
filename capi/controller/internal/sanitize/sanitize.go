package sanitize

import (
	"encoding/json"
	"regexp"
	"strings"
)

const Redacted = "REDACTED"

var (
	sensitiveKey = regexp.MustCompile(`(?i)(authorization|password|token|secret|private.?key|client.?key|client.?certificate|certificate.?authority|kubeconfig|bootstrap.?data|subscription.?id)`)
	privateKey   = regexp.MustCompile(`(?s)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----`)
	authValue    = regexp.MustCompile(`(?i)(authorization\s*[:=]\s*)([^\s,;]+)`)
	assignment   = regexp.MustCompile(`(?i)\b(password|token|client[_-]?secret|subscription[_-]?id)\s*[:=]\s*([^\s,;]+)`)
)

func Value(value any) any {
	return sanitizeValue(value, 0)
}

func Text(value string) string {
	return sanitizeText(value, 0)
}

func sanitizeValue(value any, depth int) any {
	if depth > 8 {
		return Redacted
	}
	switch typed := value.(type) {
	case map[string]any:
		result := make(map[string]any, len(typed))
		for key, item := range typed {
			if sensitiveKey.MatchString(key) {
				result[key] = Redacted
			} else {
				result[key] = sanitizeValue(item, depth+1)
			}
		}
		return result
	case []any:
		result := make([]any, len(typed))
		for index, item := range typed {
			result[index] = sanitizeValue(item, depth+1)
		}
		return result
	case string:
		return sanitizeText(typed, depth+1)
	default:
		return value
	}
}

func sanitizeText(value string, depth int) string {
	if depth > 8 {
		return Redacted
	}
	trimmed := strings.TrimSpace(value)
	if strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "[") {
		var decoded any
		if json.Unmarshal([]byte(trimmed), &decoded) == nil {
			encoded, err := json.Marshal(sanitizeValue(decoded, depth+1))
			if err == nil {
				return string(encoded)
			}
		}
	}
	value = privateKey.ReplaceAllString(value, Redacted)
	value = authValue.ReplaceAllString(value, `${1}`+Redacted)
	value = assignment.ReplaceAllString(value, `${1}=`+Redacted)
	return value
}
