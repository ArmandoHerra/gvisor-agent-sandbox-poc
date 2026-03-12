-include .env
export

IMAGE_NAME   := claude-agent
IMAGE_TAG    := latest
IMAGE        := $(IMAGE_NAME):$(IMAGE_TAG)
RUNTIME      := runsc
MEMORY       := 2g
CPUS         := 2
PIDS_LIMIT   := 100
TMP_SIZE     := 100m
WORK_SIZE    := 500m
PROXY_SOCK   := /var/run/proxy.sock
BASE_URL     := http+unix:///var/run/proxy.sock

.PHONY: build run run-proxied verify-gvisor clean help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

build: ## Build the agent image
	docker build -t $(IMAGE) .

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

run-proxied: build ## Run agent with Unix socket proxy (network-isolated)
	docker run \
		--runtime=$(RUNTIME) \
		--rm \
		--cap-drop ALL \
		--security-opt no-new-privileges \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,size=$(TMP_SIZE) \
		--tmpfs /workspace:rw,noexec,nosuid,size=$(WORK_SIZE) \
		--network none \
		--memory $(MEMORY) \
		--cpus $(CPUS) \
		--pids-limit $(PIDS_LIMIT) \
		--user 1000:1000 \
		-v $(PROXY_SOCK):$(PROXY_SOCK):ro \
		-e ANTHROPIC_BASE_URL="$(BASE_URL)" \
		-e ANTHROPIC_API_KEY="proxied" \
		$(IMAGE)

verify-gvisor: build ## Verify gVisor runtime via dmesg output
	docker run --runtime=$(RUNTIME) --rm --entrypoint python $(IMAGE) \
		-c "import subprocess; print(subprocess.check_output(['dmesg']).decode()[:200])"

clean: ## Remove the agent image
	docker rmi $(IMAGE) 2>/dev/null || true
