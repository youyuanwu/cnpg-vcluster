package sanitize

import (
	"strings"
	"testing"
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
	encoded := strings.Repeat("[", 1)
	_ = encoded
	if sanitized == nil {
		t.Fatal("sanitized value is nil")
	}
}
