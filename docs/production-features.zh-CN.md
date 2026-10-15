# 多集群、内部模板与持久审计

本页对应当前源码中的未发布功能。已经发布的 v0.5.0 镜像没有下述新增入口；部署时需要从当前代码构建候选镜像。生产中的现有 Adapter 和审计 PVC 不会因为修改这些配置自动迁移。

## 一个入口，多个执行集群

Agent 只连接一套 Adapter，通过服务端批准的 Profile 选择执行环境。每个 Profile 的 `backend_id` 指向注册的 CubeAPI、CubeProxy、API Key 和 sandbox domain。请求不能传入任意后端地址或密钥。

```text
办公网 Agent → HTTPS Adapter → staging-code       → CubeAPI / CubeProxy A
                            → production-openclaw → CubeAPI / CubeProxy B
                            → staging-mcp        → CubeAPI / CubeProxy A
```

先看 [backends.yaml](../examples/production-features/backends.yaml) 和 [profiles.yaml](../examples/production-features/profiles.yaml)。地址必须替换；API Key 用 Secret 文件引用，不放在 ConfigMap 或镜像中。设置：

```sh
CUBE_ADAPTER_BACKENDS_FILE=/etc/cube-adapter/backends/backends.yaml
CUBE_ADAPTER_PROFILES_FILE=/etc/cube-adapter/profiles/profiles.yaml
```

通过 `cube_profiles` 查有权限的 Profile；OpenClaw、DSH、Hermes 的租约类工具新增可选 `profile` 参数。Codex、OpenCode 等 MCP Host 用 `cube_acquire(..., profile=...)` 选择，再复用返回的租约。任务型身份仍只能使用 `task:*`，新增通用工具不会扩大服务端权限；审批工具不会注册给 Agent。

租约、Job、Checkpoint、Plan、Task 和回执都带集群绑定。租约保存端点指纹：运行中的资源不会在修改配置后被连到另一集群；换地址需要显式迁移。修改任务集群、模板或执行策略会使旧审批失效，需要重新建计划。升级前建立、未绑定策略摘要的旧计划也需要重建。

一个后端故障时，readiness 会报告 `degraded` 和分集群状态，健康后端仍可服务。审计或共享状态故障则影响整个入口。不要在同一 `backend_id` 下更换集群；旧 v0.5.0 租约没有端点指纹，保留默认端点或先完成租约清理。

办公网 Adapter 仍需同时访问每个 CubeAPI 和 CubeProxy。SDK 0.7.3 的数据面使用 HTTP：跨网应走受控内网通道或隧道，不能把仅开放 API 的 HTTPS Ingress 当作完整连通。新增注册表不会修改节点路由、NAT 或 NetworkPolicy。

## 内部 OpenClaw 模板

[templates.yaml](../examples/production-features/templates.yaml) 定义版本、不可变模板 ID、固定初始化/预检命令和模型配置。用 `CUBE_ADAPTER_TEMPLATE_PRESETS_FILE` 加载。模型支持默认模型、fallback 和别名，创建时校验 provider 与 model ID 的对应关系，包括 provider 名称尾部空白这类错误。

`${FILE:/absolute/path}`、`${ENV:NAME}` 只解析完整配置值，不执行字符串插值或代码。密钥在创建沙箱时注入，配置文件权限设置为 0600，不进入租约记录、审计事件或模型返回体。模型 API 地址与模型 ID 需要按内部网关实际能力填写；结构预检不是模型 API 连通验证。

内部镜像应移除官方入口中覆盖配置的初始化逻辑，或用固定 bootstrap 命令停止初始化、注入文件、重启应用并预检。网关已经运行时，仅写入文件不保证它重新加载。模板配置失败会删除本次沙箱并返回错误。例子中的预检只检查 JSON；实际部署可使用内部镜像内固定脚本进行加载、网络和应答验证。

授予原始 shell/root 能力的 Agent 能读沙箱内配置。Secret 注入不等于向该身份隐藏密钥；敏感生产凭据应配合仅允许命名任务或 MCP 工具的 Action Scope。

## 受控沙箱 MCP

这与客户端连接 Adapter 的 MCP stdio 是两个层次：`cube_mcp_call` 经 Adapter 调用沙箱里 v0.7.3 的 MCP gateway。server 启动命令、环境变量、工具名和参数 Schema 全部由服务端 [mcp-policies.yaml](../examples/production-features/mcp-policies.yaml) 注册。

用 `CUBE_ADAPTER_MCP_POLICIES_FILE` 加载，在 Profile 中绑定 `mcp_policy`。镜像必须预装 gateway 与 server，模板暴露 50005。Profile 必须关闭公共流量；MCP Profile 暂不允许 Checkpoint，避免快照复用 gateway token。已有普通代码模板不会因此自动变成 MCP 模板。

