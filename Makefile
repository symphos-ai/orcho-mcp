PYTHON ?= ./.venv/bin/python

.PHONY: \
	check \
	inspector \
	lint \
	schema-check \
	schema-update \
	smoke-post-restart \
	test \
	test-architecture \
	test-integration \
	test-protocol \
	test-self-discovery \
	test-delivery-gate-smoke

inspector:
	./scripts/mcp_inspector.sh

lint:
	$(PYTHON) -m ruff check .

test:
	$(PYTHON) -m pytest -q

test-architecture:
	$(PYTHON) -m pytest -q tests/unit/architecture

test-protocol:
	$(PYTHON) -m pytest -q tests/integration/protocol

test-integration:
	$(PYTHON) -m pytest -q -m mcp_integration

test-self-discovery:
	$(PYTHON) -m pytest -q \
		tests/acceptance/mock_pipeline/test_self_discovery_flow.py \
		-m mcp_integration

# Opt-in E2E mock smoke for the rejected-FA -> fix -> from_run_plan follow-up
# -> supersede correction path (T4). ``-m mcp_integration`` overrides the
# default addopts ``-m 'not mcp_integration'`` so the gated smoke actually runs.
test-delivery-gate-smoke:
	$(PYTHON) -m pytest -q \
		tests/acceptance/mock_pipeline/test_delivery_gate_smoke.py \
		-m mcp_integration

schema-check:
	$(PYTHON) tools/dump_mcp_schema.py --check

schema-update:
	$(PYTHON) tools/dump_mcp_schema.py

smoke-post-restart:
	$(PYTHON) scripts/post_restart_smoke.py

check: lint schema-check test-architecture test-protocol
	git diff --check
