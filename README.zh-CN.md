# cubesandbox-agent-adapter

[English](README.md) | [简体中文](README.zh-CN.md)

这是一个社区集成项目，用于把 OpenClaw、DeepSeek Harness（DSH）和 Hermes
Agent 的工具调用，经受控策略路由到
[CubeSandbox](https://github.com/TencentCloud/CubeSandbox) MicroVM 中执行。

```text
OpenClaw Tool Plugin ─┐
DSH Cordis Plugin ────┼─ 认证 HTTP ─→ Adapter ─→ Cube SDK ─→ MicroVM
Hermes Tool Plugin ───┘                          │
                                                └─ 脱敏 JSONL 审计
```

Adapter 是唯一持有 Cube 连接配置、完整 Sandbox ID 和流量令牌的组件。Runtime
插件只获得不透明租约；返回给模型的是短 Sandbox 引用，不包含底层凭据。

> **项目状态：** `v0.2.0` 是已经跑通的参考实现，但还不是可直接承载生产多租户
> 的控制面。向不可信用户开放前，请先阅读[安全边界与当前限制](#安全边界与当前限制)。

## 实战证据

下面不是产品效果图，而是 OpenClaw、DSH、Hermes Agent、Adapter 和
Kubernetes 上的 CubeSandbox 实际联调截图。它们证明的是一次功能实验已经跑通，
不代表性能基准或生产就绪。

### OpenClaw 直接调用 Adapter

本次明确禁止读取 Skill 和使用宿主 `exec`。模型只调用 Adapter 注册的
`cube_exec` 与 `cube_release`；结果中的执行器是 `cubesandbox-microvm`，只向
模型暴露短引用 `45a28df5`：

![OpenClaw 调用 CubeSandbox Adapter 的真实工具结果](docs/assets/readme/openclaw-direct-result.jpg)

### DSH 通过 Cordis Plugin 调用 Adapter

DSH 的完整轨迹记录了 `cube_exec`、`cube_release`、执行结果和短引用
`f795f7fc`。Profile Patch 同时禁用了常见宿主 Shell/FS 工具，避免同一轮任务一半
在宿主机、一半在 MicroVM：

![DSH 通过 Cordis Plugin 调用 CubeSandbox 的完整轨迹](docs/assets/readme/dsh-direct-trace.jpg)

### CubeSandbox WebUI 中的运行实例

DSH 命令保持运行期间，CubeSandbox 沙箱页面能看到同一个 `f795f7...` MicroVM
处于 `running`。截图只保留状态和省略后的 ID，节点、内网地址及完整 ID 均未公开：

![CubeSandbox WebUI 中正在运行的 MicroVM](docs/assets/readme/cubesandbox-live-sandbox.jpg)

### Adapter 脱敏审计

Adapter 审计页可以按 Runtime、动作、Request ID 和短 Sandbox 引用交叉验证
OpenClaw 与 DSH 的操作。它不记录命令正文、输出、令牌或完整 Sandbox ID：

![Cube Adapter 中的 OpenClaw 与 DSH 脱敏审计事件](docs/assets/readme/adapter-audit.jpg)

### Hermes Agent 原生插件实战

Hermes 接入采用独立的原生 Tool Plugin，不修改 Hermes 核心代码。Hermes 0.20.6
官方 Dashboard 显示 `cube-adapter-tools` 来自用户插件目录，状态为 `enabled`，并且
安装时显式拒绝覆盖内置工具：

![Hermes Agent 中已启用的 CubeSandbox 原生插件](docs/assets/readme/hermes-plugin-enabled.jpg)

本次实战会话共有 8 条消息、3 次工具调用。Hermes 默认会压缩工具目录，延迟加载的
插件工具可能先通过 `tool_describe` 与 `tool_call` 暴露给模型，最终仍会落到插件的
`cube_exec` 和 `cube_release` Handler：

![Hermes Agent 调用 CubeSandbox 的会话与工具记录](docs/assets/readme/hermes-session-tools.jpg)

60 秒命令执行期间，CubeSandbox WebUI 出现同一条 `3b287c...` MicroVM，状态为
`running`，规格为 2C/2GiB：

![Hermes 触发的 CubeSandbox MicroVM 正在运行](docs/assets/readme/hermes-cubesandbox-live.jpg)

命令结束后，审计页使用相同短引用 `3b287c8f` 串起 `acquire`、`exec` 与
`release`；Runtime 为 `hermes`，结果均为 `ok`，活动租约归零：

![Hermes 调用 CubeSandbox 的完整脱敏审计链](docs/assets/readme/hermes-adapter-audit.jpg)

验收时不要只看 Agent 最终回复。建议同时核对以下证据链：

```text
Agent 工具结果中的 sandbox_ref
       = CubeSandbox WebUI 中的运行实例短引用
       = Adapter JSONL 审计中的 Sandbox 引用
```

执行 `cube_release(action=kill)` 后，还应确认 WebUI 运行实例归零，并且异常路径
同样会清理租约。

## 项目包含什么

- 使用 `cubesandbox==0.7.0` 的带认证 Python Adapter；
- 默认拒绝公网的 `offline-code` 固定策略；
- OpenClaw Tool Plugin：`cube_exec`、`cube_read`、`cube_write`、`cube_release`；
- DSH Cordis Plugin，以及禁用常见宿主 Shell/FS 工具的 Profile Patch；
- 通过官方 Plugin Doctor 校验的 Hermes Agent 原生 Tool Plugin；
- Kubernetes、OpenClaw、DSH 和 Hermes Agent 一键安装脚本；
- 本地开发用 Docker Compose；
- Helm Chart、纯 Kubernetes Manifest、测试和镜像发布流水线；
- 追加写入、默认脱敏的 JSONL 审计事件。

## 前置条件

安装前请确认：

1. CubeSandbox 已经运行，且 `agent-code` 一类模板 alias 处于 `READY`；
2. Adapter 能访问 CubeAPI 和 CubeProxy；
3. 目标 OpenClaw、DSH 或 Hermes Runtime 能访问 Adapter；
4. 部署到 Kubernetes 时已经安装 `kubectl` 和 `helm`；
5. 安装对应插件时已经安装 `openclaw`、`dsh` 或 `hermes`。

安装器不会安装 CubeSandbox 本身。Kubernetes 节点的 KVM、XFS、bpffs、CNI、
存储和权限要求，请先阅读[部署条件与生产评估](https://aik8s.run/ai-k8s/rag-agent/cubesandbox-kubernetes/)。

Hermes 路径已在 macOS Apple Silicon 的 Hermes Agent 0.20.6 与 CubeSandbox
0.7.0 Kubernetes 实验集群上完成真实联调。

## 一键部署 Kubernetes Adapter

克隆仓库：

```bash
git clone https://github.com/aik8s/cubesandbox-agent-adapter.git
cd cubesandbox-agent-adapter
```

一条命令部署 Adapter：

```bash
./scripts/install.sh adapter \
  --context <kube-context> \
  --cube-api-url http://cube-api.cube-system.svc:3000 \
  --cube-proxy-host cube-proxy.cube-system.svc \
  --cube-proxy-port 80 \
  --template agent-code
```

该命令会：

- 在需要时创建 `agent-runtime` Namespace；
- 如果 Secret 不存在，创建带独立随机 Bearer Token 和 HMAC Key 的
  `cube-adapter-auth`；
- 用 Helm 安装单副本 Adapter；
- 等待 Deployment Ready；
- 全程不打印上述两个 Secret。

可按需覆盖镜像、Namespace 或使用已有 Secret：

```bash
./scripts/install.sh adapter \
  --namespace my-agent-runtime \
  --release cube-adapter \
  --secret existing-adapter-secret \
  --image ghcr.io/aik8s/cubesandbox-agent-adapter:v0.2.0 \
  --cube-api-url https://cube-api.example.internal \
  --cube-api-port 443 \
  --cube-proxy-host cube-proxy.example.internal \
  --cube-proxy-port 443 \
  --template agent-code
```

默认 NetworkPolicy 只接受同一 Namespace 中带以下标签的客户端 Pod：

```yaml
cubesandbox-agent-adapter-client: "true"
```

如果 CubeAPI 或 CubeProxy 不在 `cube-system`，安装前要调整 Chart 的出站策略。
`--disable-network-policy` 只应用在平台已经通过其他方式管理等价网络策略的场景。

完整的 CubeSandbox 集群安装、模板制作和第一个 MicroVM 验收见
[Kubernetes 部署实战](https://aik8s.run/ai-k8s/rag-agent/cubesandbox-kubernetes-practice/)。

## 一键接入 OpenClaw

本地 OpenClaw 可以先端口转发 Adapter：

```bash
kubectl -n agent-runtime port-forward \
  service/cube-agent-adapter-cubesandbox-agent-adapter 18080:18080
```

另开终端，一条命令安装插件、把现有 Bearer Token 导出到权限为 `0600` 的文件、
合并插件/工具 Allowlist，并校验 OpenClaw 配置：

```bash
./scripts/install.sh openclaw \
  --adapter-url http://127.0.0.1:18080 \
  --namespace agent-runtime \
  --token-from-secret cube-adapter-auth
```

然后重启 OpenClaw Gateway。安装器会合并现有的 `plugins.allow` 和
`tools.alsoAllow`，不会覆盖用户原有配置。

当 OpenClaw 运行在 Kubernetes 中时，把同一个 Secret 只读挂载到 Runtime，
然后使用容器内路径：

```bash
./scripts/install.sh openclaw \
  --adapter-url http://cube-agent-adapter-cubesandbox-agent-adapter.agent-runtime.svc:18080 \
  --token-file /var/run/secrets/cube-adapter/token
```

对于不可信 Profile，要另外拒绝宿主 `exec/read/write` 工具。安装 Cube 插件并
不会自动关闭 OpenClaw 的其他执行后端。

## 一键接入 DSH

一条命令安装 Cordis Plugin，并生成权限为 `0600` 的 Profile Patch：

```bash
./scripts/install.sh dsh \
  --adapter-url http://127.0.0.1:18080 \
  --namespace agent-runtime \
  --token-from-secret cube-adapter-auth \
  --profile web
```

生成的 Patch 会禁用 `tool-bash`、`tool-pwsh`、`tool-fs`、
`tool-fs-search` 和 `tool-str-replace-editor`，再注册四个 Cube 工具。可以使用
安装器打印的命令启动 DSH，或安装后立即启动：

```bash
./scripts/install.sh dsh \
  --adapter-url http://127.0.0.1:18080 \
  --token-from-secret cube-adapter-auth \
  --start
```

DSH 的本地 `file:` 安装会把包复制到插件目录。插件源代码变化后要重新执行安装
命令；只重启 DSH 不会刷新旧副本。

## 一键接入 Hermes Agent

一条命令安装并配置独立的 Hermes 原生插件：

```bash
./scripts/install.sh hermes \
  --adapter-url http://127.0.0.1:18080 \
  --namespace agent-runtime \
  --token-from-secret cube-adapter-auth
```

安装器会从本仓库下载插件、在明确拒绝覆盖 Hermes 内置工具的前提下启用它，写入
Adapter 地址、Token 文件路径和 `offline-code` Profile，然后以 CI 模式运行官方
Plugin Doctor。安装后启动一个新的受限会话：

```bash
hermes -t cube-adapter
```

插件注册 `cube_exec`、`cube_read`、`cube_write` 与 `cube_release`。启用 Hermes
工具压缩时，这些工具可能不会直接塞进模型 Prompt，而是先出现在内置的
`tool_describe` / `tool_call` 延迟目录中；这是正常行为，本仓库的真实联调已经覆盖
了这条路径。

安装插件不会全局关闭 Hermes 自带的宿主 Terminal 或文件工具。处理不可信任务时，
应使用 `cube-adapter` Toolset，并在生产会话所用 Profile 或 Gateway 策略中落实同样
的限制。

## 本地 Docker 开发

生成开发 Secret、构建本地镜像并启动 Adapter：

```bash
./scripts/dev-up.sh \
  --cube-api-url http://host.docker.internal:13000 \
  --cube-proxy-host host.docker.internal \
  --cube-proxy-port 13080 \
  --template agent-code

curl -fsS http://127.0.0.1:18080/healthz
```

脚本会创建权限为 `0600` 的 `.env`，默认拒绝覆盖，并且只绑定
`127.0.0.1`。只有明确要轮换两个开发 Secret 时才使用 `--force`。Compose 的
审计目录是临时的。

也可以手动启动：

```bash
cp .env.example .env
# 替换两个 Secret 占位符，并修改 Cube 端点。
chmod 600 .env
docker compose up -d --build
```

## API

```text
GET  /healthz
POST /v1/leases/acquire
POST /v1/leases/{lease_ref}/exec
POST /v1/leases/{lease_ref}/read
POST /v1/leases/{lease_ref}/write
POST /v1/leases/{lease_ref}/release
```

每个 `POST` 都要求 `Authorization: Bearer …`。`acquire` 对
`(runtime, HMAC-SHA-256(session_key))` 幂等。HMAC Key 与 Bearer Token
相互独立，因此常规轮换 Bearer Token 不会改变脱敏后的会话关联值。

模型不能选择模板、CIDR、公开流量或生命周期策略。文件路径仅允许
`/workspace` 和 `/tmp`；请求、命令、文件、输出和超时均有上限。完整契约见
[OpenAPI 文档](docs/openapi.yaml)。

## 审计

审计行包含 Runtime、带密钥的会话摘要、策略、动作、Request ID、短 Sandbox
引用、耗时和结果，不包含：

- Bearer Token 或 traffic token；
- 原始 session key；
- 完整 Sandbox ID；
- 命令正文和文件内容；
- stdout 和 stderr。

可选 `/audit` HTML 页面默认关闭，只用于受保护测试网络的演示。真实部署应把
JSONL 事件发送到持久、访问受控的审计流水线。

## 配置

| 环境变量 | 必填 | 用途 |
| --- | --- | --- |
| `CUBE_ADAPTER_TOKEN` | 是 | Runtime 到 Adapter 的 Bearer Token |
| `CUBE_ADAPTER_HMAC_KEY` | 是 | 独立的会话假名化 Key |
| `CUBE_TEMPLATE_ID` | 是 | 平台维护的 READY 模板 alias |
| `CUBE_API_URL` | 是 | Adapter 可访问的 CubeAPI 地址 |
| `CUBE_PROXY_NODE_IP` | 是 | Adapter 可访问的 CubeProxy Host |
| `CUBE_PROXY_PORT_HTTP` | 是 | CubeProxy HTTP 端口 |
| `CUBE_ADAPTER_AUDIT_LOG` | 否 | JSONL 路径；默认使用本地文件 |
| `CUBE_ADAPTER_AUDIT_UI` | 否 | 仅在受保护测试网络设为 `1` |
| `CUBE_ADAPTER_SANDBOX_TIMEOUT` | 否 | Sandbox 超时，默认 300 秒 |
| `CUBE_ADAPTER_MAX_COMMAND_SECONDS` | 否 | 命令时长上限，默认 120 秒 |

`cubesandbox==0.7.0` 不读取 `CUBE_PROXY_SCHEME`。从其他 SDK 复制连接参数前，
请先核对当前 SDK 版本。

## 测试与验收

```bash
python3 -m venv .venv
.venv/bin/pip install -r adapter/requirements.txt
PYTHON=.venv/bin/python make test
make docker-build
```

端到端验收应证明同一个短 `sandbox_ref` 同时出现在 Agent 工具结果、
CubeSandbox 实时列表和 Adapter 审计中；执行
`cube_release(action=kill)` 后，实时沙箱数量应回到 0。

## 升级与卸载

拉取新版本、重新运行对应安装器，再重启 Runtime：

```bash
git pull --ff-only
./scripts/install.sh openclaw --adapter-url <url> --token-file <path>
```

删除 Kubernetes Release，不删除独立管理的 Secret：

```bash
helm uninstall cube-agent-adapter -n agent-runtime
```

删除 OpenClaw 包前先禁用集成：

```bash
openclaw plugins disable cube-adapter-tools
```

卸载 DSH 集成时，请检查并删除
`${XDG_CONFIG_HOME:-$HOME/.config}/cubesandbox-agent-adapter` 下生成的 Patch。

删除 Hermes 插件目录前先禁用集成：

```bash
hermes plugins disable cube-adapter-tools
```

## 安全边界与当前限制

- 租约归属只保存在进程内存中，因此 Chart 强制单副本；
- 尚未实现重启恢复、流量令牌的持久加密存储和 Owner Fencing；
- 尚未实现 PTY、流式输出、取消、租户配额和审批回调；
- DSH 当前暴露 `cube_*` 工具，还不是透明的原生 `shell/fs/pty` Provider；
- OpenClaw 当前没有稳定的通用第四种 Sandbox Backend，本项目使用公开的 Tool
  Plugin 接口；
- Hermes 的宿主 Terminal/文件工具与本插件相互独立；CubeSandbox 必须作为唯一
  执行器时，要使用受限 Toolset 或 Profile；
- 生产前应在 Bearer 认证之外增加 mTLS/工作负载身份、授权、限流和集中审计。

漏洞报告和部署建议见 [SECURITY.md](SECURITY.md)。

## CubeSandbox 官方生态入口

CubeSandbox 官方正在通过
[Agent 集成指南征集 Issue](https://github.com/TencentCloud/CubeSandbox/issues/244)
建设 Integrations 目录。贡献要求包括认领框架、可运行 Demo，以及网络隔离、超时和
挂载等 Cube 特性说明。Hermes Agent 可作为 `Others` 类型提交；本仓库已经具备独立
插件、中英文 README、真实截图和可复现测试链路，可直接作为上游集成指南的基础。

其他官方渠道包括
[GitHub Discussions](https://github.com/tencentcloud/CubeSandbox/discussions)、
[Cube 100 生产用户计划](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/cube100.md)，
以及上游 README 持续维护的 Discord 入口。

## aik8s.run 延伸阅读

- [CubeSandbox Kubernetes 部署条件与生产评估](https://aik8s.run/ai-k8s/rag-agent/cubesandbox-kubernetes/)：KVM、PVM、XFS、eBPF、存储、网络和生产阻塞项；
- [CubeSandbox Kubernetes 实战：从节点预检到第一个 MicroVM 沙箱](https://aik8s.run/ai-k8s/rag-agent/cubesandbox-kubernetes-practice/)：真实集群安装、模板构建和生命周期验收；
- [用 CubeSandbox 增强 OpenClaw 与 DSH：企业安全执行面实战](https://aik8s.run/ai-k8s/rag-agent/cubesandbox-openclaw-dsh-enterprise-practice/)：完整 Agent 会话、Plugin/Adapter、策略和审计证据；
- [Agent Sandbox 选型与架构分析](https://aik8s.run/ai-k8s/rag-agent/agent-sandbox-selection/)：CubeSandbox、gVisor、Kata、KubeVirt 与托管 Sandbox 的选型边界。

## 贡献

欢迎 Issue 和 Pull Request。请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，并把
产品专属逻辑放在共享 Adapter 契约之后。

这是社区项目，并非 Tencent Cloud、CubeSandbox、OpenClaw、DeepSeek 或 Nous
Research 的官方项目。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。
