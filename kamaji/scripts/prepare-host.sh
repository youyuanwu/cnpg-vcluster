#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/host.sh"

require_exact_just
prepare_host_inotify
log "host prepared: inotify instances=$(read_inotify_value max_user_instances), watches=$(read_inotify_value max_user_watches); original values recorded in ignored mode-0600 runtime state"
