UV          ?= uv
DOCKER      ?= docker
HELM        ?= helm
KUBECONFORM ?= kubeconform
SQITCH      ?= sqitch
IMAGE_REPO  ?= ghcr.io/peinser/xarta/core
IMAGE_TAG   ?= dev
DEV_CERTIFICATE_DIR ?= .dev/runtime/certificates

.DEFAULT_GOAL := help
SHELL := /bin/bash
.PHONY: help install lock format format-check lint typecheck test security profiles-check verify \
	docker-build docker-validate helm-verify setup dev mcp services-check credentials-check certificates \
	db-create-archive db-deploy streams wallets archive-test telemetry-check scenario-bundle scenario-archive-representations scenario-generate-sign-archive \
	scenario-signature-xarta-dev-seal-v1 scenario-signature-pades-b-b-v1 scenario-signature-pades-b-t-v1 scenario-preview \
	scenario-search scenario-search-standalone scenario-search-burst scenario-search-burst-standalone scenario-wait-for scenario-flow-profile scenario-postal-local scenario-postal-local-printer scenario-x402-facilitator scenario-x402-paid-intake scenario-x402-base-sepolia scenario-all standalone debug station station-cups \
	station-mock job-expire-documents job-cleanup-generate job-cleanup-bundle job-replay-dead-letters

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "}; /^[a-zA-Z0-9_-]+:.*?## / {printf "  %-40s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install locked development dependencies
	$(UV) sync --frozen --all-groups

lock: ## Refresh the uv lock file
	$(UV) lock --upgrade

format: ## Format Python sources and tests
	$(UV) run --frozen black src/xarta tests apps/postal-station/src apps/postal-station/tests
	$(UV) run --frozen ruff check --fix src/xarta tests apps/postal-station/src apps/postal-station/tests

format-check: ## Check Python formatting
	$(UV) run --frozen black --check --diff src/xarta tests apps/postal-station/src apps/postal-station/tests

lint: format-check ## Run static lint checks
	$(UV) run --frozen ruff check src/xarta tests apps/postal-station/src apps/postal-station/tests

typecheck: ## Run static type checking
	$(UV) run --frozen mypy

test: ## Run the unit test suite
	$(UV) run --frozen pytest

security: ## Run source security checks
	$(UV) run --frozen bandit -q -lll -r src/xarta apps/postal-station/src

profiles-check: ## Validate immutable repository flow-profile versions
	$(UV) run --frozen python -m xarta.flow_profiles validate-lock examples/configuration/flow-profiles.json examples/configuration/flow-profile-lock.json

verify: lint typecheck test security profiles-check ## Run all local Python quality gates

docker-build: ## Build the production image
	$(DOCKER) build --target production -t $(IMAGE_REPO):$(IMAGE_TAG) -f docker/core/Dockerfile .

docker-validate: ## Build and run the container validation stage
	$(DOCKER) build --target validate -f docker/core/Dockerfile .

helm-verify: ## Lint, render, and validate all Helm charts
	$(HELM) lint k8s/helm/charts/core -f k8s/helm/ci/core-values.yaml
	$(HELM) lint k8s/helm/charts/db-tools -f k8s/helm/ci/db-tools-values.yaml
	bash k8s/helm/tests/preview-render-pool.sh
	bash k8s/helm/tests/archive-postgresql.sh
	bash k8s/helm/tests/mcp.sh
	bash k8s/helm/tests/peppol.sh
	bash k8s/helm/tests/template-engines.sh
	bash k8s/helm/tests/telemetry.sh
	@set -o pipefail; $(HELM) template xarta k8s/helm/charts/core -f k8s/helm/ci/core-values.yaml | $(KUBECONFORM) -strict -summary -ignore-missing-schemas
	@set -o pipefail; $(HELM) template xarta-db k8s/helm/charts/db-tools -f k8s/helm/ci/db-tools-values.yaml | $(KUBECONFORM) -strict -summary -ignore-missing-schemas

setup: install ## Set up Xarta in the running development container
	$(MAKE) services-check
	$(MAKE) certificates
	$(MAKE) db-deploy
	$(MAKE) streams
	$(MAKE) credentials-check

