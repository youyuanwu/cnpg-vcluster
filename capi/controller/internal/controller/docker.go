package controller

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type DockerContainer struct {
	ID               string
	Name             string
	Labels           map[string]string
	Networks         map[string]string
	NetworkAddresses map[string]string
	State            string
}

type DockerNetwork struct {
	ID      string
	Subnets []string
}

type DockerVolume struct {
	Name       string
	CreatedAt  string
	Mountpoint string
	Labels     map[string]string
}

type DockerExecResult struct {
	ExitCode int
	Output   string
}

type DockerClient interface {
	InspectContainer(context.Context, string) (DockerContainer, error)
	InspectNetwork(context.Context, string) (DockerNetwork, error)
	InspectVolume(context.Context, string) (*DockerVolume, error)
	CreateVolume(context.Context, string, map[string]string) (DockerVolume, error)
	RemoveVolume(context.Context, string) error
	ListWorkerContainers(context.Context, string) ([]DockerContainer, error)
	Exec(context.Context, string, []string) (DockerExecResult, error)
}

type socketDockerClient struct {
	http *http.Client
}

func NewDockerClient(socket string) DockerClient {
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		},
	}
	return &socketDockerClient{
		http: &http.Client{Transport: transport, Timeout: 2 * time.Minute},
	}
}

func (client *socketDockerClient) InspectContainer(ctx context.Context, id string) (DockerContainer, error) {
	var payload struct {
		ID     string `json:"Id"`
		Name   string `json:"Name"`
		Config struct {
			Labels map[string]string `json:"Labels"`
		} `json:"Config"`
		NetworkSettings struct {
			Networks map[string]struct {
				NetworkID string `json:"NetworkID"`
				IPAddress string `json:"IPAddress"`
			} `json:"Networks"`
		} `json:"NetworkSettings"`
		State struct {
			Status string `json:"Status"`
		} `json:"State"`
	}
	if err := client.json(ctx, http.MethodGet, "/containers/"+url.PathEscape(id)+"/json", nil, &payload); err != nil {
		return DockerContainer{}, err
	}
	networks := make(map[string]string, len(payload.NetworkSettings.Networks))
	addresses := make(map[string]string, len(payload.NetworkSettings.Networks))
	for name, network := range payload.NetworkSettings.Networks {
		networks[name] = network.NetworkID
		addresses[network.NetworkID] = network.IPAddress
	}
	return DockerContainer{
		ID:               payload.ID,
		Name:             strings.TrimPrefix(payload.Name, "/"),
		Labels:           payload.Config.Labels,
		Networks:         networks,
		NetworkAddresses: addresses,
		State:            payload.State.Status,
	}, nil
}

func (client *socketDockerClient) InspectNetwork(ctx context.Context, id string) (DockerNetwork, error) {
	var payload struct {
		ID   string `json:"Id"`
		IPAM struct {
			Config []struct {
				Subnet string `json:"Subnet"`
			} `json:"Config"`
		} `json:"IPAM"`
	}
	if err := client.json(ctx, http.MethodGet, "/networks/"+url.PathEscape(id), nil, &payload); err != nil {
		return DockerNetwork{}, err
	}
	result := DockerNetwork{ID: payload.ID}
	for _, item := range payload.IPAM.Config {
		if item.Subnet != "" {
			result.Subnets = append(result.Subnets, item.Subnet)
		}
	}
	return result, nil
}

