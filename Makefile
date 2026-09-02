-include .env
export

IMAGE_NAME   := sandbox-agent
IMAGE_TAG    := latest
IMAGE        := $(IMAGE_NAME):$(IMAGE_TAG)
PROXY_IMAGE  := llm-proxy:latest
PROXY_NAME   := llm-proxy
PROXY_NET    := proxy-net
RUNTIME      := runsc
MEMORY       := 2g
CPUS         := 2
PIDS_LIMIT   := 100
TMP_SIZE     := 100m
WORK_SIZE    := 500m
PROXY_PORT   := 18080
PROXY_LOG    := /tmp/llm-proxy.log
VENV         := .venv
PYTHON       := $(VENV)/bin/python

LOG_DIR            := logs
LOG_CAPTURE        := scripts/capture-logs.sh
LOG_MERGE          := scripts/merge-logs.sh
LOG_RETENTION_DAYS := 30

# Capability knobs — secure defaults; opt in per run (e.g. ALLOW_SHELL=1 make prompt)
COMMA          := ,
ALLOW_SHELL      ?=        # 1 enables the operator !exec REPL command
ALLOW_SHELL_TOOL ?=        # 1 enables the model-callable run_shell tool
SHELL_TIMEOUT    ?= 30     # timeout (seconds) shared by !exec and run_shell
WORKSPACE_EXEC ?= 0        # 1 lets the agent run scripts it writes to /workspace
CAP_ADD        ?=          # comma-separated Linux capabilities to add to the bounding set
RUN_AS_ROOT    ?= 0        # 1 runs the agent as uid 0 (needed to make CAP_ADD *effective*;
                           # still contained by gVisor's Sentry, not host root)

# /workspace defaults to noexec — pass the explicit `exec` option to allow it
# (dropping the word `noexec` is NOT enough; gVisor tmpfs defaults to noexec).
ifeq ($(WORKSPACE_EXEC),1)
WORKSPACE_TMPFS := /workspace:rw,nosuid,exec,size=$(WORK_SIZE)
else
WORKSPACE_TMPFS := /workspace:rw,noexec,nosuid,size=$(WORK_SIZE)
endif
CAP_ADD_FLAGS := $(foreach c,$(subst $(COMMA), ,$(CAP_ADD)),--cap-add $(c))
ifeq ($(RUN_AS_ROOT),1)
RUN_USER := 0:0
else
RUN_USER := 1000:1000
endif

.PHONY: build build-proxy run run-proxied prompt prompt-proxied \
        run-openai prompt-openai run-openai-proxied prompt-openai-proxied \
        verify-gvisor clean help \
        start-proxy stop-proxy restart-proxy clean-agents proxy-status proxy-logs proxy-logs-follow venv \
        run-logged run-proxied-logged prompt-logged prompt-proxied-logged \
        logs-list logs-latest logs-review logs-events logs-merge \
        logs-clean logs-clean-all

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: $(VENV)/bin/activate ## Create virtualenv and install dev dependencies (proxy + tests)
$(VENV)/bin/activate: requirements-dev.txt requirements-proxy.txt
	python3 -m venv $(VENV)
	$(PYTHON) -m pip install --quiet -r requirements-dev.txt
	@touch $(VENV)/bin/activate

build: ## Build the agent image
	docker build -t $(IMAGE) .

build-proxy: ## Build the proxy image
	docker build -t $(PROXY_IMAGE) -f Dockerfile.proxy .

run: build ## Run agent in gVisor sandbox (API key from host env)
	@test -n "$(ANTHROPIC_API_KEY)$(OPENAI_API_KEY)" || { echo "ERROR: set ANTHROPIC_API_KEY and/or OPENAI_API_KEY"; exit 1; }
	@echo "Running agent (runtime=$(RUNTIME), provider=$${LLM_PROVIDER:-auto}, API keys hidden)"
	@docker run \
		--runtime=$(RUNTIME) \
		--rm \
		--cap-drop ALL \
		$(CAP_ADD_FLAGS) \
		--security-opt no-new-privileges \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,size=$(TMP_SIZE) \
		--tmpfs $(WORKSPACE_TMPFS) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--user $(RUN_USER) \
		-e ALLOW_SHELL="$(ALLOW_SHELL)" \
		-e ALLOW_SHELL_TOOL="$(ALLOW_SHELL_TOOL)" \
		-e SHELL_TIMEOUT="$(SHELL_TIMEOUT)" \
		-e ANTHROPIC_API_KEY="$(ANTHROPIC_API_KEY)" \
		-e ANTHROPIC_MODEL="$(ANTHROPIC_MODEL)" \
		-e OPENAI_API_KEY="$(OPENAI_API_KEY)" \
		-e OPENAI_MODEL="$(OPENAI_MODEL)" \
		-e OPENAI_REASONING_EFFORT="$(OPENAI_REASONING_EFFORT)" \
		-e OPENAI_MAX_TOKENS="$(OPENAI_MAX_TOKENS)" \
		-e LLM_PROVIDER="$(LLM_PROVIDER)" \
		$(IMAGE)

