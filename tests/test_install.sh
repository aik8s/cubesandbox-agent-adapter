#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
mkdir -p "$TEST_DIR/bin" "$TEST_DIR/config"
touch "$TEST_DIR/calls.log"
printf 'test-token-with-at-least-24-characters' >"$TEST_DIR/token"
chmod 600 "$TEST_DIR/token"

cat >"$TEST_DIR/bin/openclaw" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'openclaw %s\n' "$*" >>"$TEST_CALLS"
if [[ "${1:-}" == config && "${2:-}" == get ]]; then
  case "${3:-}" in
    plugins.allow) printf '["existing-plugin"]\n' ;;
    tools.alsoAllow) printf '["existing-tool"]\n' ;;
    *) printf '[]\n' ;;
  esac
fi
EOF

cat >"$TEST_DIR/bin/dsh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'dsh %s\n' "$*" >>"$TEST_CALLS"
EOF

cat >"$TEST_DIR/bin/hermes" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'hermes %s\n' "$*" >>"$TEST_CALLS"
EOF

cat >"$TEST_DIR/bin/kubectl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'kubectl %s\n' "$*" >>"$TEST_CALLS"
if [[ "$*" == *" get secret "* ]]; then
  if [[ "${TEST_KUBE_SECRET:-0}" == 1 ]]; then
    if [[ "$*" == *base64decode* ]]; then
      printf 'test-token-from-kubernetes-secret'
    else
      printf 'ok'
    fi
    exit 0
  fi
  exit 1
fi
if [[ "${1:-}" == create && "${2:-}" == namespace ]]; then
  printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n' "${3:-test}"
fi
if [[ "${1:-}" == apply && "${2:-}" == -f && "${3:-}" == - ]]; then
  cat >/dev/null
fi
EOF

cat >"$TEST_DIR/bin/helm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'helm %s\n' "$*" >>"$TEST_CALLS"
EOF

cat >"$TEST_DIR/bin/opencode" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'opencode %s\n' "$*" >>"$TEST_CALLS"
if [[ "${1:-}" == --version ]]; then
  printf '%s\n' "${TEST_OPENCODE_VERSION:-1.18.31}"
fi
EOF

cat >"$TEST_DIR/bin/python-test" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == -c && "${2:-}" == 'import mcp' ]]
EOF
chmod +x "$TEST_DIR/bin/openclaw" "$TEST_DIR/bin/dsh" "$TEST_DIR/bin/hermes" "$TEST_DIR/bin/opencode" "$TEST_DIR/bin/python-test" "$TEST_DIR/bin/kubectl" "$TEST_DIR/bin/helm"

export TEST_CALLS="$TEST_DIR/calls.log"
export XDG_CONFIG_HOME="$TEST_DIR/config"
export PATH="$TEST_DIR/bin:$PATH"

"$ROOT_DIR/scripts/install.sh" adapter \
  --namespace adapter-test \
  --image ghcr.io/aik8s/cubesandbox-agent-adapter:v0.2.0 \
  --cube-api-url https://cube-api.example.test \
  --cube-api-port 443 \
  --cube-proxy-host cube-proxy.example.test \
  --cube-proxy-port 8443 \
  --audit-storage-class test-block \
  --template test-template

grep -F 'helm upgrade --install cube-agent-adapter' "$TEST_CALLS" >/dev/null
grep -F -- '--set networkPolicy.cubeApiPort=443' "$TEST_CALLS" >/dev/null
grep -F -- '--set networkPolicy.cubeProxyPort=8443' "$TEST_CALLS" >/dev/null
grep -F -- '--set-string cube.templateId=test-template' "$TEST_CALLS" >/dev/null
grep -F -- '--set-string audit.persistence.storageClass=test-block' "$TEST_CALLS" >/dev/null

export TEST_KUBE_SECRET=1
"$ROOT_DIR/scripts/install.sh" openclaw \
  --adapter-url http://127.0.0.1:18080 \
  --namespace adapter-test \
  --token-from-secret cube-adapter-auth

EXPORTED_TOKEN="$TEST_DIR/config/cubesandbox-agent-adapter/token"
test "$(cat "$EXPORTED_TOKEN")" = 'test-token-from-kubernetes-secret'

grep -F 'config set plugins.allow ["existing-plugin","cube-adapter-tools"] --strict-json' "$TEST_CALLS" >/dev/null
grep -F 'plugins install ' "$TEST_CALLS" | grep -F -- '--force --accept-capabilities' >/dev/null
grep -F 'config set tools.alsoAllow ["existing-tool","cube_exec","cube_status","cube_read","cube_write","cube_list","cube_job_start","cube_job_status","cube_job_output","cube_job_cancel","cube_checkpoint","cube_rollback","cube_fork","cube_release","cube_task_plan","cube_task_submit","cube_task_status","cube_task_result","cube_task_cancel","cube_task_receipt"] --strict-json' "$TEST_CALLS" >/dev/null
grep -F 'config validate' "$TEST_CALLS" >/dev/null