dev: setup ## Set up and run Xarta with hot reload
	$(MAKE) standalone

mcp: ## Run the local MCP service against make dev
	@set -a; [[ ! -f .env ]] || source .env; set +a; \
	port="$${MCP_PORT:-8001}"; \
	MCP_INTAKE_BASE_URL="$${MCP_INTAKE_BASE_URL:-http://127.0.0.1:8000/api/v1/intake}" \
	MCP_DOCUMENT_TYPE_BASE_URL="$${MCP_DOCUMENT_TYPE_BASE_URL:-http://127.0.0.1:8000/api/v1/document-type}" \
	MCP_ARCHIVE_BASE_URL="$${MCP_ARCHIVE_BASE_URL:-http://127.0.0.1:8000/api/v1/archive}" \
	MCP_REQUEST_TIMEOUT_SECONDS="$${MCP_REQUEST_TIMEOUT_SECONDS:-30}" \
	MCP_ARCHIVE_MAX_RESOURCE_BYTES="$${MCP_ARCHIVE_MAX_RESOURCE_BYTES:-4194304}" \
	MCP_ALLOWED_HOSTS="$${MCP_ALLOWED_HOSTS:-127.0.0.1:$$port,localhost:$$port}" \
	$(UV) run --frozen python -m xarta.bin.mcp --port "$$port"

services-check: ## Check the devcontainer dependency services
	@pg_isready --quiet --host postgres --username dev --dbname dev || { echo "PostgreSQL is unavailable" >&2; exit 1; }
	@nats --server "$${NATS_SERVERS:-nats://nats:4222}" server check connection >/dev/null || { echo "NATS is unavailable" >&2; exit 1; }
	@exec 3<>/dev/tcp/cache/6379; printf '*1\r\n$$4\r\nPING\r\n' >&3; IFS= read -r response <&3; [[ "$$response" == +PONG* ]] || { echo "Dragonfly is unavailable" >&2; exit 1; }
	@curl --fail --silent --show-error http://minio:9000/minio/health/live >/dev/null || { echo "MinIO is unavailable" >&2; exit 1; }
	@curl --fail --silent --show-error http://opensearch:9200 >/dev/null || { echo "OpenSearch is unavailable" >&2; exit 1; }
	@echo "Development services are available"

credentials-check: ## Report optional external-provider credential status
	@configured=0; missing=""; \
	for name in RESEND_CONTRACT_API_KEY RESEND_CONTRACT_WEBHOOK_SECRET RESEND_CONTRACT_ACCOUNT_ID RESEND_CONTRACT_FROM; do \
		if [[ -n "$${!name:-}" ]]; then configured=$$((configured + 1)); else missing="$$missing $$name"; fi; \
	done; \
	if [[ $$configured -eq 0 ]]; then \
		echo "Resend live contract credentials are not configured (optional)"; \
	elif [[ $$configured -eq 4 ]]; then \
		echo "Resend live contract credentials are configured"; \
	else \
		echo "Resend live contract credentials are incomplete; missing:$$missing" >&2; exit 1; \
	fi
	@echo "Doccle, Peppol, SFTP, and Resend settings in .dev/conf are placeholders for opt-in integration work"

