#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_IMAGE="ghcr.io/aik8s/cubesandbox-agent-adapter:v0.3.0"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

note() {
  printf '==> %s\n' "$*"
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

usage() {
  cat <<'EOF'
Usage:
  scripts/install.sh adapter --cube-api-url URL --cube-proxy-host HOST [options]
  scripts/install.sh openclaw --adapter-url URL (--token-file FILE | --token-from-secret NAME)
  scripts/install.sh dsh --adapter-url URL (--token-file FILE | --token-from-secret NAME) [options]
  scripts/install.sh hermes --adapter-url URL (--token-file FILE | --token-from-secret NAME)

Adapter options:
  --namespace NAME          Kubernetes namespace (default: agent-runtime)
  --release NAME            Helm release (default: cube-agent-adapter)
  --image IMAGE:TAG         Adapter image
  --cube-api-url URL        CubeAPI URL visible from the Adapter Pod
  --cube-api-port PORT      CubeAPI NetworkPolicy port (default: 3000)
  --cube-proxy-host HOST    CubeProxy host visible from the Adapter Pod
  --cube-proxy-port PORT    CubeProxy HTTP port (default: 80)
  --template ID             READY CubeSandbox template alias (default: agent-code)
  --secret NAME             Existing/generated Secret (default: cube-adapter-auth)
  --cube-api-secret NAME    Secret containing CubeAPI key
  --cube-api-key-key KEY    CubeAPI Secret key (default: cube-api-key)
  --replicas COUNT          Adapter replicas (default: 1; HA requires Redis)
  --redis-secret NAME       Secret with redis-url and state-encryption-key
  --context NAME            kubectl context
  --disable-network-policy  Disable the chart's default same-namespace policy

The adapter command creates a strong Secret only when it does not already
exist. It never prints the token. The OpenClaw, DSH and Hermes commands require
an existing read-only token file that is visible to the corresponding Runtime,
or can export one from Kubernetes with --token-from-secret, --namespace and
optional --context.
EOF
}

validate_url() {
  case "$1" in
    http://*|https://*) ;;
    *) die "URL must start with http:// or https://: $1" ;;
  esac
}

require_token_file() {
  local path="$1"
  [[ -f "$path" ]] || die "token file not found: $path"
  [[ -r "$path" ]] || die "token file is not readable: $path"
}

token_from_secret() {
  local namespace="$1"
  local secret_name="$2"
  local context="$3"
  local output="$4"
  need kubectl
  local -a kube=(kubectl)
  [[ -z "$context" ]] || kube+=(--context "$context")
  install -d -m 700 "$(dirname "$output")"
  umask 077
  "${kube[@]}" -n "$namespace" get secret "$secret_name" \
    -o go-template='{{index .data "token" | base64decode}}' >"$output"
  chmod 600 "$output"
  note "exported Adapter token to $output"
}

merge_openclaw_array() {
  local path="$1"
  shift
  local current merged
  if ! current="$(openclaw config get "$path" --json 2>/dev/null)"; then
    current='[]'
  fi
  merged="$(node -e '
    const current = JSON.parse(process.argv[1] || "[]");
    if (!Array.isArray(current)) throw new Error("config path is not an array");
    process.stdout.write(JSON.stringify([...new Set([...current, ...process.argv.slice(2)])]));
  ' "$current" "$@")"
  openclaw config set "$path" "$merged" --strict-json >/dev/null
}

