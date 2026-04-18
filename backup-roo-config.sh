#!/usr/bin/env bash
# backup-roo-config.sh
# Copies /opt/tickles/.roo to a timestamped backup folder.
#
# Usage: bash /opt/tickles/backup-roo-config.sh

set -euo pipefail

SOURCE_DIR="/opt/tickles/.roo"
BACKUP_PARENT_DIR="/opt/tickles/backups"

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "Error: Source directory $SOURCE_DIR does not exist." >&2
    exit 1
fi

BACKUP_DIR="${BACKUP_PARENT_DIR}/roo-$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"

cp -r "$SOURCE_DIR/" "$BACKUP_DIR"

echo "✓ Backup of .roo config complete: $BACKUP_DIR"