certificates: ## Create or refresh local document-signing certificates
	@mkdir -p $(DEV_CERTIFICATE_DIR)
	@if openssl x509 -checkend 604800 -noout -in $(DEV_CERTIFICATE_DIR)/public.pem >/dev/null 2>&1 \
		&& openssl x509 -checkend 604800 -noout -in $(DEV_CERTIFICATE_DIR)/chain.pem >/dev/null 2>&1 \
		&& openssl x509 -checkend 604800 -noout -in $(DEV_CERTIFICATE_DIR)/root.pem >/dev/null 2>&1 \
		&& openssl x509 -checkend 604800 -noout -in $(DEV_CERTIFICATE_DIR)/tsa-public.pem >/dev/null 2>&1 \
		&& openssl pkey -check -noout -in $(DEV_CERTIFICATE_DIR)/private.pem >/dev/null 2>&1 \
		&& openssl pkey -check -noout -in $(DEV_CERTIFICATE_DIR)/tsa-private.pem >/dev/null 2>&1 \
		&& cmp --silent \
			<(openssl x509 -in $(DEV_CERTIFICATE_DIR)/public.pem -pubkey -noout 2>/dev/null) \
			<(openssl pkey -in $(DEV_CERTIFICATE_DIR)/private.pem -pubout 2>/dev/null) \
		&& cmp --silent \
			<(openssl x509 -in $(DEV_CERTIFICATE_DIR)/tsa-public.pem -pubkey -noout 2>/dev/null) \
			<(openssl pkey -in $(DEV_CERTIFICATE_DIR)/tsa-private.pem -pubout 2>/dev/null) \
		&& openssl verify -CAfile $(DEV_CERTIFICATE_DIR)/root.pem \
			-untrusted $(DEV_CERTIFICATE_DIR)/chain.pem \
			$(DEV_CERTIFICATE_DIR)/public.pem >/dev/null 2>&1 \
		&& openssl verify -purpose timestampsign \
			-CAfile $(DEV_CERTIFICATE_DIR)/root.pem \
			$(DEV_CERTIFICATE_DIR)/tsa-public.pem >/dev/null 2>&1; then \
		echo "Development signing certificate is valid"; \
	else \
		tmp_dir=$$(mktemp -d $(DEV_CERTIFICATE_DIR)/.generate.XXXXXX); \
		trap 'rm -rf "$$tmp_dir"' EXIT; \
		openssl req -x509 -newkey rsa:3072 -nodes -sha256 \
			-keyout "$$tmp_dir/root-key.pem" -out "$$tmp_dir/root.pem" \
			-days 3650 -subj '/CN=Xarta Development Root CA' \
			-addext 'basicConstraints=critical,CA:TRUE,pathlen:1' \
			-addext 'keyUsage=critical,keyCertSign,cRLSign' \
			-addext 'subjectKeyIdentifier=hash' >/dev/null 2>&1; \
		openssl req -new -newkey rsa:3072 -nodes -sha256 \
			-keyout "$$tmp_dir/intermediate-key.pem" \
			-out "$$tmp_dir/intermediate.csr" \
			-subj '/CN=Xarta Development Signing CA' >/dev/null 2>&1; \
		openssl x509 -req -sha256 -in "$$tmp_dir/intermediate.csr" \
			-CA "$$tmp_dir/root.pem" -CAkey "$$tmp_dir/root-key.pem" \
			-CAcreateserial -out "$$tmp_dir/chain.pem" -days 1825 \
			-extfile <(printf '%s\n' \
				'basicConstraints=critical,CA:TRUE,pathlen:0' \
				'keyUsage=critical,keyCertSign,cRLSign' \
				'subjectKeyIdentifier=hash' \
				'authorityKeyIdentifier=keyid,issuer') >/dev/null 2>&1; \
		openssl req -new -newkey rsa:3072 -nodes -sha256 \
			-keyout "$$tmp_dir/private.pem" -out "$$tmp_dir/leaf.csr" \
			-subj '/CN=Xarta Development Electronic Seal' >/dev/null 2>&1; \
		openssl x509 -req -sha256 -in "$$tmp_dir/leaf.csr" \
			-CA "$$tmp_dir/chain.pem" -CAkey "$$tmp_dir/intermediate-key.pem" \
			-CAcreateserial -out "$$tmp_dir/public.pem" -days 365 \
			-extfile <(printf '%s\n' \
				'basicConstraints=critical,CA:FALSE' \
				'keyUsage=critical,digitalSignature,nonRepudiation' \
				'subjectKeyIdentifier=hash' \
				'authorityKeyIdentifier=keyid,issuer') >/dev/null 2>&1; \
		openssl req -new -newkey rsa:3072 -nodes -sha256 \
			-keyout "$$tmp_dir/tsa-private.pem" -out "$$tmp_dir/tsa.csr" \
			-subj '/CN=Xarta Development Timestamp Authority' >/dev/null 2>&1; \
		openssl x509 -req -sha256 -in "$$tmp_dir/tsa.csr" \
			-CA "$$tmp_dir/root.pem" -CAkey "$$tmp_dir/root-key.pem" \
			-CAcreateserial -out "$$tmp_dir/tsa-public.pem" -days 365 \
			-extfile <(printf '%s\n' \
				'basicConstraints=critical,CA:FALSE' \
				'keyUsage=critical,digitalSignature,nonRepudiation' \
				'extendedKeyUsage=critical,timeStamping' \
				'subjectKeyIdentifier=hash' \
				'authorityKeyIdentifier=keyid,issuer') >/dev/null 2>&1; \
		openssl verify -CAfile "$$tmp_dir/root.pem" \
			-untrusted "$$tmp_dir/chain.pem" "$$tmp_dir/public.pem" >/dev/null; \
		openssl verify -purpose timestampsign -CAfile "$$tmp_dir/root.pem" \
			"$$tmp_dir/tsa-public.pem" >/dev/null; \
		chmod 600 "$$tmp_dir/private.pem" "$$tmp_dir/tsa-private.pem"; \
		mv "$$tmp_dir/private.pem" $(DEV_CERTIFICATE_DIR)/private.pem; \
		mv "$$tmp_dir/public.pem" $(DEV_CERTIFICATE_DIR)/public.pem; \
		mv "$$tmp_dir/chain.pem" $(DEV_CERTIFICATE_DIR)/chain.pem; \
		mv "$$tmp_dir/root.pem" $(DEV_CERTIFICATE_DIR)/root.pem; \
		mv "$$tmp_dir/tsa-private.pem" $(DEV_CERTIFICATE_DIR)/tsa-private.pem; \
		mv "$$tmp_dir/tsa-public.pem" $(DEV_CERTIFICATE_DIR)/tsa-public.pem; \
		echo "Generated development signing certificate"; \
	fi

