package resources

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"sort"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"
)

func DecodeManifest(content []byte) ([]*unstructured.Unstructured, error) {
	decoder := utilyaml.NewYAMLOrJSONDecoder(bytes.NewReader(content), 4096)
	objects := make([]*unstructured.Unstructured, 0)
	for {
		var raw map[string]any
		err := decoder.Decode(&raw)
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("decode manifest: %w", err)
		}
		if len(raw) == 0 {
			continue
		}
		if raw["kind"] == "List" {
			items, _, err := unstructured.NestedSlice(raw, "items")
			if err != nil {
				return nil, err
			}
			for _, item := range items {
				value, ok := item.(map[string]any)
				if !ok {
					return nil, fmt.Errorf("manifest List contains a non-object item")
				}
				objects = append(objects, &unstructured.Unstructured{Object: value})
			}
			continue
		}
		objects = append(objects, &unstructured.Unstructured{Object: raw})
	}
	return objects, nil
}

func EncodeDocuments(objects []*unstructured.Unstructured) (string, error) {
	var output bytes.Buffer
	for index, object := range objects {
		encoded, err := json.Marshal(object.Object)
		if err != nil {
			return "", err
		}
		if index != 0 {
			output.WriteString("\n---\n")
		}
		output.Write(encoded)
	}
	return output.String(), nil
}

func MarkTenantObject(context Context, object *unstructured.Unstructured, resource string) {
	labels, annotations := markers(context, resource)
	currentLabels := object.GetLabels()
	if currentLabels == nil {
		currentLabels = map[string]string{}
	}
	for key, value := range labels {
		currentLabels[key] = value
	}
	object.SetLabels(currentLabels)
	currentAnnotations := object.GetAnnotations()
	if currentAnnotations == nil {
		currentAnnotations = map[string]string{}
	}
	for key, value := range annotations {
		currentAnnotations[key] = value
	}
	object.SetAnnotations(currentAnnotations)
}

func SortObjects(objects []*unstructured.Unstructured) {
	sort.Slice(objects, func(left, right int) bool {
		a := objects[left]
		b := objects[right]
		return a.GetAPIVersion()+"/"+a.GetKind()+"/"+a.GetNamespace()+"/"+a.GetName() <
			b.GetAPIVersion()+"/"+b.GetKind()+"/"+b.GetNamespace()+"/"+b.GetName()
	})
}