"$ROOT_DIR/scripts/install.sh" dsh \
  --adapter-url http://127.0.0.1:18080 \
  --token-file "$EXPORTED_TOKEN" \
  --profile web

PATCH_FILE="$TEST_DIR/config/cubesandbox-agent-adapter/dsh-web.patch.yml"
test -f "$PATCH_FILE"
grep -F "adapterUrl: 'http://127.0.0.1:18080'" "$PATCH_FILE" >/dev/null
grep -F "tokenFile: '$EXPORTED_TOKEN'" "$PATCH_FILE" >/dev/null
grep -F 'disabled: true' "$PATCH_FILE" >/dev/null
grep -F "name: '@cubesandbox-agent-adapter/dsh-plugin'" "$PATCH_FILE" >/dev/null

"$ROOT_DIR/scripts/install.sh" hermes \
  --adapter-url http://127.0.0.1:18080 \
  --token-file "$EXPORTED_TOKEN"

grep -F 'hermes plugins install aik8s/cubesandbox-agent-adapter/plugins/hermes --no-enable --force' "$TEST_CALLS" >/dev/null
grep -F 'hermes plugins enable cube-adapter-tools --no-allow-tool-override' "$TEST_CALLS" >/dev/null
grep -F "hermes config set plugins.entries.cube-adapter-tools.settings.adapter_url http://127.0.0.1:18080" "$TEST_CALLS" >/dev/null
grep -F "hermes config set plugins.entries.cube-adapter-tools.settings.token_file $EXPORTED_TOKEN" "$TEST_CALLS" >/dev/null
grep -F 'hermes config set plugins.entries.cube-adapter-tools.settings.profile offline-code' "$TEST_CALLS" >/dev/null
grep -F 'hermes plugins doctor cube-adapter-tools --ci' "$TEST_CALLS" >/dev/null

OPENCODE_CONFIG_FILE="$TEST_DIR/config/opencode/opencode.json"
"$ROOT_DIR/scripts/install.sh" opencode \
  --adapter-url http://127.0.0.1:18080 \
  --token-file "$EXPORTED_TOKEN" \
  --python "$TEST_DIR/bin/python-test" \
  --config-file "$OPENCODE_CONFIG_FILE"

node - "$OPENCODE_CONFIG_FILE" "$ROOT_DIR" "$EXPORTED_TOKEN" <<'EOF'
const { readFileSync, statSync } = require("node:fs");
const [path, root, token] = process.argv.slice(2);
const value = JSON.parse(readFileSync(path, "utf8"));
if (value.mcp.cubesandbox.type !== "local") throw new Error("missing local MCP server");
if (value.mcp.cubesandbox.command[1] !== "-m") throw new Error("invalid MCP command");
if (value.mcp.cubesandbox.environment.PYTHONPATH !== root) throw new Error("invalid repo path");
if (value.mcp.cubesandbox.environment.CUBE_ADAPTER_TOKEN_FILE !== token) throw new Error("invalid token file");
if (value.permission["cubesandbox_*"] !== "deny") throw new Error("missing default deny");
if (value.permission.cubesandbox_cube_task_plan !== "allow") throw new Error("missing trusted task allow");
if (Object.values(value).some((entry) => JSON.stringify(entry).includes("test-token-from-kubernetes-secret"))) {
  throw new Error("raw token leaked into OpenCode config");
}
if ((statSync(path).mode & 0o777) !== 0o600) throw new Error("OpenCode config mode is not 0600");
EOF

TEST_OPENCODE_VERSION='opencode v2.0.8' "$ROOT_DIR/scripts/install.sh" opencode \
  --adapter-url http://127.0.0.1:18080 \
  --token-file "$EXPORTED_TOKEN" \
  --python "$TEST_DIR/bin/python-test" \
  --config-file "$TEST_DIR/config/opencode/opencode-v2.json"

node - "$TEST_DIR/config/opencode/opencode-v2.json" <<'EOF'
const { readFileSync } = require("node:fs");
const value = JSON.parse(readFileSync(process.argv[2], "utf8"));
if (value.mcp.servers.cubesandbox.codemode !== false) throw new Error("missing V2 MCP server");
const allowed = value.permissions.find((rule) => rule.action === "cubesandbox_cube_task_plan");
if (!allowed || allowed.effect !== "allow") throw new Error("missing V2 trusted task allow");
EOF

printf 'installer tests: OK\n'
