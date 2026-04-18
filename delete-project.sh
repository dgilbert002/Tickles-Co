#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <project-name> [--delete-folder]" >&2
    exit 1
fi

PROJECT_NAME="$1"
DELETE_FOLDER=false
if [[ "${2:-}" == "--delete-folder" ]]; then
    DELETE_FOLDER=true
fi

DB_NAME="tickles_${PROJECT_NAME}"
PROJECT_DIR="/opt/tickles/projects/${PROJECT_NAME}"

# Validate project name (alphanumeric + underscores only)
if [[ ! "$PROJECT_NAME" =~ ^[a-zA-Z0-9_]+$ ]]; then
    echo "Error: project name must contain only letters, numbers, and underscores." >&2
    exit 1
fi

# Safety guard: never touch tickles_shared
if [[ "$PROJECT_NAME" == "shared" ]]; then
    echo "Error: cannot delete the shared project/database." >&2
    exit 1
fi

# Drop the project database
echo "Dropping database '${DB_NAME}'..."
sudo mysql -e "DROP DATABASE IF EXISTS \`${DB_NAME}\`;"
echo "Database '${DB_NAME}' dropped."

# Optionally delete the folder
if [[ "$DELETE_FOLDER" == true ]]; then
    if [[ -d "$PROJECT_DIR" ]]; then
        echo "WARNING: This will permanently delete the directory $PROJECT_DIR and all its contents." >&2
        read -p "Are you sure you want to proceed? (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            rm -rf "$PROJECT_DIR"
            echo "Deleted project directory: $PROJECT_DIR"
        else
            echo "Deletion cancelled."
        fi
    else
        echo "Note: project directory '$PROJECT_DIR' does not exist, nothing to delete."
    fi
else
    echo "Project directory '$PROJECT_DIR' left intact (pass --delete-folder to remove it)."
fi

echo "Project '${PROJECT_NAME}' deleted."