db-create-archive: ## Create the archive database on the local PostgreSQL server
	@psql postgres://dev:dev@postgres/postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'archive'" | grep -qx 1 || createdb --maintenance-db=postgres://dev:dev@postgres/postgres archive

db-deploy: db-create-archive ## Deploy changes to the local application and archive databases
	$(SQITCH) --chdir db/archive deploy db:postgres://dev:dev@postgres/archive
	$(SQITCH) --chdir db deploy db:postgres://dev:dev@postgres/dev

streams: ## Reconcile local NATS streams
	$(UV) run --frozen python -m xarta.bin.jobs --run setup_nats=latest --port 8002

wallets: ## Generate new local x402 buyer and seller wallets
	$(UV) run --frozen python tools/generate_x402_wallets.py

archive-test: ## Run focused archive behavior tests
	$(UV) run --frozen pytest \
		tests/test_pagination.py \
		tests/services/test_archive_api.py \
		tests/services/test_archive_service.py \
		tests/services/test_archive_versioning.py \
		tests/services/test_archive_deletion.py \
		tests/services/test_archive_events.py \
		tests/jobs/test_expire_archived_documents.py \
		tests/nats/test_lifecycle.py \
		tests/tracking/test_tracked_capabilities.py

scenario-bundle: services-check certificates db-deploy streams ## Verify and measure the local archive/bundle flow
	$(UV) run --frozen python tests/scenarios/bundle.py

scenario-archive-representations: services-check db-deploy streams ## Measure multi-representation archive flow completion
	@workspace="$${XARTA_SCENARIO_WORKSPACE:-}"; image="$${XARTA_SCENARIO_IMAGE:-}"; \
	if [[ -z "$$workspace" ]]; then workspace="$$(docker inspect "$$(hostname)" --format '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)"; fi; \
	if [[ -z "$$image" ]]; then image="$$(docker inspect "$$(hostname)" --format '{{.Config.Image}}' 2>/dev/null || true)"; fi; \
	XARTA_SCENARIO_WORKSPACE="$${workspace:-$(CURDIR)}" \
	XARTA_SCENARIO_IMAGE="$${image:-dev-app_xarta_core}" \
	ARCHIVE_REPRESENTATIONS_NATS_WORKERS="$${ARCHIVE_REPRESENTATIONS_NATS_WORKERS:-10}" \
	SEARCH_SCENARIO_NATS_WORKERS="$${ARCHIVE_REPRESENTATIONS_NATS_WORKERS:-10}" \
	SCENARIO_INTAKE_BASE_URL=http://intake:8000 \
	$(UV) run --frozen python -m tests.scenarios.archive_representations \
		--compose-file .dev/compose.scenarios.yml $(ARCHIVE_REPRESENTATIONS_SCENARIO_ARGS)