install_adapter() {
  local namespace="agent-runtime"
  local release="cube-agent-adapter"
  local image="$DEFAULT_IMAGE"
  local cube_api_url=""
  local cube_api_port="3000"
  local cube_proxy_host=""
  local cube_proxy_port="80"
  local template_id="agent-code"
  local secret_name="cube-adapter-auth"
  local cube_api_secret=""
  local cube_api_key_key="cube-api-key"
  local replicas="1"
  local redis_secret=""
  local context=""
  local network_policy="true"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --namespace) namespace="${2:?}"; shift 2 ;;
      --release) release="${2:?}"; shift 2 ;;
      --image) image="${2:?}"; shift 2 ;;
      --cube-api-url) cube_api_url="${2:?}"; shift 2 ;;
      --cube-api-port) cube_api_port="${2:?}"; shift 2 ;;
      --cube-proxy-host) cube_proxy_host="${2:?}"; shift 2 ;;
      --cube-proxy-port) cube_proxy_port="${2:?}"; shift 2 ;;
      --template) template_id="${2:?}"; shift 2 ;;
      --secret) secret_name="${2:?}"; shift 2 ;;
      --cube-api-secret) cube_api_secret="${2:?}"; shift 2 ;;
      --cube-api-key-key) cube_api_key_key="${2:?}"; shift 2 ;;
      --replicas) replicas="${2:?}"; shift 2 ;;
      --redis-secret) redis_secret="${2:?}"; shift 2 ;;
      --context) context="${2:?}"; shift 2 ;;
      --disable-network-policy) network_policy="false"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown adapter option: $1" ;;
    esac
  done

  [[ -n "$cube_api_url" ]] || die "--cube-api-url is required"
  [[ -n "$cube_proxy_host" ]] || die "--cube-proxy-host is required"
  validate_url "$cube_api_url"
  [[ "$cube_proxy_port" =~ ^[0-9]+$ ]] || die "--cube-proxy-port must be numeric"
  [[ "$cube_api_port" =~ ^[0-9]+$ ]] || die "--cube-api-port must be numeric"
  [[ "$replicas" =~ ^[1-9][0-9]*$ ]] || die "--replicas must be a positive integer"
  if (( replicas > 1 )) && [[ -z "$redis_secret" ]]; then
    die "--replicas greater than 1 requires --redis-secret"
  fi
  [[ "$image" == *:* ]] || die "--image must include an explicit tag"

  need kubectl
  need helm
  need openssl

  local -a kube=(kubectl)
  local -a helm_cmd=(helm)
  if [[ -n "$context" ]]; then
    kube+=(--context "$context")
    helm_cmd+=(--kube-context "$context")
  fi

  note "creating namespace if needed"
  "${kube[@]}" create namespace "$namespace" --dry-run=client -o yaml | "${kube[@]}" apply -f - >/dev/null

  if ! "${kube[@]}" -n "$namespace" get secret "$secret_name" >/dev/null 2>&1; then
    note "creating Adapter bearer and HMAC keys"
    local secret_tmp
    secret_tmp="$(mktemp -d)"
    umask 077
    printf '%s' "$(openssl rand -hex 32)" >"$secret_tmp/token"
    printf '%s' "$(openssl rand -hex 32)" >"$secret_tmp/hmac-key"
    "${kube[@]}" -n "$namespace" create secret generic "$secret_name" \
      --from-file=token="$secret_tmp/token" \
      --from-file=hmac-key="$secret_tmp/hmac-key" >/dev/null
    rm -f "$secret_tmp/token" "$secret_tmp/hmac-key"
    rmdir "$secret_tmp"
  else
    note "reusing existing Secret $namespace/$secret_name"
    [[ "$("${kube[@]}" -n "$namespace" get secret "$secret_name" -o go-template='{{if index .data "token"}}ok{{end}}')" == ok ]] \
      || die "existing Secret is missing key: token"
    [[ "$("${kube[@]}" -n "$namespace" get secret "$secret_name" -o go-template='{{if index .data "hmac-key"}}ok{{end}}')" == ok ]] \
      || die "existing Secret is missing key: hmac-key"
  fi

  if [[ -n "$cube_api_secret" ]]; then
    [[ "$("${kube[@]}" -n "$namespace" get secret "$cube_api_secret" -o go-template="{{if index .data \"$cube_api_key_key\"}}ok{{end}}")" == ok ]] \
      || die "CubeAPI Secret is missing key: $cube_api_key_key"
  fi
  if [[ -n "$redis_secret" ]]; then
    [[ "$("${kube[@]}" -n "$namespace" get secret "$redis_secret" -o go-template='{{if index .data "redis-url"}}ok{{end}}')" == ok ]] \
      || die "Redis Secret is missing key: redis-url"
    [[ "$("${kube[@]}" -n "$namespace" get secret "$redis_secret" -o go-template='{{if index .data "state-encryption-key"}}ok{{end}}')" == ok ]] \
      || die "Redis Secret is missing key: state-encryption-key"
  fi

  local image_repo="${image%:*}"
  local image_tag="${image##*:}"
  note "installing Adapter with Helm"
  local -a helm_values=(
    --set-string image.repository="$image_repo"
    --set-string image.tag="$image_tag"
    --set-string auth.existingSecret="$secret_name"
    --set-string cube.apiUrl="$cube_api_url"
    --set networkPolicy.cubeApiPort="$cube_api_port"
    --set-string cube.proxyHost="$cube_proxy_host"
    --set cube.proxyPort="$cube_proxy_port"
    --set networkPolicy.cubeProxyPort="$cube_proxy_port"
    --set-string cube.templateId="$template_id"
    --set networkPolicy.enabled="$network_policy"
    --set replicaCount="$replicas"
  )
  if [[ -n "$cube_api_secret" ]]; then
    helm_values+=(
      --set-string cube.existingSecret="$cube_api_secret"
      --set-string cube.apiKeyKey="$cube_api_key_key"
    )
  fi
  if [[ -n "$redis_secret" ]]; then
    helm_values+=(
      --set state.enabled=true
      --set-string state.existingSecret="$redis_secret"
    )
  fi
  "${helm_cmd[@]}" upgrade --install "$release" "$ROOT_DIR/charts/cubesandbox-agent-adapter" \
    --namespace "$namespace" \
    "${helm_values[@]}" \
    --wait --timeout 5m

  note "installed"
  printf 'Adapter service: http://%s-cubesandbox-agent-adapter.%s.svc:18080\n' "$release" "$namespace"
  printf 'Secret:          %s/%s (not printed)\n' "$namespace" "$secret_name"
}

install_openclaw() {
  local adapter_url=""
  local token_file=""
  local token_secret=""
  local namespace="agent-runtime"
  local context=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --adapter-url) adapter_url="${2:?}"; shift 2 ;;
      --token-file) token_file="${2:?}"; shift 2 ;;
      --token-from-secret) token_secret="${2:?}"; shift 2 ;;
      --namespace) namespace="${2:?}"; shift 2 ;;
      --context) context="${2:?}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown OpenClaw option: $1" ;;
    esac
  done
  [[ -n "$adapter_url" ]] || die "--adapter-url is required"
  if [[ -n "$token_file" && -n "$token_secret" ]]; then
    die "use either --token-file or --token-from-secret"
  fi
  if [[ -n "$token_secret" ]]; then
    token_file="${XDG_CONFIG_HOME:-$HOME/.config}/cubesandbox-agent-adapter/token"
    token_from_secret "$namespace" "$token_secret" "$context" "$token_file"
  fi
  [[ -n "$token_file" ]] || die "--token-file or --token-from-secret is required"
  validate_url "$adapter_url"
  require_token_file "$token_file"
  need openclaw
  need node

  token_file="$(cd "$(dirname "$token_file")" && pwd)/$(basename "$token_file")"
  note "installing OpenClaw Tool Plugin"
  # OpenClaw requires an explicit trust acknowledgement for local sources.
  # This path is the reviewed plugin bundled in the current checkout.
  openclaw plugins install "$ROOT_DIR/plugins/openclaw" --force --accept-capabilities
  openclaw plugins enable cube-adapter-tools >/dev/null
  openclaw config set plugins.entries.cube-adapter-tools.config.adapterUrl "$adapter_url" >/dev/null
  openclaw config set plugins.entries.cube-adapter-tools.config.tokenFile "$token_file" >/dev/null
  openclaw config set plugins.entries.cube-adapter-tools.config.profile offline-code >/dev/null
  merge_openclaw_array plugins.allow cube-adapter-tools
  merge_openclaw_array tools.alsoAllow \
    cube_exec cube_status cube_read cube_write cube_list \
    cube_job_start cube_job_status cube_job_output cube_job_cancel \
    cube_checkpoint cube_rollback cube_fork cube_release
  openclaw config validate
  note "OpenClaw integration installed; restart the Gateway"
}