func (client *socketDockerClient) InspectVolume(ctx context.Context, name string) (*DockerVolume, error) {
	var payload struct {
		Name       string            `json:"Name"`
		CreatedAt  string            `json:"CreatedAt"`
		Mountpoint string            `json:"Mountpoint"`
		Labels     map[string]string `json:"Labels"`
	}
	err := client.json(ctx, http.MethodGet, "/volumes/"+url.PathEscape(name), nil, &payload)
	if dockerStatus(err) == http.StatusNotFound {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &DockerVolume{
		Name:       payload.Name,
		CreatedAt:  payload.CreatedAt,
		Mountpoint: payload.Mountpoint,
		Labels:     payload.Labels,
	}, nil
}

func (client *socketDockerClient) CreateVolume(ctx context.Context, name string, labels map[string]string) (DockerVolume, error) {
	var payload DockerVolume
	if err := client.json(ctx, http.MethodPost, "/volumes/create", map[string]any{
		"Name":   name,
		"Labels": labels,
	}, &payload); err != nil {
		return DockerVolume{}, err
	}
	return payload, nil
}

func (client *socketDockerClient) RemoveVolume(ctx context.Context, name string) error {
	err := client.json(ctx, http.MethodDelete, "/volumes/"+url.PathEscape(name), nil, nil)
	if dockerStatus(err) == http.StatusNotFound {
		return nil
	}
	return err
}

func (client *socketDockerClient) ListWorkerContainers(ctx context.Context, tenant string) ([]DockerContainer, error) {
	filters, err := json.Marshal(map[string][]string{
		"label": {
			"io.x-k8s.kind.cluster=" + tenant,
			"io.x-k8s.kind.role=worker",
		},
	})
	if err != nil {
		return nil, err
	}
	var payload []struct {
		ID     string            `json:"Id"`
		Names  []string          `json:"Names"`
		Labels map[string]string `json:"Labels"`
		State  string            `json:"State"`
	}
	path := "/containers/json?all=true&filters=" + url.QueryEscape(string(filters))
	if err := client.json(ctx, http.MethodGet, path, nil, &payload); err != nil {
		return nil, err
	}
	result := make([]DockerContainer, 0, len(payload))
	for _, item := range payload {
		container, err := client.InspectContainer(ctx, item.ID)
		if err != nil {
			if dockerStatus(err) == http.StatusNotFound {
				continue
			}
			return nil, err
		}
		result = append(result, container)
	}
	return result, nil
}

func (client *socketDockerClient) Exec(ctx context.Context, container string, command []string) (DockerExecResult, error) {
	var created struct {
		ID string `json:"Id"`
	}
	if err := client.json(ctx, http.MethodPost, "/containers/"+url.PathEscape(container)+"/exec", map[string]any{
		"AttachStdout": true,
		"AttachStderr": true,
		"Cmd":          command,
	}, &created); err != nil {
		return DockerExecResult{}, err
	}
	body, status, err := client.request(ctx, http.MethodPost, "/exec/"+url.PathEscape(created.ID)+"/start", map[string]any{
		"Detach": false,
		"Tty":    false,
	})
	if err != nil {
		return DockerExecResult{}, err
	}
	if status < 200 || status >= 300 {
		return DockerExecResult{}, &dockerError{status: status, message: string(body)}
	}
	var inspected struct {
		ExitCode int `json:"ExitCode"`
	}
	if err := client.json(ctx, http.MethodGet, "/exec/"+url.PathEscape(created.ID)+"/json", nil, &inspected); err != nil {
		return DockerExecResult{}, err
	}
	return DockerExecResult{ExitCode: inspected.ExitCode, Output: decodeDockerStream(body)}, nil
}

func (client *socketDockerClient) json(ctx context.Context, method, path string, input, output any) error {
	body, status, err := client.request(ctx, method, path, input)
	if err != nil {
		return err
	}
	if status < 200 || status >= 300 {
		return &dockerError{status: status, message: string(body)}
	}
	if output == nil || len(body) == 0 {
		return nil
	}
	if err := json.Unmarshal(body, output); err != nil {
		return fmt.Errorf("decode Docker response: %w", err)
	}
	return nil
}

func (client *socketDockerClient) request(ctx context.Context, method, path string, input any) ([]byte, int, error) {
	var body io.Reader
	if input != nil {
		encoded, err := json.Marshal(input)
		if err != nil {
			return nil, 0, err
		}
		body = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, "http://docker"+path, body)
	if err != nil {
		return nil, 0, err
	}
	if input != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := client.http.Do(request)
	if err != nil {
		return nil, 0, fmt.Errorf("Docker API request failed: %w", err)
	}
	defer response.Body.Close()
	data, err := io.ReadAll(io.LimitReader(response.Body, 8<<20))
	if err != nil {
		return nil, 0, fmt.Errorf("read Docker API response: %w", err)
	}
	return data, response.StatusCode, nil
}

type dockerError struct {
	status  int
	message string
}

func (err *dockerError) Error() string {
	return fmt.Sprintf("Docker API returned %d: %s", err.status, strings.TrimSpace(err.message))
}

func dockerStatus(err error) int {
	if value, ok := err.(*dockerError); ok {
		return value.status
	}
	return 0
}

func decodeDockerStream(data []byte) string {
	var output bytes.Buffer
	for len(data) >= 8 {
		size := int(binary.BigEndian.Uint32(data[4:8]))
		if size < 0 || len(data) < 8+size {
			return string(data)
		}
		output.Write(data[8 : 8+size])
		data = data[8+size:]
	}
	if output.Len() == 0 {
		return string(data)
	}
	return output.String()
}