scenario-signature-xarta-dev-seal-v1: services-check certificates db-deploy streams ## Verify legacy electronic-seal signing
	$(UV) run --frozen python -m tests.scenarios.signature_xarta_dev_seal_v1

scenario-signature-pades-b-b-v1: services-check certificates db-deploy streams ## Verify PAdES-B-B signing
	$(UV) run --frozen python -m tests.scenarios.signature_pades_b_b_v1

scenario-signature-pades-b-b-v1-split: services-check certificates db-deploy streams ## Verify PAdES-B-B across split services
	@workspace="$${XARTA_SCENARIO_WORKSPACE:-}"; image="$${XARTA_SCENARIO_IMAGE:-}"; \
	if [[ -z "$$workspace" ]]; then workspace="$$(docker inspect "$$(hostname)" --format '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)"; fi; \
	if [[ -z "$$image" ]]; then image="$$(docker inspect "$$(hostname)" --format '{{.Config.Image}}' 2>/dev/null || true)"; fi; \
	XARTA_SCENARIO_WORKSPACE="$${workspace:-$(CURDIR)}" \
	XARTA_SCENARIO_IMAGE="$${image:-dev-app_xarta_core}" \
	OTEL_EXPORTER_OTLP_ENDPOINT="$${OTEL_EXPORTER_OTLP_ENDPOINT:-http://otel-collector:4318}" \
	SCENARIO_JAEGER_QUERY_URL="$${SCENARIO_JAEGER_QUERY_URL:-http://jaeger:16686}" \
	SCENARIO_INTAKE_BASE_URL=http://intake:8000 \
	SCENARIO_ARCHIVE_BASE_URL=http://archive:8000 \
	$(UV) run --frozen python -m tests.scenarios.signature_pades_b_b_v1 \
		--compose-file .dev/compose.scenarios.yml --validate-trace $(SIGNATURE_SCENARIO_ARGS)

telemetry-check: ## Check the local Collector health and trace exporter metrics
	@curl --fail --silent http://otel-collector:13133/health >/dev/null
	@metrics="$$(curl --fail --silent http://otel-collector:8888/metrics)"; \
	grep -q '^otelcol_process_uptime' <<<"$$metrics"; \
	grep -q '^otelcol_exporter_queue_capacity' <<<"$$metrics"; \
	grep -q '^otelcol_exporter_queue_size' <<<"$$metrics"
	@echo "OpenTelemetry Collector health and exporter metrics are available"

scenario-signature-pades-b-t-v1: services-check certificates db-deploy streams ## Verify RFC 3161-backed PAdES-B-T signing
	$(UV) run --frozen python -m tests.scenarios.signature_pades_b_t_v1

scenario-generate-sign-archive: scenario-signature-xarta-dev-seal-v1 scenario-signature-pades-b-b-v1 scenario-signature-pades-b-t-v1 ## Verify every configured signing policy

scenario-search: services-check db-deploy streams ## Measure search lifecycle behavior across split Compose services
	@workspace="$${XARTA_SCENARIO_WORKSPACE:-}"; image="$${XARTA_SCENARIO_IMAGE:-}"; \
	if [[ -z "$$workspace" ]]; then workspace="$$(docker inspect "$$(hostname)" --format '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)"; fi; \
	if [[ -z "$$image" ]]; then image="$$(docker inspect "$$(hostname)" --format '{{.Config.Image}}' 2>/dev/null || true)"; fi; \
	XARTA_SCENARIO_WORKSPACE="$${workspace:-$(CURDIR)}" \
	XARTA_SCENARIO_IMAGE="$${image:-dev-app_xarta_core}" \
	SEARCH_SCENARIO_NATS_WORKERS="$${SEARCH_SCENARIO_NATS_WORKERS:-10}" \
	SCENARIO_INTAKE_BASE_URL=http://intake:8000 \
	SCENARIO_ARCHIVE_BASE_URL=http://archive:8000 \
	$(UV) run --frozen python tests/scenarios/search.py --compose-file .dev/compose.scenarios.yml $(SEARCH_SCENARIO_ARGS)

