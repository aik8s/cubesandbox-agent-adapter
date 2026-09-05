# 本地 Code Agent 到生产资源的受控可信执行

很多工程师希望继续使用本地熟悉的 Codex、Claude Code 或其他 Code Agent，但训练
数据、内部服务、GPU 和生产数据库只存在于隔离的生产网。直接给笔记本开生产网权限，
或者把生产凭据交给模型和本地插件，都会扩大信任边界。

CubeSandbox Agent Adapter 可以放在生产侧，作为本地 Agent 唯一能够调用的受控执行
入口：

```text
办公网中的本地 Code Agent
          |
          | HTTPS + mTLS/OIDC + 租户身份
          v
生产网接入网关 -> CubeSandbox Agent Adapter -> 命名任务 + JSON Schema
                                      -> 独立审批 -> 运维方固定的 Profile
                                                    |
                                                    v
                                         CubeSandbox MicroVM
                                         | 只读数据 / 受限身份
                                         | 无公网或受控内网出口
                                         v
                                      训练或数据清洗任务
```

本地 Agent 只获得不透明的 Plan/Task 引用和批准后的任务结果。Cube 连接配置、完整
Sandbox ID、流量令牌、数据卷和生产工作负载身份都留在生产侧。服务端任务契约固定命令
参数向量、Profile、输出路径、返回方式和资源限制，模型不能注入 Shell 片段、切换 Cube
模板、开放公网或延长生命周期。

## 这个模式解决什么问题

- 保留本地 Code Agent 的交互体验，但生产数据和生产凭据不落到办公电脑；
- 在 MicroVM 中运行模型生成的代码，避免直接在生产节点或共享容器中执行；
- 通过租户、Profile、并发数、任务数、超时、工作区和网络策略限制爆炸半径；
- 使用异步 Job 承载分钟到小时级任务，支持查询、分页输出和取消；
- 审计记录主体、Profile、动作、摘要、耗时和结果，而不记录原始命令、输出或令牌；
- 任务结束后销毁 MicroVM 和临时工作区，或者按运维方策略暂停/保留。

这里的“可信执行”是**策略受控、隔离且可审计的执行面**，不是 SGX、TDX、SEV 一类
带远程证明的机密计算 TEE。若业务要求宿主机不可见、内存加密或硬件证明，需要叠加
相应的机密计算能力。

## 两种训练模式

### 单机训练直接在 MicroVM 中运行

适用于预处理、特征验证、小模型训练、LoRA 前置检查或能够在单个 CubeSandbox 模板
中完成的任务。模板预装固定版本依赖和经过审查的脚本，把批准的数据放入只读输入路径，
Adapter 为任务创建临时 `/workspace`。本地 Agent 只提交符合 JSON Schema 的参数，通过
`cube_task_plan`、独立审批和 `cube_task_submit` 启动任务，不能替换脚本或提交任意命令。

仓库示例：

- [`trusted-training` Profile](../examples/trusted-execution/profiles.yaml)；
- [训练任务 Prompt](../examples/trusted-execution/prompts/training-job.zh-CN.md)；
- [无第三方依赖的训练示例](../examples/trusted-execution/tasks/train_logistic.py)。

### 大规模或分布式训练只由 MicroVM 提交

GPU 集群、队列和分布式训练不应该把 `kubectl` 管理员权限交给本地 Agent。更安全的
做法是在受控模板里只放置一个窄权限的 `trainctl submit` 或内部训练 API 客户端：

1. Agent 生成并校验训练配置；
2. MicroVM 使用绑定到模板的短期工作负载身份提交任务；
3. 内部平台校验镜像、数据集、资源上限、队列和审批状态；
4. Agent 只读取任务状态、指标摘要和批准的产物引用。

这时 CubeSandbox 是提交与验证边界，真正的训练由现有 Kubernetes、Volcano、Slurm
或训练平台调度。不要在模板里放通用集群管理员 kubeconfig。

