# k8s-incident-copilot. `make help` lists targets.
CLUSTER  ?= incident-copilot
NS       ?= shop
IMAGE    ?= incident-shop/app:1.0.0
PY       ?= .venv/bin/python
NAME     ?=
VARIANT  ?= 1
FIXTURE  ?= eval/fixtures/dependency-timeout-v2.json.gz
# The image is side-loaded (k3d image import), never pulled. On a host disk above 85% the kubelet's image
# GC deletes it whenever no pod is using it (it did, mid-capture). Dev-box only: relax GC and disk eviction.
KUBELET_ARGS ?= image-gc-high-threshold=100 image-gc-low-threshold=99 eviction-hard=imagefs.available<1%,nodefs.available<1%

.PHONY: help setup up cluster app down incident reset investigate demo eval eval-live capture test lint

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup: ## Python venv + deps
	python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]'

up: setup cluster app ## One command: venv, k3d cluster, app, RBAC identities

cluster: ## Create a 1-node k3d cluster (no Traefik)
	k3d cluster create $(CLUSTER) --servers 1 --agents 0 --wait \
	  --k3s-arg "--disable=traefik@server:0" $(foreach a,$(KUBELET_ARGS),--k3s-arg "--kubelet-arg=$(a)@server:0")

app: ## Build the demo image, import it, install the chart, mint copilot kubeconfigs
	docker build -q -t $(IMAGE) app/
	k3d image import $(IMAGE) -c $(CLUSTER)
	kubectl create namespace $(NS) --dry-run=client -o yaml | kubectl apply -f -
	helm upgrade --install shop charts/shop -n $(NS) --wait
	./scripts/kubeconfigs.sh

down: ## Delete the cluster
	k3d cluster delete $(CLUSTER)

incident: ## Break something: make incident NAME=oom-killed [VARIANT=2]   (list: make incident)
	@if [ -z "$(NAME)" ]; then $(PY) -m copilot.incidents list; else $(PY) -m copilot.incidents inject $(NAME) --variant $(VARIANT); fi

reset: ## Back to a healthy release
	$(PY) -m copilot.incidents reset

investigate: ## Investigate the live namespace (needs GEMINI_API_KEY); asks before changing anything
	$(PY) -m copilot.cli investigate --namespace $(NS) --mode live

demo: ## Replay a captured incident through the full agent (no cluster, no key, no cost)
	$(PY) -m copilot.cli investigate --fixture $(FIXTURE) --mode replay

eval: ## Full eval in recorded-response mode (free, deterministic); rewrites the README table
	$(PY) eval/run_eval.py

eval-live: ## Re-run the eval with live model calls and re-record (costs ~$1; capped by COPILOT_SPEND_CAP_USD)
	COPILOT_LLM_MODE=record $(PY) eval/run_eval.py

capture: ## Re-stage every incident on the cluster and snapshot it (~70 min)
	$(PY) eval/capture.py

test: ## Unit tests (deterministic tools, guard, audit, replayed agent)
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check copilot eval tests scripts
