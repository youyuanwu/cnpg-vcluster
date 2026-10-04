mod creation_support;
mod support;

#[path = "controller/azure_reconcile.rs"]
mod azure_reconcile;
#[path = "controller/builders.rs"]
mod builders;
#[path = "controller/builders_bootstrap.rs"]
mod builders_bootstrap;
#[path = "controller/builders_ownership.rs"]
mod builders_ownership;
#[path = "controller/creation_controller.rs"]
mod creation_controller;
#[path = "controller/creation_objects.rs"]
mod creation_objects;
#[path = "controller/readiness_conditions.rs"]
mod readiness_conditions;
#[path = "controller/readiness_workers.rs"]
mod readiness_workers;