yaml_quote() {
  local escaped="${1//\'/\'\'}"
  printf "'%s'" "$escaped"
}

install_dsh() {
  local adapter_url=""
  local token_file=""
  local token_secret=""
  local namespace="agent-runtime"
  local context=""
  local profile="web"
  local start="false"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --adapter-url) adapter_url="${2:?}"; shift 2 ;;
      --token-file) token_file="${2:?}"; shift 2 ;;
      --token-from-secret) token_secret="${2:?}"; shift 2 ;;
      --namespace) namespace="${2:?}"; shift 2 ;;
      --context) context="${2:?}"; shift 2 ;;
      --profile) profile="${2:?}"; shift 2 ;;
      --start) start="true"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown DSH option: $1" ;;
    esac
  done
  [[ -n "$adapter_url" ]] || die "--adapter-url is required"
  if [[ -n "$token_file" && -n "$token_secret" ]]; then
    die "use either --token-file or --token-from-secret"
  fi
  if [[ -n "$token_secret" ]]; then
    token_file="${XDG_CONFIG_HOME:-$HOME/.config}/cubesandbox-agent-adapter/token"
    token_from_secret "$namespace" "$token_secret" "$context" "$token_file"
  fi
  [[ -n "$token_file" ]] || die "--token-file or --token-from-secret is required"
  validate_url "$adapter_url"
  require_token_file "$token_file"
  need dsh

  token_file="$(cd "$(dirname "$token_file")" && pwd)/$(basename "$token_file")"
  note "installing DSH Cordis Plugin into profile $profile"
  dsh plugin --profile "$profile" add "$ROOT_DIR/plugins/dsh"

  local config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/cubesandbox-agent-adapter"
  local patch_file="$config_dir/dsh-${profile}.patch.yml"
  install -d -m 700 "$config_dir"
  umask 077
  cat >"$patch_file" <<EOF
