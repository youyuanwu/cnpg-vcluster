use std::path::PathBuf;

use tenant_controller::{
    api::tenant_crd,
    management::{AZURE_MANAGEMENT_RESOURCES, MANAGEMENT_RESOURCES},
    permissions::{azure_controller_role, controller_role},
};

type GenerateResult<T> = Result<T, Box<dyn std::error::Error>>;
type GeneratedFiles = [(&'static str, Vec<u8>); 5];

#[rustfmt::skip]
fn render<T: serde::Serialize>(value: &T) -> GenerateResult<Vec<u8>> { Ok(format!("---\n{}", serde_yaml::to_string(value)?).into_bytes()) }

fn generated_files() -> GenerateResult<GeneratedFiles> {
    if MANAGEMENT_RESOURCES
        .iter()
        .chain(AZURE_MANAGEMENT_RESOURCES)
        .any(|resource| !resource.valid_inventory_contract())
    {
        return Err("invalid management resource inventory contract".into());
    }
    let mut resources = serde_json::to_vec_pretty(MANAGEMENT_RESOURCES)?;
    resources.push(b'\n');
    let mut azure_resources = serde_json::to_vec_pretty(AZURE_MANAGEMENT_RESOURCES)?;
    azure_resources.push(b'\n');
    Ok([
        (
            "crd/bases/tenancy.cnpg-vcluster.io_tenants.yaml",
            render(&tenant_crd())?,
        ),
        ("rbac/role.yaml", render(&controller_role())?),
        ("rbac/role-azure.yaml", render(&azure_controller_role())?),
        ("management-resources.json", resources),
        ("azure-management-resources.json", azure_resources),
    ])
}

fn run(args: &[String]) -> GenerateResult<()> {
    let mut output_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("config");
    let mut check = false;
    let mut output_specified = false;
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            "--output-dir" if !output_specified => {
                let value = rest.next().ok_or("--output-dir requires a directory")?;
                if value.is_empty() || value.starts_with("--") {
                    return Err("--output-dir requires a directory".into());
                }
                output_dir = PathBuf::from(value);
                output_specified = true;
            }
            "--check" if !check => check = true,
            _ => return Err(format!("unknown or repeated generator argument: {arg}").into()),
        }
    }
    for (relative, contents) in generated_files()? {
        let path = output_dir.join(relative);
        if check {
            let existing = std::fs::read(&path)
                .map_err(|error| format!("cannot verify {}: {error}", path.display()))?;
            if existing != contents {
                return Err(format!("generated fixture differs: {}", path.display()).into());
            }
        } else {
            std::fs::create_dir_all(path.parent().ok_or("invalid output path")?)?;
            std::fs::write(&path, contents)?;
        }
    }
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    run(&std::env::args().skip(1).collect::<Vec<_>>())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn artifacts_are_deterministic_and_authoritative_fixtures_match() {
        assert_eq!(generated_files().unwrap(), generated_files().unwrap());
        run(&["--check".into()]).unwrap();
    }

    #[test]
    fn output_modes_refuse_bad_inputs() {
        assert!(run(&["--unknown".into()]).is_err());
        assert!(run(&["--output-dir".into()]).is_err());
        assert!(run(&["--output-dir".into(), "--check".into()]).is_err());
        assert!(run(&["--output-dir".into(), "".into()]).is_err());
        assert!(
            run(&[
                "--output-dir".into(),
                "one".into(),
                "--output-dir".into(),
                "two".into(),
            ])
            .is_err()
        );
        assert!(run(&["--check".into(), "--check".into()]).is_err());
        assert!(
            run(&[
                "--output-dir".into(),
                "/nonexistent/tenant-controller-fixtures".into(),
                "--check".into()
            ])
            .is_err()
        );
    }

    #[test]
    fn caller_selected_output_directory_is_not_ignored() {
        let temp = std::env::temp_dir().join(format!(
            "tenant-contracts-{}-{}",
            std::process::id(),
            std::thread::current().name().unwrap_or("generator")
        ));
        let dest = temp.to_string_lossy().into_owned();
        run(&["--output-dir".into(), dest.clone()]).unwrap();
        run(&["--output-dir".into(), dest.clone(), "--check".into()]).unwrap();
        let path = std::path::Path::new(&dest).join("rbac/role.yaml");
        std::fs::write(&path, b"altered").unwrap();
        assert!(run(&["--output-dir".into(), dest, "--check".into()]).is_err());
        std::fs::remove_dir_all(temp).unwrap();
    }
}
