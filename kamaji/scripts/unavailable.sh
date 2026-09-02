#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

operation="${1:-operation}"
phase="${2:-a later phase}"
die "${operation} is intentionally unavailable until ${phase}"