- id: tool-bash
  disabled: true
- id: tool-fs
  disabled: true
- id: tool-fs-search
  disabled: true
- id: tool-str-replace-editor
  disabled: true
- id: tool-pwsh
  disabled: true
- insert:
    - id: cube-adapter-tools
      name: '@cubesandbox-agent-adapter/dsh-plugin'
      config:
        adapterUrl: $(yaml_quote "$adapter_url")
        tokenFile: $(yaml_quote "$token_file")
        profile: offline-code
EOF
  chmod 600 "$patch_file"
  note "DSH integration installed; patch: $patch_file"
  if [[ "$start" == "true" ]]; then
    exec dsh web --patch "$patch_file"
  fi
  printf 'Start DSH with: dsh web --patch %q\n' "$patch_file"
}

install_hermes() {
  local adapter_url=""
  local token_file=""
  local token_secret=""
  local namespace="agent-runtime"
  local context=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --adapter-url) adapter_url="${2:?}"; shift 2 ;;
      --token-file) token_file="${2:?}"; shift 2 ;;
      --token-from-secret) token_secret="${2:?}"; shift 2 ;;
      --namespace) namespace="${2:?}"; shift 2 ;;
      --context) context="${2:?}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown Hermes option: $1" ;;
    esac
  done
  [[ -n "$adapter_url" ]] || die "--adapter-url is required"
  if [[ -n "$token_file" && -n "$token_secret" ]]; then
    die "use either --token-file or --token-from-secret"
  fi
  if [[ -n "$token_secret" ]]; then
    token_file="${XDG_CONFIG_HOME:-$HOME/.config}/cubesandbox-agent-adapter/token"
    token_from_secret "$namespace" "$token_secret" "$context" "$token_file"
  fi
  [[ -n "$token_file" ]] || die "--token-file or --token-from-secret is required"
  validate_url "$adapter_url"
  require_token_file "$token_file"
  need hermes

  token_file="$(cd "$(dirname "$token_file")" && pwd)/$(basename "$token_file")"
  note "installing the Hermes Agent native Tool Plugin"
  hermes plugins install \
    aik8s/cubesandbox-agent-adapter/plugins/hermes \
    --no-enable --force
  hermes plugins enable cube-adapter-tools --no-allow-tool-override
  hermes config set \
    plugins.entries.cube-adapter-tools.settings.adapter_url "$adapter_url" >/dev/null
  hermes config set \
    plugins.entries.cube-adapter-tools.settings.token_file "$token_file" >/dev/null
  hermes config set \
    plugins.entries.cube-adapter-tools.settings.profile offline-code >/dev/null
  hermes plugins doctor cube-adapter-tools --ci
  note "Hermes integration installed; start a new Hermes session"
}

main() {
  local component="${1:-}"
  [[ -n "$component" ]] || { usage; exit 1; }
  shift
  case "$component" in
    adapter) install_adapter "$@" ;;
    openclaw) install_openclaw "$@" ;;
    dsh) install_dsh "$@" ;;
    hermes) install_hermes "$@" ;;
    -h|--help|help) usage ;;
    *) die "unknown component: $component" ;;
  esac
}

main "$@"
