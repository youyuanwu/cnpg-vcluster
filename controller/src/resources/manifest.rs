use std::collections::BTreeMap;

use kube::core::DynamicObject;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{BuildError, Context};

pub fn decode_manifest(content: &[u8]) -> Result<Vec<DynamicObject>, BuildError> {
    let mut objects = Vec::new();
    for document in serde_yaml::Deserializer::from_slice(content) {
        let raw = Value::deserialize(document)?;
        if raw.is_null() || raw.as_object().is_some_and(|object| object.is_empty()) {
            continue;
        }
        if raw.get("kind").and_then(Value::as_str) == Some("List") {
            match raw.get("items") {
                Some(Value::Array(items)) => {
                    for item in items {
                        objects.push(decode_object(item.clone())?);
                    }
                }
                None | Some(Value::Null) => {}
                _ => {
                    return Err(BuildError::Manifest(
                        "manifest List items must be an array".into(),
                    ));
                }
            }
        } else {
            objects.push(decode_object(raw)?);
        }
    }
    Ok(objects)
}

fn decode_object(value: Value) -> Result<DynamicObject, BuildError> {
    let object: DynamicObject = serde_json::from_value(value)?;
    if object
        .types
        .as_ref()
        .is_none_or(|types| types.api_version.is_empty() || types.kind.is_empty())
        || object.metadata.name.as_deref().is_none_or(str::is_empty)
    {
        return Err(BuildError::Manifest(
            "manifest object requires apiVersion, kind, and metadata.name".into(),
        ));
    }
    Ok(object)
}

pub fn to_dynamic(object: &impl Serialize) -> Result<DynamicObject, BuildError> {
    Ok(serde_json::from_value(serde_json::to_value(object)?)?)
}

pub fn encode_documents(objects: &[DynamicObject]) -> Result<String, BuildError> {
    Ok(objects
        .iter()
        .map(serde_json::to_string)
        .collect::<Result<Vec<_>, _>>()?
        .join("\n---\n"))
}

pub fn mark_tenant_object(context: &Context<'_>, object: &mut DynamicObject, resource: &str) {
    object
        .metadata
        .labels
        .get_or_insert_default()
        .extend(context.identity().labels());
    object
        .metadata
        .annotations
        .get_or_insert_default()
        .extend(context.identity().annotations(resource));
}

pub fn sort_objects(objects: &mut [DynamicObject]) {
    objects.sort_by_cached_key(|object| {
        let (api, kind) = object.types.as_ref().map_or(("", ""), |types| {
            (types.api_version.as_str(), types.kind.as_str())
        });
        format!(
            "{api}/{kind}/{}/{}",
            object.metadata.namespace.as_deref().unwrap_or(""),
            object.metadata.name.as_deref().unwrap_or("")
        )
    });
}

pub(super) fn replace_strings(
    value: &mut Value,
    replacements: &BTreeMap<&str, &str>,
    counts: &mut BTreeMap<String, usize>,
) {
    match value {
        Value::String(text) => {
            if let Some(replacement) = replacements.get(text.as_str()) {
                *counts.entry(text.clone()).or_default() += 1;
                *text = (*replacement).into();
            }
        }
        Value::Array(values) => {
            for item in values {
                replace_strings(item, replacements, counts);
            }
        }
        Value::Object(values) => {
            for item in values.values_mut() {
                replace_strings(item, replacements, counts);
            }
        }
        _ => {}
    }
}

pub(super) fn replace_object_strings(
    object: &mut DynamicObject,
    replacements: &BTreeMap<&str, &str>,
    counts: &mut BTreeMap<String, usize>,
) -> Result<(), BuildError> {
    let mut value = serde_json::to_value(&*object)?;
    replace_strings(&mut value, replacements, counts);
    *object = serde_json::from_value(value)?;
    Ok(())
}