## 数据清洗模式

批准的数据由平台只读挂载或由模板内的窄权限数据客户端暂存到批准的输入路径。清洗脚本
在 MicroVM 中去重、校验、脱敏并生成统计报告，原始行不写入日志，也不返回给模型。
服务端只返回白名单报告和摘要；大型结果由模板内的窄权限工作负载身份写入批准的数据存储。

仓库示例：

- [`trusted-data-cleaning` Profile](../examples/trusted-execution/profiles.yaml)；
- [数据清洗 Prompt](../examples/trusted-execution/prompts/data-cleaning-job.zh-CN.md)；
- [CSV 清洗示例](../examples/trusted-execution/tasks/clean_csv.py)。

## 必须落实的安全边界

1. Adapter 放在生产网内，通过企业网关、VPN、零信任代理或端口转发暴露最小入口；
   远端连接必须使用 HTTPS，并优先使用 mTLS 或短期 OIDC 身份。
2. 每个团队使用独立主体，只授权 `mcp` Runtime 和所需 Profile；训练与数据清洗使用
   不同令牌或身份、模板和数据权限。
3. 数据集由平台预置或只读挂载，不使用 Artifact API 把生产数据上传到本地。当前单个
   Artifact 上限为 8 MiB，Job 聚合输出上限为 1 MiB，本来就不应承担大数据传输。
4. 禁止公网并不等于已经限制所有内网。还要在 CubeSandbox、Kubernetes 或网络网关
   中配置目标级白名单，只允许数据目录、训练 API、模型仓库或结果存储。
5. 不把长期云密钥、数据库密码、kubeconfig 或 Cube traffic token 写进 Prompt、脚本、
   镜像和工作区；由平台注入短期工作负载身份。
6. 限制返回给 Agent 的内容。MicroVM 隔离不能阻止恶意任务通过 stdout、文件读取或
   Artifact 下载泄露数据；生产环境仍需结果白名单、内容检查、审批和 DLP。
7. 任务失败、Agent 断线和超时都必须触发取消或回收；监控活动租约、Job、MicroVM、
   临时卷和审计 Sink 是否回到预期状态。

## 服务端强制任务契约

设置 `CUBE_ADAPTER_TASK_TEMPLATES_FILE`，加载
[`task-templates.yaml`](../examples/trusted-execution/task-templates.yaml) 一类由运维方维护
的 YAML。每个命名任务包含封闭 JSON Schema、固定 `argv`、工作目录、Cube Profile、
审批要求、Plan 有效期，以及输出白名单、大小限制、JSON Schema 和
`content`/`digest` 返回方式。

动态参数只能替换完整的一个参数，并由服务端逐项 Shell quote；
`prefix-${value}` 一类拼接占位符会在启动时直接拒绝。任务状态流为：

```text
plan -> pending_approval -> approved -> submitted/running
                                      -> succeeded -> 校验输出
                                                   -> 销毁 MicroVM
                                                   -> 签名 Receipt
```

按 [`token-principals.example.json`](../config/token-principals.example.json) 分离身份：本地
Agent 仅拥有 `task:plan/submit/status/result/cancel/receipt`，没有 `exec:run`、
`job:start`、文件、Artifact、PTY 和 Checkpoint 权限；生产审批者只拥有
`task:approve` 和 `approver` 角色。`allowed_task_templates` 与 `allowed_profiles` 再限制
双方可操作的任务。普通 Agent 既不能自批，也不能绕过任务模板调用任意命令。

任务成功后，Adapter 只读取白名单输出：`expose: content` 才返回内容，
`expose: digest` 仅返回大小和 SHA-256；输出校验通过并确认 MicroVM 清理后才签发 HS256
Execution Receipt。优先配置独立的 `CUBE_ADAPTER_RECEIPT_HMAC_KEY`，使用
`scripts/verify_receipt.py --key-file ... receipt.json` 离线验签。

