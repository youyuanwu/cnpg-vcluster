package controller

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func TestListWorkerContainersIgnoresContainerRemovedAfterList(t *testing.T) {
	client := &socketDockerClient{
		http: &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
			status := http.StatusOK
			body := `[{"Id":"removed","Names":["/worker"],"Labels":{},"State":"running"}]`
			if strings.Contains(request.URL.Path, "/containers/removed/json") {
				status = http.StatusNotFound
				body = `{"message":"No such container"}`
			}
			return &http.Response{
				StatusCode: status,
				Body:       io.NopCloser(strings.NewReader(body)),
				Header:     make(http.Header),
			}, nil
		})},
	}
	containers, err := client.ListWorkerContainers(context.Background(), "tenant-a")
	if err != nil {
		t.Fatal(err)
	}
	if len(containers) != 0 {
		t.Fatalf("removed container remained in inventory: %#v", containers)
	}
}
