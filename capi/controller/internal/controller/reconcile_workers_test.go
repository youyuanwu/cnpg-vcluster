package controller

import (
	"fmt"
	"slices"
	"testing"
)

func TestWorkerBootstrapCommandsPrepareEveryDatabaseDirectory(t *testing.T) {
	for _, offline := range []bool{false, true} {
		for _, count := range []int32{1, 3} {
			t.Run(fmt.Sprintf("offline=%t/count=%d", offline, count), func(t *testing.T) {
				foundation := testFoundation()
				foundation.OfflineEnforced = offline
				foundation.Registry = &FoundationRegistry{Address: "172.18.0.10", Port: 5000}
				commands, err := workerBootstrapCommands(foundation, count)
				if err != nil {
					t.Fatal(err)
				}
				withoutDatabases, err := workerBootstrapCommands(foundation, 0)
				if err != nil {
					t.Fatal(err)
				}
				if len(commands) != len(withoutDatabases)+int(count) {
					t.Fatalf("unexpected database command count: %d", len(commands)-len(withoutDatabases))
				}
				start := len(requiredWorkerImageKeys) * 5
				for ordinal := int32(1); ordinal <= count; ordinal++ {
					directory := fmt.Sprintf("'/var/lib/storage/volumes/cnpg/%d'", ordinal)
					expected := "mkdir -p " + directory + " && chown 26:26 " + directory + " && chmod 0700 " + directory
					if got := commands[start+int(ordinal)-1]; got != expected {
						t.Fatalf("unexpected preparation for ordinal %d: %s", ordinal, got)
					}
				}
				unchanged := append(slices.Clone(commands[:start]), commands[start+int(count):]...)
				if !slices.Equal(unchanged, withoutDatabases) {
					t.Fatal("storage preparation changed image or offline commands")
				}
				repeated, err := workerBootstrapCommands(foundation, count)
				if err != nil || !slices.Equal(commands, repeated) {
					t.Fatalf("bootstrap commands are not repeatable: %v", err)
				}
				slices.Reverse(foundation.Cache.ImageArchives)
				reordered, err := workerBootstrapCommands(foundation, count)
				if err != nil || !slices.Equal(commands, reordered) {
					t.Fatalf("bootstrap commands depend on archive ordering: %v", err)
				}
			})
		}
	}
}
