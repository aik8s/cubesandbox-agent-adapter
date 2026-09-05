# Docker Compose 部署 Adapter

[English](deploy-docker.md) | [返回 README](../README.zh-CN.md)

适合已有 CubeSandbox、希望在电脑或单台服务器上接入 Agent 的用户。本指南使用
`ghcr.io/aik8s/cubesandbox-agent-adapter:v0.4.0` 发布镜像，支持 Linux amd64/arm64，
也可以通过 macOS 上的 Docker Desktop 运行 Linux 容器。

## 1. 先准备后端和工具

Adapter 是连接 Agent 与 CubeSandbox 的服务。本 Compose 文件只部署 Adapter 和可选的
Redis，不安装 CubeSandbox，不创建沙箱模板，也不安装 Agent 应用。

准备 Git、Python 3（仅用于本地生成配置）、Docker 和 Docker Compose v2：

```bash
docker version
docker compose version
```

另外需要一个能够创建沙箱的 CubeSandbox 后端，以及以下信息：

| 配置 | 示例，占位地址必须替换 | 作用 |
| --- | --- | --- |
| `CUBE_API_URL` | `http://cube-api.example.internal:3000` | 创建和管理沙箱 |
| `CUBE_PROXY_NODE_IP` | `cube-proxy.example.internal` | 访问沙箱执行接口；虽然变量名带 IP，也支持主机名 |
| `CUBE_PROXY_PORT_HTTP` | `80` | CubeProxy 的 HTTP 端口 |
| `CUBE_TEMPLATE_ID` | `agent-code` | 已存在、处于 READY 状态的模板；应包含 shell 和可写 `/workspace` |
| `CUBE_API_KEY` | 由后端管理员提供 | 仅在 CubeAPI 启用认证时设置 |

地址必须从 **Adapter 容器内** 可达。Adapter 可以连接裸金属或 Kubernetes 上的
CubeSandbox；运行 Adapter 的机器本身不需要 `/dev/kvm` 或特权容器，虚拟化要求在
CubeSandbox 计算节点上。

