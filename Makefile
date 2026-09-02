.PHONY: test test-python test-node test-install lint typecheck helm-lint docker-build

PYTHON ?= python3

test: lint test-python test-node test-install helm-lint

test-python:
	$(PYTHON) -m unittest discover -s adapter -p 'test_*.py' -v
	$(PYTHON) -m unittest -v plugins/hermes/test_plugin.py

lint:
	ruff check adapter plugins/hermes
	node --check plugins/openclaw/index.js
	node --check plugins/dsh/index.js
	bash -n scripts/install.sh scripts/dev-up.sh

typecheck:
	mypy adapter plugins/hermes

test-node:
	node --check plugins/openclaw/index.js
	node --check plugins/dsh/index.js
	node --loader ./plugins/openclaw/test-loader.mjs plugins/openclaw/test-plugin.mjs
	node plugins/dsh/test-plugin.mjs

test-install:
	bash tests/test_install.sh

helm-lint:
	helm lint charts/cubesandbox-agent-adapter
	helm template test charts/cubesandbox-agent-adapter >/dev/null

docker-build:
	docker build -t cubesandbox-agent-adapter:dev adapter
