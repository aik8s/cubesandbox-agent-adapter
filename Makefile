.PHONY: test test-python test-node test-install helm-lint docker-build

PYTHON ?= python3

test: test-python test-node test-install helm-lint

test-python:
	$(PYTHON) -m unittest -v adapter/test_cube_adapter.py plugins/hermes/test_plugin.py

test-node:
	node --check plugins/openclaw/index.js
	node --check plugins/dsh/index.js
	node plugins/dsh/test-plugin.mjs

test-install:
	bash tests/test_install.sh

helm-lint:
	helm lint charts/cubesandbox-agent-adapter
	helm template test charts/cubesandbox-agent-adapter >/dev/null

docker-build:
	docker build -t cubesandbox-agent-adapter:dev adapter
