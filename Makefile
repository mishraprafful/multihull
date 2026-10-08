ROUTER_BIN ?= router/target/release/multihull
E2E_ARGS ?= -q --timeout 600
KIND_CLUSTER ?= multihull-live
IN_CLUSTER ?= multihull-in-cluster
LIVE_ARGS ?= -q -x

.PHONY: e2e e2e-quick e2e-kind kind-up kind-down kind-in-cluster router-release mock-image

router-release:
	cd router && cargo build --release -p multihull

mock-image:
	docker build -t multihull-mock-server:e2e testing/mock-server

e2e: router-release
	cd testing/e2e && E2E_ROUTER_BIN=$(abspath $(ROUTER_BIN)) uv run pytest $(E2E_ARGS)

ifdef E2E_ROUTER_BIN
e2e-quick:
	cd testing/e2e && uv run pytest $(E2E_ARGS)
else
e2e-quick: e2e
endif

kind-up:
	kind create cluster --name $(KIND_CLUSTER) --config testing/live/kind-config.yaml

kind-down:
	kind delete cluster --name $(KIND_CLUSTER)

e2e-kind: router-release
	cd testing/live && LIVE_KIND_CLUSTER=$(KIND_CLUSTER) LIVE_ROUTER_BIN=$(abspath $(ROUTER_BIN)) uv run pytest $(LIVE_ARGS)

kind-in-cluster:
	cd testing/live && LIVE_KIND_CLUSTER=$(IN_CLUSTER) uv run pytest $(LIVE_ARGS) in_cluster
