# Tickles Makefile
# Phase R — CI Gates & Operational Canaries

.PHONY: refresh-snapshots gate-local test-all

# Load .env if it exists
ifneq ("$(wildcard .env)","")
    include .env
    export $(shell sed 's/=.*//' .env)
endif

# Database connection defaults
PG_USER ?= admin
PG_HOST ?= 127.0.0.1
PG_PORT ?= 5432
DSN_SHARED ?= postgresql://$(PG_USER):$(PG_PASSWORD)@$(PG_HOST):$(PG_PORT)/tickles_shared
DSN_COMPANY ?= postgresql://$(PG_USER):$(PG_PASSWORD)@$(PG_HOST):$(PG_PORT)/tickles_jarvais

refresh-snapshots: ## Regenerate canonical snapshots from current master SQL
	@echo "Regenerating tickles_shared.snapshot.sql..."
	@dropdb -U $(PG_USER) -h $(PG_HOST) --if-exists _snapshot_shared
	@createdb -U $(PG_USER) -h $(PG_HOST) _snapshot_shared
	@sed 's/^\\c /-- \\c /; s/^\\connect /-- \\connect /' shared/migration/tickles_shared_pg.sql > _tmp_shared.sql
	@psql -U $(PG_USER) -h $(PG_HOST) -d _snapshot_shared -f _tmp_shared.sql > /dev/null
	@pg_dump -U $(PG_USER) -h $(PG_HOST) --schema-only --no-owner --no-privileges _snapshot_shared \
	  | sed '/^--/d; /^$$/d' > shared/scripts/snapshots/tickles_shared.snapshot.sql
	@dropdb -U $(PG_USER) -h $(PG_HOST) _snapshot_shared
	@rm _tmp_shared.sql
	@echo "Regenerating tickles_company.snapshot.sql..."
	@dropdb -U $(PG_USER) -h $(PG_HOST) --if-exists _snapshot_company
	@createdb -U $(PG_USER) -h $(PG_HOST) _snapshot_company
	@sed 's/^\\c /-- \\c /; s/^\\connect /-- \\connect /' shared/migration/tickles_company_pg.sql > _tmp_company.sql
	@psql -U $(PG_USER) -h $(PG_HOST) -d _snapshot_company -f _tmp_company.sql > /dev/null
	@pg_dump -U $(PG_USER) -h $(PG_HOST) --schema-only --no-owner --no-privileges _snapshot_company \
	  | sed '/^--/d; /^$$/d' > shared/scripts/snapshots/tickles_company.snapshot.sql
	@dropdb -U $(PG_USER) -h $(PG_HOST) _snapshot_company
	@rm _tmp_company.sql
	@echo "Snapshots refreshed."

gate-local: ## Run CI gates locally (schema-diff and writer-registry)
	@echo "Running schema-diff gate..."
	@python3 shared/scripts/schema_diff.py --dsn "$(DSN_SHARED)" --snapshot shared/scripts/snapshots/tickles_shared.snapshot.sql
	@python3 shared/scripts/schema_diff.py --dsn "$(DSN_COMPANY)" --snapshot shared/scripts/snapshots/tickles_company.snapshot.sql
	@echo "Running writer-registry gate..."
	@python3 shared/scripts/writer_registry_grep.py --dsn "$(DSN_SHARED)"
	@echo "Gates passed."

test-all: ## Run all Phase R tests
	@python3 -m pytest shared/tests/test_schema_diff.py
	@python3 -m pytest shared/tests/test_writer_registry_grep.py
	@python3 -m pytest shared/tests/test_master_sync_gate.py
	@python3 -m pytest shared/tests/test_cron_canary.py
