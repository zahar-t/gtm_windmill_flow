# GTM Engine — local dev / ops shortcuts.
.PHONY: help windmill windmill-down windmill-logs build dry-run run-once test migrate

help:   ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  %-14s %s\n", $$1, $$2}'

# --- Windmill: see + run the DAG ---
windmill:        ## bring up local Windmill at http://localhost:8000 (admin@windmill.dev / changeme)
	docker compose up -d
	@echo "Windmill → http://localhost:8000   login: admin@windmill.dev / changeme"

windmill-down:   ## stop Windmill (make windmill-down ARGS=-v also wipes the DB)
	docker compose down $(ARGS)

windmill-logs:   ## tail the Windmill server logs
	docker compose logs -f windmill_server

# --- Pipeline (containerized) ---
build:           ## build the pipeline runtime image
	docker build -t gtm-engine .

dry-run: build   ## run the full funnel keyless (synthetic leads, NO real sends)
	docker run --rm gtm-engine python -m scripts.run_pipeline --demo

run-once: build  ## run the real pipeline once (needs .env with real keys)
	docker run --rm --env-file .env gtm-engine python -m scripts.run_pipeline

# --- Tests / DB ---
test:            ## run the gate suite (Step 4 — installs dev deps first)
	pip install -r requirements.txt && pytest -q

migrate:         ## apply a migration: make migrate FILE=scripts/sql/migrations/001_node_state_and_dlq.sql
	psql "$$SUPABASE_DB_URL" -f $(FILE)
