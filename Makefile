ROUTER_BIN ?= router/target/release/multihull
E2E_ARGS ?= -q --timeout 600

.PHONY: e2e e2e-quick router-release mock-image

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
