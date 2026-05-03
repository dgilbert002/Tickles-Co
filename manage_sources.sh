#!/bin/bash
# Collector Catalogue Manager — Linux/macOS launcher
# Usage: ./manage_sources.sh [--tui]
#
# Default behaviour prints the web panel URL and exits.
# Pass --tui to launch the legacy interactive TUI over SSH.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

python3 manage_sources.py "$@"
