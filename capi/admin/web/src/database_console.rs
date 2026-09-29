use tenant_admin_shared::query::{DatabaseInstanceObservation, DatabaseQueryResult};

use crate::format::sort_database_instances;

pub const DEFAULT_DATABASE: &str = "postgres";
pub const DEFAULT_SQL: &str = "SELECT current_database(), current_user, pg_is_in_recovery();";

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum QueryResultPresentation {
    Rows {
        column_count: usize,
        row_count: usize,
    },
    Command {
        affected_rows: u64,
    },
}

pub fn selectable_database_instances(
    instances: &[DatabaseInstanceObservation],
) -> Vec<DatabaseInstanceObservation> {
    let mut options = instances
        .iter()
        .filter(|instance| !instance.name.trim().is_empty())
        .cloned()
        .collect::<Vec<_>>();
    sort_database_instances(&mut options);
    options
}

pub fn project_query_result(result: &DatabaseQueryResult) -> QueryResultPresentation {
    if result.columns.is_empty() {
        QueryResultPresentation::Command {
            affected_rows: result.affected_rows,
        }
    } else {
        QueryResultPresentation::Rows {
            column_count: result.columns.len(),
            row_count: result.rows.len(),
        }
    }
}

pub fn format_query_duration(duration_ms: u64) -> String {
    if duration_ms < 1_000 {
        format!("{duration_ms} ms")
    } else {
        format!("{}.{:03} s", duration_ms / 1_000, duration_ms % 1_000)
    }
}

#[cfg(test)]
mod tests {
    use tenant_admin_shared::query::{
        DatabaseInstanceObservation, DatabaseInstanceRole, DatabaseQueryResult,
    };

    use super::{
        QueryResultPresentation, format_query_duration, project_query_result,
        selectable_database_instances,
    };

    #[test]
    fn projects_row_and_command_results() {
        let rows = DatabaseQueryResult {
            columns: vec!["value".to_owned()],
            rows: vec![vec![None], vec![Some("ok".to_owned())]],
            affected_rows: 0,
            truncated: false,
        };
        assert_eq!(
            project_query_result(&rows),
            QueryResultPresentation::Rows {
                column_count: 1,
                row_count: 2,
            }
        );

        let command = DatabaseQueryResult {
            columns: Vec::new(),
            rows: Vec::new(),
            affected_rows: 4,
            truncated: false,
        };
        assert_eq!(
            project_query_result(&command),
            QueryResultPresentation::Command { affected_rows: 4 }
        );
    }

    #[test]
    fn filters_empty_names_and_sorts_primary_first() {
        let options = selectable_database_instances(&[
            instance("standby-b", DatabaseInstanceRole::Standby),
            instance("", DatabaseInstanceRole::Primary),
            instance("unknown-a", DatabaseInstanceRole::Unknown),
            instance("primary-a", DatabaseInstanceRole::Primary),
        ]);

        assert_eq!(
            options
                .iter()
                .map(|instance| instance.name.as_str())
                .collect::<Vec<_>>(),
            vec!["primary-a", "standby-b", "unknown-a"]
        );
    }

    #[test]
    fn formats_query_duration_without_native_time_apis() {
        assert_eq!(format_query_duration(999), "999 ms");
        assert_eq!(format_query_duration(1_234), "1.234 s");
    }

    fn instance(name: &str, role: DatabaseInstanceRole) -> DatabaseInstanceObservation {
        DatabaseInstanceObservation {
            name: name.to_owned(),
            role,
            status: None,
            timeline: None,
            node: None,
            zone: None,
        }
    }
}
