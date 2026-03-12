-include .env
export

IMAGE_NAME   := claude-agent
IMAGE_TAG    := latest
IMAGE        := $(IMAGE_NAME):$(IMAGE_TAG)
PROXY_IMAGE  := anthropic-proxy:latest
PROXY_NAME   := anthropic-proxy
PROXY_NET    := proxy-net
RUNTIME      := runsc
MEMORY       := 2g
CPUS         := 2
PIDS_LIMIT   := 100
TMP_SIZE     := 100m
WORK_SIZE    := 500m
PROXY_PORT   := 18080
PROXY_LOG    := /tmp/anthropic-proxy.log
VENV         := .venv
PYTHON       := $(VENV)/bin/python

.PHONY: build build-proxy run run-proxied prompt prompt-proxied \
        verify-gvisor clean help \
        start-proxy stop-proxy proxy-status proxy-logs venv

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: $(VENV)/bin/activate ## Create virtualenv and install proxy dependencies
$(VENV)/bin/activate: requirements-proxy.txt
	python3 -m venv $(VENV)
	$(PYTHON) -m pip install --quiet -r requirements-proxy.txt
	@touch $(VENV)/bin/activate

build: ## Build the agent image
	docker build -t $(IMAGE) .

build-proxy: ## Build the proxy image
	docker build -t $(PROXY_IMAGE) -f Dockerfile.proxy .

run: build ## Run agent in gVisor sandbox (API key from host env)
	@test -n "$(ANTHROPIC_API_KEY)" || { echo "ERROR: ANTHROPIC_API_KEY is not set"; exit 1; }
	docker run \
		--runtime=$(RUNTIME) \
		--rm \
		--cap-drop ALL \
		--security-opt no-new-privileges \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,size=$(TMP_SIZE) \
		--tmpfs /workspace:rw,noexec,nosuid,size=$(WORK_SIZE) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--user 1000:1000 \
		-e ANTHROPIC_API_KEY="$(ANTHROPIC_API_KEY)" \
		$(IMAGE)

prompt: build ## Interactive prompt — direct mode (API key from host env)
	@test -n "$(ANTHROPIC_API_KEY)" || { echo "ERROR: ANTHROPIC_API_KEY is not set"; exit 1; }
	docker run \
		--runtime=$(RUNTIME) \
		--rm -it \
		--cap-drop ALL \
		--security-opt no-new-privileges \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,size=$(TMP_SIZE) \
		--tmpfs /workspace:rw,noexec,nosuid,size=$(WORK_SIZE) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--user 1000:1000 \
		-e ANTHROPIC_API_KEY="$(ANTHROPIC_API_KEY)" \
		$(IMAGE) --interactive

start-proxy: build-proxy ## Start the proxy container (bridge + internal network)
	@test -n "$(ANTHROPIC_API_KEY)" || { echo "ERROR: ANTHROPIC_API_KEY is not set"; exit 1; }
	@if docker inspect -f '{{.State.Running}}' $(PROXY_NAME) 2>/dev/null | grep -q true; then \
		echo "Proxy already running"; \
	else \
		docker network inspect $(PROXY_NET) >/dev/null 2>&1 || \
			docker network create --internal $(PROXY_NET); \
		echo "Starting proxy container..."; \
		docker run -d --name $(PROXY_NAME) \
			--network bridge \
			-e ANTHROPIC_API_KEY="$(ANTHROPIC_API_KEY)" \
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

proxy-status: ## Check proxy status
	@docker inspect $(PROXY_NAME) --format 'Proxy running ({{.State.Status}})' 2>/dev/null || echo "Proxy not running"

proxy-logs: ## Show proxy logs
	@docker logs $(PROXY_NAME) 2>/dev/null || echo "No proxy container found"

run-proxied: build start-proxy ## Run agent via proxy (gVisor sandbox, network-isolated)
	@PROXY_IP=$$(docker inspect -f '{{json .NetworkSettings.Networks}}' $(PROXY_NAME) | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['$(PROXY_NET)']['IPAddress'])") && \
	echo "Proxy IP on $(PROXY_NET): $$PROXY_IP" && \
	docker run \
		--runtime=$(RUNTIME) \
		--rm \
		--network=$(PROXY_NET) \
		--cap-drop ALL \
		--security-opt no-new-privileges \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,size=$(TMP_SIZE) \
		--tmpfs /workspace:rw,noexec,nosuid,size=$(WORK_SIZE) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--user 1000:1000 \
		--add-host=proxy-host:$$PROXY_IP \
		-e ANTHROPIC_PROXY_URL="http://proxy-host:$(PROXY_PORT)" \
		-e ANTHROPIC_API_KEY="proxied" \
		$(IMAGE)

prompt-proxied: build start-proxy ## Interactive prompt — network-isolated via proxy
	@PROXY_IP=$$(docker inspect -f '{{json .NetworkSettings.Networks}}' $(PROXY_NAME) | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['$(PROXY_NET)']['IPAddress'])") && \
	echo "Proxy IP on $(PROXY_NET): $$PROXY_IP" && \
	docker run \
		--runtime=$(RUNTIME) \
		--rm -it \
		--network=$(PROXY_NET) \
		--cap-drop ALL \
		--security-opt no-new-privileges \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,size=$(TMP_SIZE) \
		--tmpfs /workspace:rw,noexec,nosuid,size=$(WORK_SIZE) \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--user 1000:1000 \
		--add-host=proxy-host:$$PROXY_IP \
		-e ANTHROPIC_PROXY_URL="http://proxy-host:$(PROXY_PORT)" \
		-e ANTHROPIC_API_KEY="proxied" \
		$(IMAGE) --interactive

verify-gvisor: build ## Verify gVisor runtime via dmesg output
	docker run --runtime=$(RUNTIME) --rm --entrypoint python $(IMAGE) \
		-c "import subprocess; print(subprocess.check_output(['dmesg']).decode()[:200])"

clean: stop-proxy ## Remove images, network, and clean up
	docker rmi $(IMAGE) 2>/dev/null || true
	docker rmi $(PROXY_IMAGE) 2>/dev/null || true
	docker network rm $(PROXY_NET) 2>/dev/null || true
	rm -f $(PROXY_LOG)
