package sanitize

import (
	"encoding/json"
	"errors"

	"github.com/go-logr/logr"
)

type sanitizingSink struct {
	delegate logr.LogSink
}

func Logger(base logr.Logger) logr.Logger {
	if base.GetSink() == nil {
		return base
	}
	return logr.New(&sanitizingSink{delegate: base.GetSink()})
}

func (sink *sanitizingSink) Init(info logr.RuntimeInfo) {
	sink.delegate.Init(info)
}

func (sink *sanitizingSink) Enabled(level int) bool {
	return sink.delegate.Enabled(level)
}

func (sink *sanitizingSink) Info(level int, message string, keysAndValues ...any) {
	sink.delegate.Info(level, Text(message), sanitizePairs(keysAndValues)...)
}

func (sink *sanitizingSink) Error(err error, message string, keysAndValues ...any) {
	var sanitized error
	if err != nil {
		sanitized = errors.New(Text(err.Error()))
	}
	sink.delegate.Error(sanitized, Text(message), sanitizePairs(keysAndValues)...)
}

func (sink *sanitizingSink) WithValues(keysAndValues ...any) logr.LogSink {
	return &sanitizingSink{
		delegate: sink.delegate.WithValues(sanitizePairs(keysAndValues)...),
	}
}

func (sink *sanitizingSink) WithName(name string) logr.LogSink {
	return &sanitizingSink{delegate: sink.delegate.WithName(Text(name))}
}

func sanitizePairs(values []any) []any {
	sanitized := make([]any, len(values))
	copy(sanitized, values)
	for index := 0; index < len(sanitized); index += 2 {
		key, ok := sanitized[index].(string)
		if !ok {
			sanitized[index] = Text(toString(sanitized[index]))
			continue
		}
		sanitized[index] = Text(key)
		if index+1 >= len(sanitized) {
			continue
		}
		if sensitiveKey.MatchString(key) {
			sanitized[index+1] = Redacted
		} else {
			sanitized[index+1] = Value(sanitized[index+1])
		}
	}
	return sanitized
}

func toString(value any) string {
	if value == nil {
		return ""
	}
	if text, ok := value.(string); ok {
		return text
	}
	data, err := json.Marshal(value)
	if err != nil {
		return Redacted
	}
	return string(data)
}
