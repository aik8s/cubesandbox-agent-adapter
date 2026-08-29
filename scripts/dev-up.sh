#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cube_api_url=""
proxy_host=""
proxy_port="80"
template_id="agent-code"
force="false"

usage() {
  cat <<'EOF'
Usage: scripts/dev-up.sh --cube-api-url URL --cube-proxy-host HOST [options]

Options:
  --cube-proxy-port PORT  default: 80
  --template ID           default: agent-code
  --force                 replace an existing .env with new generated secrets
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cube-api-url) cube_api_url="${2:?}"; shift 2 ;;
    --cube-proxy-host) proxy_host="${2:?}"; shift 2 ;;
    --cube-proxy-port) proxy_port="${2:?}"; shift 2 ;;
    --template) template_id="${2:?}"; shift 2 ;;
    --force) force="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'error: unknown option: %s\n' "$1" >&2; exit 1 ;;
  esac
done

[[ "$cube_api_url" == http://* || "$cube_api_url" == https://* ]] || {
  printf 'error: --cube-api-url must use http:// or https://\n' >&2
  exit 1
}
[[ -n "$proxy_host" ]] || { printf 'error: --cube-proxy-host is required\n' >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { printf 'error: openssl is required\n' >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { printf 'error: docker is required\n' >&2; exit 1; }

env_file="$ROOT_DIR/.env"
if [[ -e "$env_file" && "$force" != "true" ]]; then
  printf 'error: %s already exists; use --force to rotate and replace it\n' "$env_file" >&2
  exit 1
fi

umask 077
cat >"$env_file" <<EOF
CUBE_ADAPTER_BIND=0.0.0.0
CUBE_ADAPTER_PORT=18080
CUBE_ADAPTER_TOKEN=$(openssl rand -hex 32)
CUBE_ADAPTER_HMAC_KEY=$(openssl rand -hex 32)
CUBE_ADAPTER_AUDIT_LOG=/var/log/cube-adapter/audit.jsonl
CUBE_ADAPTER_AUDIT_UI=0
CUBE_TEMPLATE_ID=$template_id
CUBE_API_URL=$cube_api_url
CUBE_PROXY_NODE_IP=$proxy_host
CUBE_PROXY_PORT_HTTP=$proxy_port
EOF
chmod 600 "$env_file"

docker compose -f "$ROOT_DIR/compose.yaml" up -d --build
printf 'Adapter is starting on http://127.0.0.1:18080\n'
printf 'Health check: curl -fsS http://127.0.0.1:18080/healthz\n'