scenario-search-standalone: services-check certificates db-deploy streams ## Measure search lifecycle behavior in one local process
	$(UV) run --frozen python tests/scenarios/search.py $(SEARCH_SCENARIO_ARGS)

scenario-search-burst: services-check db-deploy streams ## Burst-submit and validate across split Compose services
	@workspace="$${XARTA_SCENARIO_WORKSPACE:-}"; image="$${XARTA_SCENARIO_IMAGE:-}"; \
	if [[ -z "$$workspace" ]]; then workspace="$$(docker inspect "$$(hostname)" --format '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)"; fi; \
	if [[ -z "$$image" ]]; then image="$$(docker inspect "$$(hostname)" --format '{{.Config.Image}}' 2>/dev/null || true)"; fi; \
	XARTA_SCENARIO_WORKSPACE="$${workspace:-$(CURDIR)}" \
	XARTA_SCENARIO_IMAGE="$${image:-dev-app_xarta_core}" \
	SEARCH_SCENARIO_NATS_WORKERS="$${SEARCH_SCENARIO_NATS_WORKERS:-10}" \
	SCENARIO_INTAKE_BASE_URL=http://intake:8000 \
	SCENARIO_ARCHIVE_BASE_URL=http://archive:8000 \
	$(UV) run --frozen python tests/scenarios/search_burst.py --compose-file .dev/compose.scenarios.yml $(SEARCH_BURST_SCENARIO_ARGS)

scenario-search-burst-standalone: services-check certificates db-deploy streams ## Burst-submit and validate in one local process
	$(UV) run --frozen python tests/scenarios/search_burst.py $(SEARCH_BURST_SCENARIO_ARGS)

scenario-wait-for: services-check certificates db-deploy streams ## Verify and measure wait-for retries and exhaustion
	$(UV) run --frozen python -m tests.scenarios.wait_for

scenario-flow-profile: services-check certificates db-deploy streams ## Verify and measure deployed flow-profile execution
	$(UV) run --frozen python tests/scenarios/flow_profile.py

scenario-postal-local: services-check certificates db-deploy streams ## Verify the local tracked postal production flow
	$(UV) run --frozen python -m tests.scenarios.postal_local

scenario-postal-local-printer: services-check certificates db-deploy streams ## PHYSICAL PRINT: submit one rendered postal letter through CUPS
	@test -n "$${POSTAL_STATION_PRINTER:-}" || { echo "POSTAL_STATION_PRINTER is required" >&2; exit 1; }
	@test "$${CONFIRM_PHYSICAL_PRINT:-}" = yes || { echo "Set CONFIRM_PHYSICAL_PRINT=yes to authorize one physical print" >&2; exit 1; }
	$(UV) run --frozen python -m tests.scenarios.postal_local --physical-printer "$${POSTAL_STATION_PRINTER}"

scenario-preview: services-check certificates db-deploy streams ## Verify and measure unpaid Jinja selftest previews
	$(UV) run --frozen python -m tests.scenarios.preview

scenario-x402-facilitator: ## Verify and measure 50 x402 payments through a local facilitator
	$(UV) run --frozen python -m tests.scenarios.x402_facilitator

scenario-x402-paid-intake: services-check certificates db-deploy streams ## Verify and measure 50 priced paid intake flows
	$(UV) run --frozen python -m tests.scenarios.x402_paid_intake

scenario-x402-base-sepolia: services-check certificates db-deploy streams ## OPT-IN: settle one real Base Sepolia USDC payment
	@test "$${X402_BASE_SEPOLIA_CONTRACT_TEST:-}" = true || { echo "Set X402_BASE_SEPOLIA_CONTRACT_TEST=true to authorize a real Base Sepolia settlement" >&2; exit 1; }
	$(UV) run --frozen python -m tests.scenarios.x402_base_sepolia