prompt: build ## Interactive prompt — direct mode (API key from host env)
	@test -n "$(ANTHROPIC_API_KEY)$(OPENAI_API_KEY)" || { echo "ERROR: set ANTHROPIC_API_KEY and/or OPENAI_API_KEY"; exit 1; }
	@echo "Starting REPL (runtime=$(RUNTIME), provider=$${LLM_PROVIDER:-auto}, API keys hidden)"
	@docker run \
		--runtime=$(RUNTIME) \
		--rm -it \
		--cap-drop ALL \
		$(CAP_ADD_FLAGS) \
		--security-opt no-new-privileges \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,size=$(TMP_SIZE) \
		--tmpfs $(WORKSPACE_TMPFS) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--user $(RUN_USER) \
		-e ALLOW_SHELL="$(ALLOW_SHELL)" \
		-e ALLOW_SHELL_TOOL="$(ALLOW_SHELL_TOOL)" \
		-e SHELL_TIMEOUT="$(SHELL_TIMEOUT)" \
		-e ANTHROPIC_API_KEY="$(ANTHROPIC_API_KEY)" \
		-e ANTHROPIC_MODEL="$(ANTHROPIC_MODEL)" \
		-e OPENAI_API_KEY="$(OPENAI_API_KEY)" \
		-e OPENAI_MODEL="$(OPENAI_MODEL)" \
		-e OPENAI_REASONING_EFFORT="$(OPENAI_REASONING_EFFORT)" \
		-e OPENAI_MAX_TOKENS="$(OPENAI_MAX_TOKENS)" \
		-e LLM_PROVIDER="$(LLM_PROVIDER)" \
		$(IMAGE) --interactive

start-proxy: build-proxy ## Start the proxy container (bridge + internal network)
	@test -n "$(ANTHROPIC_API_KEY)$(OPENAI_API_KEY)" || { echo "ERROR: set ANTHROPIC_API_KEY and/or OPENAI_API_KEY"; exit 1; }
	@if docker inspect -f '{{.State.Running}}' $(PROXY_NAME) 2>/dev/null | grep -q true; then \
		echo "Proxy already running"; \
	else \
		docker network inspect $(PROXY_NET) >/dev/null 2>&1 || \
			docker network create --internal $(PROXY_NET); \
		echo "Starting proxy container..."; \
		docker run -d --name $(PROXY_NAME) \
			--network bridge \
			-e ANTHROPIC_API_KEY="$(ANTHROPIC_API_KEY)" \
			-e OPENAI_API_KEY="$(OPENAI_API_KEY)" \
			-e PROXY_HOST=0.0.0.0 \
			-e PROXY_PORT=$(PROXY_PORT) \
			$(if $(PROXY_ALLOWED_EXTERNAL_HOSTS),-e PROXY_ALLOWED_EXTERNAL_HOSTS="$(PROXY_ALLOWED_EXTERNAL_HOSTS)") \
			$(PROXY_IMAGE); \
		docker network connect $(PROXY_NET) $(PROXY_NAME); \
		sleep 1; \
		if docker inspect $(PROXY_NAME) --format '{{.State.Running}}' | grep -q true; then \
			echo "Proxy started"; \
		else \
			echo "ERROR: Proxy failed to start"; \
			docker logs $(PROXY_NAME); \
			exit 1; \
		fi; \
	fi

stop-proxy: ## Stop the proxy container
	@docker rm -f $(PROXY_NAME) 2>/dev/null && echo "Proxy stopped" || echo "Proxy not running"

