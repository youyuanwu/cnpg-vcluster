package sanitize

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/go-logr/logr/funcr"
)

func TestValueRedactsNestedSecrets(t *testing.T) {
	value := map[string]any{
		"message": `{"authorization":"Bearer abc","nested":{"client_secret":"value"}}`,
		"token":   "abc",
	}
	sanitized := Value(value).(map[string]any)
	if sanitized["token"] != Redacted {
		t.Fatalf("token was not redacted: %#v", sanitized)
	}
	message := sanitized["message"].(string)
	if strings.Contains(message, "abc") || strings.Contains(message, "value") {
		t.Fatalf("nested secret leaked: %s", message)
	}
}

func TestTextRedactsPrivateKeyAndAssignments(t *testing.T) {
	input := "password=hunter2 Authorization:Bearer-token\n-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
	output := Text(input)
	for _, secret := range []string{"hunter2", "Bearer-token", "secret"} {
		if strings.Contains(output, secret) {
			t.Fatalf("secret %q leaked in %q", secret, output)
		}
	}
}

func TestDepthLimitFailsClosed(t *testing.T) {
	value := any("secret")
	for range 10 {
		value = []any{value}
	}
	sanitized := Value(value)
	encoded, err := json.Marshal(sanitized)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "secret") {
		t.Fatalf("depth-limited secret leaked: %s", encoded)
	}
}

func TestTextRedactsKubeconfigAndEmbeddedJSON(t *testing.T) {
	inputs := []string{
		"client-key-data: c2VjcmV0",
		`provider failed: {"authorization":"Bearer abc","nested":{"token":"value"}} trailing`,
		`[ERROR] provider: {"token":"prefixed"}`,
		`"{\"client_secret\":\"double\"}"`,
	}
	for _, input := range inputs {
		output := Text(input)
		for _, secret := range []string{"c2VjcmV0", "abc", "value", "prefixed", "double"} {
			if strings.Contains(output, secret) {
				t.Fatalf("secret %q leaked from %q as %q", secret, input, output)
			}
		}
	}
}

func TestLoggerSanitizesMessagesErrorsAndValues(t *testing.T) {
	var output strings.Builder
	base := funcr.New(
		func(prefix, args string) {
			output.WriteString(prefix)
			output.WriteString(args)
		},
		funcr.Options{},
	)
	logger := Logger(base)
	logger.Error(
		errors.New("Authorization: Bearer abc"),
		`failed: {"token":"nested"}`,
		"client_secret",
		"value",
		"typed",
		map[string]string{"token": "typed-secret"},
	)
	text := output.String()
	for _, secret := range []string{"abc", "nested", "value", "typed-secret"} {
		if strings.Contains(text, secret) {
			t.Fatalf("logger leaked %q in %q", secret, text)
		}
	}
}
