#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <company-name>" >&2
    exit 1
fi

COMPANY_NAME="$1"
SHARED_DB="tickles_shared"
COMPANY_DB="tickles_${COMPANY_NAME}"
PROJECT_DIR="/opt/tickles/projects/${COMPANY_NAME}"

# Validate company name (alphanumeric + underscores only)
if [[ ! "$COMPANY_NAME" =~ ^[a-zA-Z0-9_]+$ ]]; then
    echo "Error: company name must contain only letters, numbers, and underscores." >&2
    exit 1
fi

# Create shared database if it doesn't exist
if ! sudo mysql -e "USE \`${SHARED_DB}\`;" >/dev/null 2>&1; then
    echo "Creating shared database: ${SHARED_DB}"
    sudo mysql < "/opt/tickles/shared/migration/tickles_shared.sql"
else
    echo "Shared database ${SHARED_DB} already exists"
fi

# Create company database from template
if ! sudo mysql -e "USE \`${COMPANY_DB}\`;" >/dev/null 2>&1; then
    echo "Creating company database: ${COMPANY_DB}"
    sudo mysql -e "CREATE DATABASE \`${COMPANY_DB}\`;"
    SQL_TEMPLATE=$(<"/opt/tickles/shared/migration/tickles_company.sql")
    SQL_CONTENT=${SQL_TEMPLATE//COMPANY_NAME/$COMPANY_NAME}
    echo "$SQL_CONTENT" | sudo mysql "${COMPANY_DB}"
else
    echo "Warning: company database ${COMPANY_DB} already exists"
fi

# Create project directory structure
if [[ -d "$PROJECT_DIR" ]]; then
    echo "Warning: directory $PROJECT_DIR already exists"
else
    mkdir -p "${PROJECT_DIR}/config"
    mkdir -p "${PROJECT_DIR}/logs"
    mkdir -p "${PROJECT_DIR}/strategies"
    echo "Created project directory structure:"
    echo "  ${PROJECT_DIR}/"
    echo "  ├── config/"
    echo "  ├── logs/"
    echo "  └── strategies/"
fi

echo "Successfully created project for ${COMPANY_NAME}"
echo "  Shared database: ${SHARED_DB}"
echo "  Company database: ${COMPANY_DB}"
echo "  Project directory: ${PROJECT_DIR}"