restart-proxy: stop-proxy start-proxy ## Restart the proxy (picks up rebuilt image + changed keys/config)
	@echo "Proxy restarted with current image and keys."

clean-agents: ## Remove any stray agent containers (e.g. after a closed terminal)
	@ids=$$(docker ps -aq --filter ancestor=$(IMAGE)); \
	if [ -n "$$ids" ]; then \
		docker rm -f $$ids >/dev/null && echo "Removed stray agent container(s)."; \
	else \
		echo "No stray agent containers."; \
	fi

proxy-status: ## Check proxy status
	@docker inspect $(PROXY_NAME) --format 'Proxy running ({{.State.Status}})' 2>/dev/null || echo "Proxy not running"

proxy-logs: ## Show proxy logs (snapshot; logs are on stderr, both streams shown)
	@if docker inspect $(PROXY_NAME) >/dev/null 2>&1; then \
		docker logs $(PROXY_NAME) 2>&1; \
	else \
		echo "No proxy container found"; \
	fi

proxy-logs-follow: ## Stream proxy logs live (Ctrl-C to stop)
	@if docker inspect $(PROXY_NAME) >/dev/null 2>&1; then \
		docker logs -f $(PROXY_NAME) 2>&1; \
	else \
		echo "No proxy container found"; \
	fi

run-proxied: build start-proxy ## Run agent via proxy (gVisor sandbox, network-isolated)
	@PROXY_IP=$$(docker inspect -f '{{json .NetworkSettings.Networks}}' $(PROXY_NAME) | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['$(PROXY_NET)']['IPAddress'])") && \
	echo "Proxy IP on $(PROXY_NET): $$PROXY_IP" && \
	docker run \
		--runtime=$(RUNTIME) \
		--rm \
		--network=$(PROXY_NET) \
		--cap-drop ALL \
		$(CAP_ADD_FLAGS) \
		--security-opt no-new-privileges \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,size=$(TMP_SIZE) \
		--tmpfs $(WORKSPACE_TMPFS) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--user $(RUN_USER) \
		-e ALLOW_SHELL="$(ALLOW_SHELL)" \
		-e ALLOW_SHELL_TOOL="$(ALLOW_SHELL_TOOL)" \
		-e SHELL_TIMEOUT="$(SHELL_TIMEOUT)" \
		--add-host=proxy-host:$$PROXY_IP \
		-e ANTHROPIC_PROXY_URL="http://proxy-host:$(PROXY_PORT)" \
		-e ANTHROPIC_API_KEY="proxied" \
		-e ANTHROPIC_MODEL="$(ANTHROPIC_MODEL)" \
		$(IMAGE)

prompt-proxied: build start-proxy ## Interactive prompt — network-isolated via proxy
	@PROXY_IP=$$(docker inspect -f '{{json .NetworkSettings.Networks}}' $(PROXY_NAME) | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['$(PROXY_NET)']['IPAddress'])") && \
	echo "Proxy IP on $(PROXY_NET): $$PROXY_IP" && \
	docker run \
		--runtime=$(RUNTIME) \
		--rm -it \
		--network=$(PROXY_NET) \
		--cap-drop ALL \
		$(CAP_ADD_FLAGS) \
		--security-opt no-new-privileges \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,size=$(TMP_SIZE) \
		--tmpfs $(WORKSPACE_TMPFS) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--user $(RUN_USER) \
		-e ALLOW_SHELL="$(ALLOW_SHELL)" \
		-e ALLOW_SHELL_TOOL="$(ALLOW_SHELL_TOOL)" \
		-e SHELL_TIMEOUT="$(SHELL_TIMEOUT)" \
		--add-host=proxy-host:$$PROXY_IP \
		-e ANTHROPIC_PROXY_URL="http://proxy-host:$(PROXY_PORT)" \
		-e ANTHROPIC_API_KEY="proxied" \
		-e ANTHROPIC_MODEL="$(ANTHROPIC_MODEL)" \
		$(IMAGE) --interactive

# ---------------------------------------------------------------------------
# OpenAI provider — dedicated targets (no need to set LLM_PROVIDER by hand)
# ---------------------------------------------------------------------------

run-openai: ## Run agent in gVisor sandbox (OpenAI, direct mode)
	@LLM_PROVIDER=openai $(MAKE) --no-print-directory run