如果没有后端，先从 [README 中的 CubeSandbox 部署入口](../README.zh-CN.md#cubesandbox-和-kubernetes-到底是什么关系)
开始，验证能创建沙箱后再继续。Agent 的模型 API 单独配置，以下部署和 E2E 测试不调用模型。

## 2. 下载固定版本并生成密钥

在计划运行 Adapter 的机器上执行。后续 Compose 命令都在仓库根目录运行：

```bash
git clone --branch v0.4.0 --depth 1 https://github.com/aik8s/cubesandbox-agent-adapter.git
cd cubesandbox-agent-adapter
```

以下命令从 `.env.example` 生成 `.env`，写入三个独立随机密钥，不打印密钥；已有 `.env`
时拒绝覆盖。保存好该文件，日后重启和升级继续使用：

```bash
python3 - <<'PY'
import os
import secrets
from pathlib import Path

os.umask(0o077)
content = Path('.env.example').read_text()
content = content.replace('replace-with-openssl-rand-hex-32', secrets.token_hex(32))
content = content.replace('replace-with-an-independent-openssl-rand-hex-32', secrets.token_hex(32))
content = content.replace(
    '# CUBE_ADAPTER_RECEIPT_HMAC_KEY=replace-with-another-openssl-rand-hex-32',
    'CUBE_ADAPTER_RECEIPT_HMAC_KEY=' + secrets.token_hex(32),
)
with Path('.env').open('x') as target:
    target.write(content)
print('Created .env; edit the CubeSandbox endpoints before starting.')
PY
```

用编辑器修改 `.env` 中的四项后端配置，按需取消 `CUBE_API_KEY` 的注释并填写凭据。
示例里的 `host.docker.internal:13000/13080` 只是开发地址，不代表已有服务在监听。
不要将 `.env`、密钥或带密钥的 `docker compose config` 完整输出发到 Issue。

## 3. 确认网络地址填写正确

| CubeSandbox 的位置 | 如何选择地址 |
| --- | --- |
| 远程 Linux 服务器 | 使用容器能够解析、路由到的服务器地址和实际端口 |
| Kubernetes 集群内 | 使用管理员提供的可达入口；集群外 Docker 通常不能直接解析 `.svc` 或访问 ClusterIP |
| 与 Adapter 位于同一 Docker 网络 | 通过共享网络中的服务名连接，需要将服务加入同一网络 |
| Docker Desktop 宿主机 | 可以使用 `host.docker.internal`，但仍需确保后端监听地址和防火墙允许连接 |

容器里的 `127.0.0.1` 指向容器本身。原生 Linux Docker 不保证自动提供
`host.docker.internal`；优先填写容器可达的宿主机地址。如使用 `host-gateway` 映射，
还要让后端监听对应宿主接口；映射不会让仅监听宿主 `127.0.0.1` 的服务自动可达。
同样，默认的 `kubectl port-forward` 仅监听宿主回环地址，不能直接当作 Linux 容器入口。

## 4. 拉取发布镜像并启动

```bash
docker compose config --quiet
docker compose pull adapter
docker compose up -d --no-build adapter
docker compose ps
docker compose exec -T adapter python -m adapter.cube_adapter --version
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
```

版本应为 `0.4.0`。`/healthz` 检查进程存活，`/readyz` 检查后端依赖和模板；等到
Compose 显示 `healthy` 且 `/readyz` 成功后再继续。启动失败时查看：

```bash
docker compose logs --tail=100 adapter
```

仓库 Compose 同时包含 `build:` 配置，`--no-build` 明确使用拉取的镜像。源码开发时才
使用 `scripts/dev-up.sh` 或 `up --build`。相关参数见 [Docker Compose 官方说明](https://docs.docker.com/reference/cli/docker/compose/up/)。

## 5. 不依赖模型，验证真实沙箱链路

下面使用容器已有的 Python 和 Adapter Token 运行仓库 E2E。它会实际创建一个沙箱，
验证执行、文件读写和异步 Job，并在 `finally` 中尝试销毁沙箱：

```bash
docker compose exec -T adapter sh -c '
  export CUBE_E2E_ADAPTER_URL=http://127.0.0.1:18080
  export CUBE_E2E_ADAPTER_TOKEN="$CUBE_ADAPTER_TOKEN"
  export CUBE_E2E_PROFILE=offline-code
  exec python -
' < tests/e2e_real.py
```

退出码为 0 且无异常才算通过；测试成功时默认不打印结果。如果中途断网或释放失败，
到 CubeSandbox 管理端确认并回收测试实例。容器 `healthy` 本身不证明沙箱执行成功。

## 6. 接入一个本地客户端

先将共享 Adapter Token 导出到客户端机器上的私有文件。以下命令适用于客户端与
Compose 位于同一台机器，拒绝覆盖已有 `docker.token`：

```bash
install -d -m 700 "$HOME/.config/cubesandbox-agent-adapter"
(
  umask 077
  set -C
  sed -n 's/^CUBE_ADAPTER_TOKEN=//p' .env > "$HOME/.config/cubesandbox-agent-adapter/docker.token"
)
```

该提取命令适用于本指南生成的、未加引号的单行 Token；复制后不要单独修改其中一份。
以下选择一个已经安装并配置好模型 API 的客户端即可，不需要全部安装：

```bash
# OpenClaw：安装后重启 Gateway
./scripts/install.sh openclaw \
  --adapter-url http://127.0.0.1:18080 \
  --token-file "$HOME/.config/cubesandbox-agent-adapter/docker.token"

# DSH：安装后使用安装器打印的启动命令
./scripts/install.sh dsh \
  --adapter-url http://127.0.0.1:18080 \
  --token-file "$HOME/.config/cubesandbox-agent-adapter/docker.token" \
  --profile web

# Hermes：安装后启动 hermes -t cube-adapter
./scripts/install.sh hermes \
  --adapter-url http://127.0.0.1:18080 \
  --token-file "$HOME/.config/cubesandbox-agent-adapter/docker.token"
```

MCP 客户端参考 [README 的 MCP 配置](../README.zh-CN.md#mcp-stdio-门面)，将 URL
设为上述回环地址，Token 文件设为刚才创建的文件；本地 MCP Python 环境需安装
`adapter/requirements.txt`。这些客户端安装器仍需要各应用自身的 CLI。

提示 Agent：“只使用 CubeSandbox 工具，在沙箱执行 `printf hello`，确认返回值后调用
`cube_release(action=kill)`。”检查真实工具调用及清理结果。客户端自身的宿主工具权限
需要另外管理，安装 Adapter 插件不会自动禁用所有客户端的宿主执行。

## 7. 部署在远程服务器时如何连接

默认只发布宿主机的 `127.0.0.1:18080`。如果 Compose 在服务器、Agent 在笔记本，
可在笔记本建立 SSH 隧道并保持运行：

```bash
ssh -N -L 18080:127.0.0.1:18080 user@adapter-server
```

通过受信渠道将 **Adapter Token 文件**交给客户端，客户端仍连接
`http://127.0.0.1:18080`。不需要把包含 CubeAPI 凭据和签名密钥的整个 `.env` 复制到客户端。
多人长期使用时配置 HTTPS 接入及认证；MCP 客户端拒绝远端明文 HTTP URL。

## 8. 状态、升级和停止

默认是单副本内存状态。审计日志保存在 Compose 命名卷中，但租约、任务、审批和 Receipt
不会因为有审计卷而自动持久化。重建容器前先结束任务并释放沙箱。

需要重启恢复时，在 `.env` 配置 `CUBE_ADAPTER_STATE_BACKEND_URL=redis://redis:6379/0`
和独立的 Fernet `CUBE_ADAPTER_STATE_ENCRYPTION_KEY`，先启动 Redis 再重建 Adapter：

```bash
docker compose --profile ha up -d --wait redis
docker compose --profile ha up -d --no-build adapter
```

Fernet 密钥是 32 个随机字节的 URL-safe Base64 编码；[`.env.example`](../.env.example)
中给出了使用 Python `cryptography` 包生成密钥的命令。保留加密密钥和
Redis 数据备份；从内存切换到 Redis 不会迁移现有内存记录。这里的 `ha` 是 Compose
profile 名称，单机 Redis 加单个 Adapter 不构成跨主机高可用。

升级时先结束任务、备份 `.env` 和持久数据，再按目标版本说明更新 checkout 和
`compose.yaml` 的固定镜像版本，执行 `pull adapter` 与 `up -d --no-build adapter`，
重新检查版本、就绪状态和 E2E。不要在升级时重新生成密钥；回退旧镜像前检查状态兼容性。

```bash
docker compose stop adapter
# 恢复运行
docker compose start adapter
# 移除本 Compose 项目的容器和网络，保留命名卷
docker compose down
```

若启动过 Redis，停止整个项目时使用 `docker compose --profile ha down`。
`down -v` 会删除命名卷中的审计和 Redis 数据，常规停止与升级不要使用该参数。

## 9. 下一步：可信任务与常见问题

v0.4.0 的训练、清洗、独立审批需要另外配置任务模板、Profile 和分角色身份。
不要只取消 `.env` 的 TaskTemplate 注释：默认 `config/profiles.yaml` 不包含示例所需的
`trusted-training` / `trusted-data-cleaning` Profile。还需准备对应 READY 沙箱模板，
将任务脚本放在 **沙箱模板内**，并只读挂载 Principal 配置到 Adapter；容器 UID 65532
必须能够读取挂载文件。具体契约见[可信执行示例](../examples/trusted-execution/README.md)
和[可信执行指南](trusted-execution.zh-CN.md)。

| 现象 | 优先检查 |
| --- | --- |
| 镜像拉取失败或 x509 错误 | Docker daemon 的仓库访问与 CA 信任；受限环境可同步同一 digest 的镜像到可达仓库，并修改 Compose 镜像引用 |
| `/healthz` 成功、`/readyz` 失败 | 容器内 CubeAPI/CubeProxy 地址、认证、READY 模板和日志 |
| 客户端返回 401 | 客户端 Token 文件是否与当前 `.env` 一致 |
| 提示只允许 loopback HTTP | 本机使用回环 URL；远程使用 SSH 隧道或 HTTPS |
| 容器 healthy，但任务执行失败 | 运行第 5 步 E2E，检查 CubeSandbox 计算节点和模板内运行环境 |
| 端口已被占用 | 选择空闲宿主端口，例如将映射改为 `127.0.0.1:18081:18080`，同步修改客户端 URL |
