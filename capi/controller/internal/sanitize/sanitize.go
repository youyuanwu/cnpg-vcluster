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
	authValue    = regexp.MustCompile(`(?im)(authorization\s*[:=]\s*)[^\r\n]+`)
	assignment   = regexp.MustCompile(`(?i)\b(password|token|client[_-]?secret|subscription[_-]?id)\s*[:=]\s*([^\s,;]+)`)
	sensitiveLine = regexp.MustCompile(`(?im)^([ \t-]*(?:client-key-data|client-certificate-data|certificate-authority-data|password|token|client-secret|subscription-id|authorization)[ \t]*:[ \t]*).*$`)
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
	if strings.HasPrefix(trimmed, `"`) {
		var decoded string
		if json.Unmarshal([]byte(trimmed), &decoded) == nil {
			encoded, err := json.Marshal(sanitizeText(decoded, depth+1))
			if err == nil {
				return string(encoded)
			}
		}
	}
	if strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "[") {
		var decoded any
		if json.Unmarshal([]byte(trimmed), &decoded) == nil {
			encoded, err := json.Marshal(sanitizeValue(decoded, depth+1))
			if err == nil {
				return string(encoded)
			}
		}
	}
	if start := strings.IndexAny(value, "[{"); start >= 0 {
		for end := len(value); end > start; end-- {
			var decoded any
			if json.Unmarshal([]byte(value[start:end]), &decoded) != nil {
				continue
			}
			encoded, err := json.Marshal(sanitizeValue(decoded, depth+1))
			if err == nil {
				value = value[:start] + string(encoded) + sanitizeText(value[end:], depth+1)
			}
			break
		}
	}
	value = privateKey.ReplaceAllString(value, Redacted)
	value = authValue.ReplaceAllString(value, `${1}`+Redacted)
	value = assignment.ReplaceAllString(value, `${1}=`+Redacted)
	value = sensitiveLine.ReplaceAllString(value, `${1}`+Redacted)
	return value
}