scenario-all: ## Run every deterministic devcontainer scenario sequentially
	$(MAKE) scenario-preview
	$(MAKE) scenario-bundle
	$(MAKE) scenario-generate-sign-archive
	$(MAKE) scenario-search
	$(MAKE) scenario-wait-for
	$(MAKE) scenario-flow-profile
	$(MAKE) scenario-postal-local
	$(MAKE) scenario-x402-facilitator
	$(MAKE) scenario-x402-paid-intake

station: ## Claim and process one local postal run with a fake printer
	POSTAL_STATION_API_URL="$${POSTAL_STATION_API_URL:-http://127.0.0.1:8000}" \
	POSTAL_STATION_ID="$${POSTAL_STATION_ID:-development-station}" \
	POSTAL_STATION_API_TOKEN="$${POSTAL_STATION_API_TOKEN:-development-postal-station-token}" \
	POSTAL_STATION_CACHE_DIR="$${POSTAL_STATION_CACHE_DIR:-.dev/runtime/postal-station}" \
	PYTHONPATH=apps/postal-station/src $(UV) run --frozen python -m postal_station

station-mock: ## Continuously simulate local postal printing, scans, and handover
	POSTAL_STATION_API_URL="$${POSTAL_STATION_API_URL:-http://127.0.0.1:8000}" \
	POSTAL_STATION_ID="$${POSTAL_STATION_ID:-development-station}" \
	POSTAL_STATION_API_TOKEN="$${POSTAL_STATION_API_TOKEN:-development-postal-station-token}" \
	POSTAL_STATION_CACHE_DIR="$${POSTAL_STATION_CACHE_DIR:-.dev/runtime/postal-station-mock}" \
	PYTHONPATH=apps/postal-station/src $(UV) run --frozen python -m postal_station \
		--mock --poll-interval-seconds "$${POSTAL_STATION_MOCK_POLL_INTERVAL_SECONDS:-1}"

station-cups: ## PHYSICAL PRINT: claim and process one local postal run through CUPS
	@test -n "$${POSTAL_STATION_PRINTER:-}" || { echo "POSTAL_STATION_PRINTER is required" >&2; exit 1; }
	@test "$${CONFIRM_PHYSICAL_PRINT:-}" = yes || { echo "Set CONFIRM_PHYSICAL_PRINT=yes to authorize physical printing" >&2; exit 1; }
	POSTAL_STATION_API_URL="$${POSTAL_STATION_API_URL:-http://127.0.0.1:8000}" \
	POSTAL_STATION_ID="$${POSTAL_STATION_ID:-development-station}" \
	POSTAL_STATION_API_TOKEN="$${POSTAL_STATION_API_TOKEN:-development-postal-station-token}" \
	POSTAL_STATION_CACHE_DIR="$${POSTAL_STATION_CACHE_DIR:-.dev/runtime/postal-station}" \
	PYTHONPATH=apps/postal-station/src $(UV) run --frozen python -m postal_station --cups

standalone: ## Run all services with hot reload
	@set -a; [[ ! -f .env ]] || source .env; set +a; \
	unset EVM_PRIVATE_KEY X402_SELLER_PRIVATE_KEY; \
	DOCUMENT_SIGN_PUBLIC_PEM=$(DEV_CERTIFICATE_DIR)/public.pem \
	DOCUMENT_SIGN_PRIVATE_PEM=$(DEV_CERTIFICATE_DIR)/private.pem \
	DOCUMENT_SIGN_CHAIN_PEM=$(DEV_CERTIFICATE_DIR)/chain.pem \
	SIGNATURE_CONFIG_PATH=.dev/conf/signature.json \
	TIMESTAMP_PROVIDERS_CONFIG_PATH=.dev/conf/timestamp-providers.json \
	POSTAL_CONFIGURATIONS_CONFIG_PATH=.dev/conf/postal-destinations.json \
	POSTAL_LOCAL_PROFILES_CONFIG_PATH=.dev/conf/postal-local-profiles.json \
	POSTAL_BPOST_PROFILES_CONFIG_PATH=.dev/conf/postal-bpost-profiles.json \
	POSTAL_LOCAL_CAPACITY_CONFIG_PATH=.dev/conf/postal-local-capacity.json \
	POSTAL_LOCAL_STATIONS_CONFIG_PATH=.dev/conf/postal-local-stations.json \
	POSTAL_LOCAL_STORAGE_CONFIG_PATH=.dev/conf/postal-local-storage.json \
	ARCHIVE_POSTGRESQL_USER="$${ARCHIVE_POSTGRESQL_USER:-dev}" \
	ARCHIVE_POSTGRESQL_PASSWORD="$${ARCHIVE_POSTGRESQL_PASSWORD:-dev}" \
	ARCHIVE_POSTGRESQL_DATABASE="$${ARCHIVE_POSTGRESQL_DATABASE:-archive}" \
	ARCHIVE_POSTGRESQL_HOST="$${ARCHIVE_POSTGRESQL_HOST:-postgres}" \
	INTAKE_CAPABILITIES='["archive","bundle","doccle","email","generate","postal","search-index","sftp","signature","wait-for","webhook"]' \
	SCENARIO_DECODE_POSTGRES_JSON=true \
	$(UV) run --frozen sanic -r -1 --workers 1 --host 0.0.0.0 --factory tests.scenarios.common:scenario_standalone_application