prompt-openai: ## Interactive prompt — OpenAI, direct mode
	@LLM_PROVIDER=openai $(MAKE) --no-print-directory prompt

run-openai-proxied: build start-proxy ## Run agent via proxy (OpenAI, network-isolated)
	@test -n "$(OPENAI_API_KEY)" || { echo "ERROR: OPENAI_API_KEY is not set (needed by the proxy)"; exit 1; }
	@PROXY_IP=$$(docker inspect -f '{{json .NetworkSettings.Networks}}' $(PROXY_NAME) | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['$(PROXY_NET)']['IPAddress'])") && \
	echo "Proxy IP on $(PROXY_NET): $$PROXY_IP (provider=openai, API key hidden)" && \
	docker run \
		--runtime=$(RUNTIME) \
		--rm \
		--network=$(PROXY_NET) \
		--cap-drop ALL \
		$(CAP_ADD_FLAGS) \
		--security-opt no-new-privileges \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,size=$(TMP_SIZE) \
		--tmpfs $(WORKSPACE_TMPFS) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--user $(RUN_USER) \
		-e ALLOW_SHELL="$(ALLOW_SHELL)" \
		-e ALLOW_SHELL_TOOL="$(ALLOW_SHELL_TOOL)" \
		-e SHELL_TIMEOUT="$(SHELL_TIMEOUT)" \
		--add-host=proxy-host:$$PROXY_IP \
		-e LLM_PROVIDER="openai" \
		-e OPENAI_PROXY_URL="http://proxy-host:$(PROXY_PORT)" \
		-e OPENAI_API_KEY="proxied" \
		-e OPENAI_MODEL="$(OPENAI_MODEL)" \
		-e OPENAI_REASONING_EFFORT="$(OPENAI_REASONING_EFFORT)" \
		$(IMAGE)

prompt-openai-proxied: build start-proxy ## Interactive prompt — OpenAI, network-isolated via proxy
	@test -n "$(OPENAI_API_KEY)" || { echo "ERROR: OPENAI_API_KEY is not set (needed by the proxy)"; exit 1; }
	@PROXY_IP=$$(docker inspect -f '{{json .NetworkSettings.Networks}}' $(PROXY_NAME) | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['$(PROXY_NET)']['IPAddress'])") && \
	echo "Proxy IP on $(PROXY_NET): $$PROXY_IP (provider=openai, API key hidden)" && \
	docker run \
		--runtime=$(RUNTIME) \
		--rm -it \
		--network=$(PROXY_NET) \
		--cap-drop ALL \
		$(CAP_ADD_FLAGS) \
		--security-opt no-new-privileges \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,size=$(TMP_SIZE) \
		--tmpfs $(WORKSPACE_TMPFS) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--user $(RUN_USER) \
		-e ALLOW_SHELL="$(ALLOW_SHELL)" \
		-e ALLOW_SHELL_TOOL="$(ALLOW_SHELL_TOOL)" \
		-e SHELL_TIMEOUT="$(SHELL_TIMEOUT)" \
		--add-host=proxy-host:$$PROXY_IP \
		-e LLM_PROVIDER="openai" \
		-e OPENAI_PROXY_URL="http://proxy-host:$(PROXY_PORT)" \
		-e OPENAI_API_KEY="proxied" \
		-e OPENAI_MODEL="$(OPENAI_MODEL)" \
		-e OPENAI_REASONING_EFFORT="$(OPENAI_REASONING_EFFORT)" \
		$(IMAGE) --interactive

verify-gvisor: build ## Verify gVisor runtime via dmesg output
	docker run --runtime=$(RUNTIME) --rm --entrypoint python $(IMAGE) \
		-c "import subprocess; print(subprocess.check_output(['dmesg']).decode()[:200])"

clean: stop-proxy ## Remove images, network, and clean up
	docker rmi $(IMAGE) 2>/dev/null || true
	docker rmi $(PROXY_IMAGE) 2>/dev/null || true
	docker network rm $(PROXY_NET) 2>/dev/null || true
	rm -f $(PROXY_LOG)

# ---------------------------------------------------------------------------
# Logged run targets — identical to base targets but persist logs to disk
# ---------------------------------------------------------------------------

