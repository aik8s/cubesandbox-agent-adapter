# 办公网 Agent 访问生产 CubeSandbox

本文说明办公网 Kubernetes 中的 OpenClaw、DSH、Hermes、Codex、Claude Code 或
OpenCode，如何通过 Adapter 使用生产网络里的 CubeSandbox，以及当前多集群支持边界。

## 当前支持情况

| 场景 | 当前状态 |
| --- | --- |
| Agent 和 Adapter 在办公网，访问一个生产 CubeSandbox | 支持，但 Adapter 必须同时访问 CubeAPI 和 CubeProxy |
| Agent 获取生产沙箱执行结果 | 支持；结果经 CubeProxy 返回 Adapter，再由 Adapter 返回 Agent |
| Agent 直接访问沙箱 IP | 不需要，也不应成为默认网络路径 |
| 多个 Adapter Deployment 分别绑定多个 CubeSandbox 集群 | 支持，是当前推荐方式 |
| 一个 Adapter 进程按 Profile/任务路由多个 CubeSandbox 集群 | **不支持** |
| OpenClaw 的同一插件配置动态选择多个 Adapter | **不支持**；当前插件配置只有一个 `adapterUrl` 和一个 `profile` |

当前 Helm Chart 和进程配置只有一组 `CUBE_API_URL`、`CUBE_PROXY_NODE_IP`、
`CUBE_PROXY_PORT_HTTP`、`CUBE_API_KEY` 和默认模板。Profile 可以选择模板、网络、
Volume、Checkpoint 与配额，但没有 `backend` 或 `cluster` 字段。因此 Profile 名字即使
写成 `prod-a`，也仍会调用同一个后端。

## 请求和结果如何穿过网络边界

```text
办公网 Agent
    │ HTTPS / mTLS + Adapter 身份
    ▼
办公网 Adapter
    ├── CubeAPI：创建、查询、暂停、快照、Volume、模板检查
    └── CubeProxy：命令、文件、PTY、Job 输出等沙箱数据面
             │
             ▼
      生产 CubeSandbox MicroVM
```

只把 CubeAPI 加入白名单通常不够：创建沙箱可能成功，但 `exec`、文件、PTY 和 Job
读取会因为 CubeProxy 不可达而失败。办公网不需要生产沙箱的单独 IP；SDK 使用
CubeProxy 和沙箱路由信息找到数据面。异步任务的 stdout、stderr 和退出码保存在沙箱
中，Adapter 通过同一数据面轮询并按策略返回。

生产侧的对象存储、数据库和 Redis 是 CubeSandbox 自身的后端依赖，不需要直接暴露给
办公网 Adapter。训练数据也不建议经过 Agent 文本或 Adapter 请求体传输，应由生产
沙箱使用短期工作负载身份访问受控数据源。

## 当前推荐的多集群部署

为每个生产 CubeSandbox 后端部署一套独立 Adapter，例如：

```text
cube-adapter-prod-a -> CubeAPI/CubeProxy A
cube-adapter-prod-b -> CubeAPI/CubeProxy B
```

每套 Deployment 使用独立的：

- CubeAPI Key、TLS/mTLS 证书和 NetworkPolicy；
- `state.prefix`、Redis 加密密钥和审计存储；
- Profile、TaskTemplate、模板 alias 和调用方 Principal；
- 指标标签、告警、发布节奏和故障域。

这能避免沙箱 ID、Snapshot ID、Volume ID、Session Key 和凭据跨集群混用。对于单个
OpenClaw 实例，当前插件只能配置一个 Adapter；可先按生产域拆分 OpenClaw Agent，或
在前面增加一个仅按服务端策略路由的网关。不要让模型提交任意 URL、集群地址或凭据。
Codex、Claude Code 和 OpenCode 等 MCP 客户端可以注册多个不同名称的 MCP Server，
每个 Server 固定指向一套 Adapter，但仍应通过权限策略限制可见工具。

## 更安全的生产拓扑

安全边界更强的做法是把 Adapter 部署到生产侧，仅把 Adapter 的窄 API 暴露给办公网：

```text
办公网 Agent -> API Gateway / mTLS -> 生产侧 Adapter -> 本地 CubeAPI/CubeProxy
```

这样生产 CubeAPI Key 不离开生产域，办公网也不直接获得 CubeAPI/CubeProxy 网络权限。
如果 Adapter 必须部署在办公网，至少需要：

1. 专线、VPN、PrivateLink 或等价私网路由；
2. Adapter Pod 使用固定出口 NAT，生产入口只允许该来源；
3. CubeAPI 与 CubeProxy 都使用 TLS，优先 mTLS；
4. 每个集群独立凭据，定期轮换，不使用共享管理员 Key；
5. 标准 NetworkPolicy 使用明确的 `ipBlock` 和端口；需要 FQDN 策略时使用 Cilium
   等支持 DNS/FQDN Egress 的实现；
6. 禁止 Agent 获得直连生产端点、原始 API Key 或任意 egress；
7. Adapter 使用 OIDC/细粒度 Principal、TaskTemplate、审批、强审计和输出白名单；
8. 对 CubeAPI、CubeProxy、Adapter、Redis 状态和审计 Sink 分别设置连通性与延迟告警。

## 单 Adapter 多后端还缺什么

实现单进程安全路由不能只增加一个 `cluster` 请求字段，至少还需要：

- 后端注册表：`backend_id`、CubeAPI/CubeProxy、模板、TLS、凭据 Secret 引用和健康状态；
- 服务端路由：Profile/TaskTemplate 固定绑定 `backend_id`，调用方只能选择被授权的
  Profile，不能提交端点；
- SDK 客户端池：每个后端独立的 `cubesandbox.Config`，所有 Sandbox、Template、
  Volume、Snapshot 和 reconnect 调用必须携带正确配置；
- 状态模型：Lease、Checkpoint、Job、Task、Session 索引都持久化 `backend_id`；
  Session 幂等键和配额键也要包含后端，防止跨集群复用；
- 审计与 Receipt：记录不可伪造的后端标识、策略版本和模板摘要，同时继续隐藏地址、
  Key 和完整资源 ID；
- 故障处理：按后端熔断、重试、Readiness、GC、孤儿资源对账和不可达时的故障闭锁；
- Helm/Secret/NetworkPolicy：多组端点与凭据、每后端 egress allowlist、证书轮换；
- 客户端契约：OpenClaw 工具名或服务器端 Profile 路由需要稳定，避免模型自由选择生产域。

当前 `LeaseRecord`、`CheckpointRecord` 和 Session 索引没有 `backend_id`，Readiness 也只
检查一组模板和一组 CubeProxy，因此现状不能安全扩展为单实例多后端。

## 上线前验证

每个目标集群至少验证：创建/释放、命令、文件、长任务输出、断线恢复、超时、权限拒绝、
Snapshot/Rollback/Clone、孤儿清理、凭据轮换、Adapter 重启和审计 Sink 故障。只验证
`/readyz` 或“沙箱创建成功”不能证明数据面和结果返回链路可用。
