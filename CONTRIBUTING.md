# Contributing

Thanks for helping improve cubesandbox-agent-adapter.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r adapter/requirements.txt
PYTHON=.venv/bin/python make test
```

Optional checks:

```bash
make docker-build
helm template test charts/cubesandbox-agent-adapter >/dev/null
```

## Pull requests

- keep policy decisions in the Adapter, not in model-controlled arguments;
- never add real credentials, cluster addresses, full Sandbox IDs or customer
  data to tests, fixtures, screenshots or logs;
- update `docs/openapi.yaml` when the HTTP contract changes;
- update both Runtime plugins when a shared tool response changes;
- add tests for fail-closed policy and redaction behavior;
- document compatibility and migration impact.

Small, reviewable pull requests are preferred. A change that weakens a default
security boundary must be explicit in the title and documentation.

## Release process

1. update `VERSION`, chart `version`/`appVersion` and package versions;
2. run `make test` and `make docker-build`;
3. tag `vX.Y.Z`;
4. verify the release workflow publishes the matching GHCR image;
5. run an end-to-end test against a disposable CubeSandbox environment.