run-logged: build ## Run agent with log capture (direct mode)
	@test -n "$(ANTHROPIC_API_KEY)" || { echo "ERROR: ANTHROPIC_API_KEY is not set"; exit 1; }
	@bash $(LOG_CAPTURE) \
		--mode direct \
		--interactive false \
		--log-dir $(LOG_DIR) \
		--agent-image $(IMAGE) \
		--runtime $(RUNTIME) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--tmp-size $(TMP_SIZE) \
		--work-size $(WORK_SIZE)

run-proxied-logged: build start-proxy ## Run agent via proxy with log capture
	@bash $(LOG_CAPTURE) \
		--mode proxied \
		--interactive false \
		--log-dir $(LOG_DIR) \
		--agent-image $(IMAGE) \
		--runtime $(RUNTIME) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--tmp-size $(TMP_SIZE) \
		--work-size $(WORK_SIZE) \
		--proxy-name $(PROXY_NAME) \
		--proxy-net $(PROXY_NET) \
		--proxy-port $(PROXY_PORT)

prompt-logged: build ## Interactive REPL with log capture (direct mode)
	@test -n "$(ANTHROPIC_API_KEY)" || { echo "ERROR: ANTHROPIC_API_KEY is not set"; exit 1; }
	@bash $(LOG_CAPTURE) \
		--mode direct \
		--interactive true \
		--log-dir $(LOG_DIR) \
		--agent-image $(IMAGE) \
		--runtime $(RUNTIME) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--tmp-size $(TMP_SIZE) \
		--work-size $(WORK_SIZE)

prompt-proxied-logged: build start-proxy ## Interactive REPL with log capture (proxied mode)
	@bash $(LOG_CAPTURE) \
		--mode proxied \
		--interactive true \
		--log-dir $(LOG_DIR) \
		--agent-image $(IMAGE) \
		--runtime $(RUNTIME) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--tmp-size $(TMP_SIZE) \
		--work-size $(WORK_SIZE) \
		--proxy-name $(PROXY_NAME) \
		--proxy-net $(PROXY_NET) \
		--proxy-port $(PROXY_PORT)

# ---------------------------------------------------------------------------
# Log review and maintenance targets
# ---------------------------------------------------------------------------

SESSION ?= latest