在 Kubernetes 中，把任务 YAML 放入 ConfigMap，并在 Helm 中设置
`taskTemplates.enabled=true` 与 `taskTemplates.existingConfigMap=<名称>`；task-only 与
approver Principal 放入认证 Secret 并设置 `auth.tokenPrincipalsKey`，独立 Receipt Key
通过 `auth.receiptHmacKey` 引用。生产环境建议启用 Redis 加密状态，确保 Plan、审批、
Task 和 Receipt 在 Adapter 重启后仍可恢复。

当前是一名独立审批者的流程。多人会签、撤销回调、DLP、数据集目录、短期工作负载身份
和对外可验证的公钥证明，仍应与企业审批、训练和数据平台集成。HMAC Receipt 是共享
密钥域内的完整性证明，不等同于硬件远程证明。

## 真实环境验收截图

下面的截图来自隔离验收命名空间中的真实 CubeSandbox MicroVM 执行，不是界面模拟。
验收结果为 23/23 PASS；页面使用 light 模式，并排除了令牌、地址、完整内部标识、
原始命令和任务数据。

总体结果、服务健康、模板目录与可信边界：

![可信执行验收总览](assets/trusted-execution-acceptance/01-overall-runtime-trust.jpg)

Plan、独立审批与幂等提交：

![Plan 与独立审批验收](assets/trusted-execution-acceptance/02-plan-and-approval.jpg)

真实 MicroVM 执行、输出白名单与清理：

![执行、输出与清理验收](assets/trusted-execution-acceptance/03-execution-output-cleanup.jpg)

离线验签与一致的 Receipt 传输：

![签名执行凭证验收](assets/trusted-execution-acceptance/04-signed-receipt.jpg)

拒绝、过期、非法输出、取消和免审批策略：

![失败路径验收](assets/trusted-execution-acceptance/05-failure-handling.jpg)

OpenClaw、DSH、Codex/MCP 与 Hermes 均完成真实 plan、submit、status、result、receipt
链路，且 MicroVM 清理已验证：

![四类 Agent 客户端验收](assets/trusted-execution-acceptance/06-agent-clients.jpg)

### 客户端自身界面实测证据

上面的证据卡用于汇总和关联后端验收结果；下面四张图则直接来自各客户端自身的
Light 模式界面。每个客户端均实际调用了 `cube_task_plan`、`cube_task_submit`、
`cube_task_status`、`cube_task_result` 和 `cube_task_receipt`，最终状态为
`succeeded`，MicroVM 清理为 `verified`，Receipt 算法为 HS256。

OpenClaw Control UI，直接显示 5 次 Adapter 工具调用与最终结果：

![OpenClaw 在自身 Control UI 中完成可信任务](assets/trusted-execution-apps/01-openclaw-trusted-task.jpg)

DeepSeek Harness Web，直接显示同一条 5 工具调用链和输出校验结果：

![DSH 在自身 Web UI 中完成可信任务](assets/trusted-execution-apps/02-dsh-trusted-task.jpg)

Codex CLI 0.153.2，通过 Adapter 的 stdio MCP Server 且只启用 5 个可信任务工具：

![Codex 在自身 TUI 中通过 MCP 完成可信任务](assets/trusted-execution-apps/03-codex-trusted-task.png)

Hermes Agent 0.20.6 官方 Dashboard；会话中的 6 tools 包含 1 次
`tool_describe` 工具发现和同样的 5 次 `cube_task_*` 调用：

![Hermes 在自身 Dashboard 中完成可信任务](assets/trusted-execution-apps/04-hermes-trusted-task.jpg)

发布版截图不包含令牌、Adapter/模型网关地址、完整 Plan/Task/Sandbox 标识或内网信息。
Hermes 历史记录里的原始签名 Receipt 载荷已做可见遮挡，但最终状态、清理结果和签名
算法仍保留在画面中。
