#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Thin wrapper — delegates to scripts/gen_pages.py for HTML generation.
# All logic lives in the Python script. This shell wrapper exists only so the
# workflow can call `scripts/gen-pages.sh` (matching the existing step name).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/gen_pages.py" "$@"