debug: ## Run all services in debug mode
	@set -a; [[ ! -f .env ]] || source .env; set +a; \
	unset EVM_PRIVATE_KEY X402_SELLER_PRIVATE_KEY; \
	DOCUMENT_SIGN_PUBLIC_PEM=$(DEV_CERTIFICATE_DIR)/public.pem \
	DOCUMENT_SIGN_PRIVATE_PEM=$(DEV_CERTIFICATE_DIR)/private.pem \
	DOCUMENT_SIGN_CHAIN_PEM=$(DEV_CERTIFICATE_DIR)/chain.pem \
	SIGNATURE_CONFIG_PATH=.dev/conf/signature.json \
	TIMESTAMP_PROVIDERS_CONFIG_PATH=.dev/conf/timestamp-providers.json \
	POSTAL_CONFIGURATIONS_CONFIG_PATH=.dev/conf/postal-destinations.json \
	POSTAL_LOCAL_PROFILES_CONFIG_PATH=.dev/conf/postal-local-profiles.json \
	POSTAL_BPOST_PROFILES_CONFIG_PATH=.dev/conf/postal-bpost-profiles.json \
	POSTAL_LOCAL_CAPACITY_CONFIG_PATH=.dev/conf/postal-local-capacity.json \
	POSTAL_LOCAL_STATIONS_CONFIG_PATH=.dev/conf/postal-local-stations.json \
	POSTAL_LOCAL_STORAGE_CONFIG_PATH=.dev/conf/postal-local-storage.json \
	ARCHIVE_POSTGRESQL_USER="$${ARCHIVE_POSTGRESQL_USER:-dev}" \
	ARCHIVE_POSTGRESQL_PASSWORD="$${ARCHIVE_POSTGRESQL_PASSWORD:-dev}" \
	ARCHIVE_POSTGRESQL_DATABASE="$${ARCHIVE_POSTGRESQL_DATABASE:-archive}" \
	ARCHIVE_POSTGRESQL_HOST="$${ARCHIVE_POSTGRESQL_HOST:-postgres}" \
	INTAKE_CAPABILITIES='["archive","bundle","doccle","email","generate","postal","search-index","sftp","signature","wait-for","webhook"]' \
	SCENARIO_DECODE_POSTGRES_JSON=true \
	$(UV) run --frozen sanic -r -1 --debug --host 0.0.0.0 --factory tests.scenarios.common:scenario_standalone_application

job-expire-documents:
	$(UV) run --frozen python -m xarta.bin.jobs --run expire_archived_documents=latest --port 8001

job-cleanup-generate:
	$(UV) run --frozen python -m xarta.bin.jobs --run cleanup_generate=latest --port 8001

job-cleanup-bundle:
	$(UV) run --frozen python -m xarta.bin.jobs --run cleanup_bundle=latest --port 8001

job-replay-dead-letters: ## Replay explicitly eligible dead-lettered requests
	$(UV) run --frozen python -m xarta.bin.jobs --run replay_dead_letters=latest --port 8001