`cube_mcp_list` 返回平台批准的 Schema，不把上游描述自动变成权限。`cube_mcp_call` 校验白名单、封闭参数 Schema、输入大小、整次请求时间和输出大小；返回前过滤协议附加字段并脱敏已注入的凭据。审计只记录工具名、参数摘要与执行链，不记录原始参数或结果。

服务端工具实现仍须限制可访问的数据与网络，不能仅靠 Schema 防止任意工具返回敏感内容。调用超时或网关结果无法确认，会保留未知意图并阻断执行，不能自动重试有副作用的工具。

## 长任务与恢复

Profile 用 `keepalive_enabled` 开启续租、`max_keepalive_seconds` 限制总寿命。后台只为有活动 Job/PTY 的运行中租约续租，周期必须小于初始超时的一半；`cube_renew` 也受同一上限约束。续租不会延长命令的 `max_command_seconds`。

恢复必须配置加密、可靠持久化的 Redis 状态后端。`cube_recover` 查看原租约和任务映射，确认已经过期的沙箱不会自动重建，未知任务不会自动重跑。

`cube_job_resume` 用独立 `stdout_offset`、`stderr_offset` 按字节读取日志，返回 Base64 与两个 `next_offset`。客户端持久化游标后可断线续读，UTF-8 跨块时先拼接字节再解码。旧 `cube_job_output` 保留兼容接口，但其 JSON-fragment 游标不适合增长中的日志续读。沙箱及其日志已被删除后，不能靠状态记录找回输出；重要产物应写到受控持久存储。

## 控制台与审计存储

开启 `CUBE_ADAPTER_AUDIT_UI=1` 后访问 `/console`。页面为 Light 模式，令牌只保存在本页内存；远程访问要求 HTTPS，本地端口转发可用 HTTP。页面可查看策略、逐个查看任务合同与参数、独立审批，以及审计查询、翻页、执行链详情和当前页导出。`/audit` 保留旧管理员视图。

查询走 `POST /v1/audit/query`，支持集群、操作、执行链、租约、任务过滤，单页 1–500 条。`auditor`、`approver` 仅可查本租户；管理员可跨租户。未带租户摘要的旧事件仅对管理员可见。查询本身也被审计，数据库阻断时在线页面不会绕过强审计；使用离线管理入口核对。控制台计划列表目前最多返回最新 500 条。

SQLite 仍是默认路径，只允许一个写入进程，使用可靠本地盘或块存储。PostgreSQL 用 `CUBE_ADAPTER_AUDIT_DATABASE_URL` 接入；DSN 放在 Secret，远程库使用 `sslmode=verify-full`。事件、意图/完成状态、外送队列在一个同步事务中提交，检查 `fsync`、`full_page_writes` 和 `synchronous_commit`。多副本还需要同一套加密 Redis 状态、HMAC Key、策略配置和命名空间。

审计写入实例有续期所有权。其他实例仍在处理中的意图不会被误当成崩溃；实例退出或所有权超时后遗留的意图，会阻断所有实例。失联实例不能重新续期参与写入；数据库恢复后需要核对并重启。失联检测最长为所有权 TTL（默认 60 秒），不是分布式外部执行的 exactly-once 保证。