logs-list: ## List all captured sessions with metadata summary
	@echo ""
	@printf "%-40s %-10s %-6s %-10s %s\n" "SESSION" "MODE" "EXIT" "DURATION" "STATUS"
	@printf "%-40s %-10s %-6s %-10s %s\n" "-------" "----" "----" "--------" "------"
	@for d in $(LOG_DIR)/*/; do \
		[ -d "$$d" ] || continue; \
		session="$$(basename $$d)"; \
		[ "$$session" = "latest" ] && continue; \
		meta="$$d/metadata.json"; \
		if [ -f "$$meta" ]; then \
			if command -v jq >/dev/null 2>&1; then \
				mode="$$(jq -r '.mode // "?"' $$meta)"; \
				exit_code="$$(jq -r '.agent_exit_code // "-"' $$meta)"; \
				duration="$$(jq -r 'if .duration_seconds != null then (.duration_seconds | tostring) + "s" else "-" end' $$meta)"; \
				ended="$$(jq -r '.ended_at' $$meta)"; \
			else \
				mode="$$(python3 -c "import json,sys; d=json.load(open('$$meta')); print(d.get('mode','?'))" 2>/dev/null || echo '?')"; \
				exit_code="$$(python3 -c "import json,sys; d=json.load(open('$$meta')); print(d.get('agent_exit_code','-'))" 2>/dev/null || echo '-')"; \
				duration="$$(python3 -c "import json,sys; d=json.load(open('$$meta')); s=d.get('duration_seconds'); print(str(s)+'s' if s is not None else '-')" 2>/dev/null || echo '-')"; \
				ended="$$(python3 -c "import json,sys; d=json.load(open('$$meta')); print(d.get('ended_at','null'))" 2>/dev/null || echo 'null')"; \
			fi; \
			if [ "$$ended" = "null" ] || [ -z "$$ended" ]; then \
				status="INCOMPLETE"; \
			elif [ "$$exit_code" = "137" ]; then \
				status="OOM-KILLED"; \
			elif [ "$$exit_code" = "0" ]; then \
				status="OK"; \
			else \
				status="ERROR($$exit_code)"; \
			fi; \
			printf "%-40s %-10s %-6s %-10s %s\n" "$$session" "$$mode" "$$exit_code" "$$duration" "$$status"; \
		else \
			printf "%-40s %-10s %-6s %-10s %s\n" "$$session" "?" "-" "-" "NO-METADATA"; \
		fi; \
	done
	@echo ""

logs-latest: ## Review the most recent session's merged log
	@if [ ! -L "$(LOG_DIR)/latest" ]; then \
		echo "No sessions found. Run make run-logged first."; exit 1; \
	fi
	@LATEST="$(LOG_DIR)/latest"; \
	if [ -f "$$LATEST/session.log" ]; then \
		cat "$$LATEST/session.log"; \
	elif [ -f "$$LATEST/agent.log" ]; then \
		echo "[logs-latest] No session.log found — showing agent.log:"; \
		cat "$$LATEST/agent.log"; \
	else \
		echo "[logs-latest] No logs found in $$(readlink $(LOG_DIR)/latest)"; \
	fi

logs-review: ## Review a session's merged log (SESSION=<id>)
	@TARGET="$(LOG_DIR)/$(SESSION)"; \
	if [ ! -d "$$TARGET" ] && [ ! -L "$$TARGET" ]; then \
		echo "Session not found: $(SESSION)"; \
		echo "Run 'make logs-list' to see available sessions."; exit 1; \
	fi; \
	if [ -f "$$TARGET/session.log" ]; then \
		cat "$$TARGET/session.log"; \
	elif [ -f "$$TARGET/agent.log" ]; then \
		echo "[logs-review] No session.log found — showing agent.log:"; \
		cat "$$TARGET/agent.log"; \
	else \
		echo "[logs-review] No logs found in $$TARGET"; \
	fi

logs-events: ## Show runtime events for a session (SESSION=<id>)
	@TARGET="$(LOG_DIR)/$(SESSION)"; \
	if [ ! -d "$$TARGET" ] && [ ! -L "$$TARGET" ]; then \
		echo "Session not found: $(SESSION)"; \
		echo "Run 'make logs-list' to see available sessions."; exit 1; \
	fi; \
	if [ ! -f "$$TARGET/events.log" ]; then \
		echo "No events.log found in $$TARGET"; exit 0; \
	fi; \
	if command -v jq >/dev/null 2>&1; then \
		cat "$$TARGET/events.log" | jq -r '"\(.time // .timeNano) \(.Action) \(.Actor.ID[:12] // "")"' 2>/dev/null \
		|| cat "$$TARGET/events.log"; \
	else \
		python3 -m json.tool < "$$TARGET/events.log" 2>/dev/null \
		|| cat "$$TARGET/events.log"; \
	fi

logs-merge: ## Regenerate session.log for a session (SESSION=<id>)
	@TARGET="$(LOG_DIR)/$(SESSION)"; \
	if [ ! -d "$$TARGET" ] && [ ! -L "$$TARGET" ]; then \
		echo "Session not found: $(SESSION)"; exit 1; \
	fi
	@bash $(LOG_MERGE) "$(LOG_DIR)/$(SESSION)"

logs-clean: ## Remove sessions older than LOG_RETENTION_DAYS (default: 30)
	@echo "Removing sessions older than $(LOG_RETENTION_DAYS) days from $(LOG_DIR)/..."
	@find $(LOG_DIR) -maxdepth 1 -mindepth 1 -type d \
		-not -name '.gitkeep' \
		-mtime +$(LOG_RETENTION_DAYS) \
		-exec rm -rf {} + 2>/dev/null || true
	@# Refresh latest symlink if its target was removed
	@if [ -L "$(LOG_DIR)/latest" ] && [ ! -e "$(LOG_DIR)/latest" ]; then \
		rm -f "$(LOG_DIR)/latest"; \
		echo "Removed stale 'latest' symlink."; \
	fi
	@echo "Done."

logs-clean-all: ## Remove all session logs (keeps logs/.gitkeep)
	@echo "Removing all session logs from $(LOG_DIR)/..."
	@find $(LOG_DIR) -maxdepth 1 -mindepth 1 \
		-not -name '.gitkeep' \
		-exec rm -rf {} + 2>/dev/null || true
	@echo "Done."