PostgreSQL 接入不等于已具备数据库 HA。主机故障不丢已确认提交，需要部署方配置同步备库与可靠存储；`CUBE_ADAPTER_AUDIT_REQUIRE_SYNC_REPLICA=1` / Helm `audit.postgresql.requireSyncReplica` 校验同步备库配置不为空，备库选举、复制健康和灾备仍由数据库平台负责。[PostgreSQL WAL 说明](https://www.postgresql.org/docs/current/runtime-config-wal.html) 列出了同步提交的保证边界。

外送采用带领取期限的队列，多副本共同发送；仍可能重复，收集器按 `event_id` 去重。JSONL/stdout 是副本，PostgreSQL 才是权威记录。数据库不可写时不会降级成无审计执行。多副本不能共用原来的 SQLite PVC。

## 部署与离线迁移

[values-ha.yaml](../examples/production-features/values-ha.yaml) 是 Helm 示例，先准备 PostgreSQL、Redis、Secret 与三个目录 ConfigMap。`configurationSecrets` 挂载只读 Secret，`networkPolicy.extraEgress` 为每个后端配置最小必要路由。先在测试环境构建、固定候选镜像 digest，再安装；示例不会创建数据库或同步备库。

迁移生产审计前停掉全部写入实例，导出并核对源数据库，处理每条待确认操作。不要删除旧库。

```sh
python -m adapter.audit_admin --database /durable/audit.jsonl.sqlite3 pending
python -m adapter.audit_admin --database /durable/audit.jsonl.sqlite3 verify
python -m adapter.audit_admin --database /durable/audit.jsonl.sqlite3 export --output /secure/archive.jsonl
# AUDIT_DSN 由 Secret / 密钥管理器注入，不把密码放进命令行。
python -m adapter.audit_admin --postgres-url-env AUDIT_DSN --schema cube_adapter_audit import-sqlite --source /durable/audit.jsonl.sqlite3
python -m adapter.audit_admin --postgres-url-env AUDIT_DSN --schema cube_adapter_audit verify
```

导出为独占创建的 0600 JSONL 和 SHA256 manifest，不覆盖已有文件。事件新增哈希链，旧未带哈希的记录会明确计数；哈希只能与独立保存的可信锚点一起使用，不能阻止数据库管理员重写历史。

导入只允许空目标库、已停写且没有待核对操作的 SQLite 源库，保留事件 ID、原链摘要和未投递队列；不重放已确认投递、不改动源记录。导入原子完成后，先比较总数、摘要、待投递数，再切换。审计与 Redis 状态是两个独立数据源，两者都需备份。

另行配置 PostgreSQL 备份/WAL 归档与保留策略，将备份恢复到独立库验证后再考虑生产切换。没有自动删除或保留一天后裁剪审计的动作；此前“一天”要求针对 CubeSandbox MySQL binlog，并不代表授权删除 Adapter 审计历史。

## 实测与界面

控制台截图来自本地运行的候选 Adapter，使用测试租户和合成任务，均为 Light 模式：

![已授权 Profile 与后端绑定](assets/production-features/01-console-profiles-light.png)

![独立审批人查看并批准任务计划](assets/production-features/02-console-approval-light.png)

![持久审计查询与执行链](assets/production-features/03-console-audit-light.png)

- [真实界面验收](assets/production-features/console-results-2026-10-14.json)：审批、查询与导出通过，令牌未写入浏览器存储。
- [sr1 实测](assets/production-features/live-results-2026-10-14.json)：两个独立 Adapter 进程共用 PostgreSQL/Redis，完成后端绑定、配置注入、沙箱 MCP、任务重启接续、独立审批和签名回执；本次沙箱及模板已清理。
- [两个集群的只读核对](assets/production-features/multi-cluster-readonly-2026-10-14.json)：两套真实控制面及代理可达。sr1 的两个逻辑绑定不冒充两个独立集群的写入测试。
- [数据库断连与恢复演练](assets/production-features/postgres-fault-restore-2026-10-15.json)：停止真实 PostgreSQL 后，两个 Adapter 实例均返回 503，测试后端调用数为 0；旧实例保持阻断，重启后恢复。`pg_dump` 还原到独立数据库，15 条事件与链摘要一致，源库未覆盖。该项使用受控测试替身隔离审计边界，真实沙箱执行由 sr1 验收覆盖。
- [加强的续租实测](assets/production-features/live-results-2026-10-15.json)：初始超时 15 秒、超时即删除、关闭自动恢复；35 秒任务跨 Adapter 重启完成，记录 6 次续租，原命令没有重跑。沙箱与临时模板已清理。
- [最终回归](assets/production-features/regression-2026-10-15.json)：75 项 Adapter 测试全部通过，包括 5 项 PostgreSQL 和 1 项 Redis 集成测试，无跳过；原生插件、安装器、Helm、Ruff 与 Mypy 通过。

内部模型配置的测试范围是注入和结构预检，尚未验证 OpenClaw 应用启动及模型实际应答。上述截图是 Adapter 自身界面，不是新增功能在各 Agent 客户端内的截图。

## 生产候选镜像升级

2026-10-15 经单独授权，在生产环境完成候选镜像升级。镜像在中转机构建、推送暂存仓库，经镜像同步服务进入生产，先由不挂载业务 Secret/审计盘的一次性 Pod 验证拉取和非 root 启动，再以固定 digest 更新现有 Adapter。未发布新的 GitHub Release。

[生产验收记录](assets/production-features/production-upgrade-2026-10-15.json)中，创建、命令执行、状态、持久审计查询和删除均通过。升级前一致性快照中的 145,736 条历史事件逐行摘要一致，未完成意图为 0。保留现有单副本、SQLite 审计 PVC、Profile、Secret 和入口；部署 spec 对比仅镜像改变。审计哈希链从新事件开始，旧记录不回写补签。

生产尚未启用新增控制台、MCP 策略、自动续租或 PostgreSQL 多副本，也未改动 CubeSandbox 后端、节点标签或网络配置。这些能力需要另行配置和授权，不会因换镜像自动开放。回滚点为更新前 Deployment 修订版 6；一致性快照在原审计 PVC 中，未删除历史记录。
