# 本地 Code Agent 训练任务 Prompt

请只使用 `cube-trusted-training` MCP Server 暴露的可信任务工具。不要用本地 Shell、
SSH、kubectl、本地文件、`cube_exec`、原始 Job、文件或 Artifact 工具访问生产资源。

任务目标：通过服务端维护的 `train-logistic` 任务处理已批准的
`/workspace/input/training.csv`，只返回白名单指标、清单、产物摘要和签名 Receipt。

1. 调用 `cube_task_plan`，模板为 `train-logistic`，参数为
   `input=/workspace/input/training.csv`、`label=label`、
   `features=feature_a,feature_b`、`epochs=300`、`learning_rate=0.1`。
2. 如果状态为 `pending_approval`，只报告 `plan_ref`、模板、参数/命令摘要和过期时间；
   不要尝试审批，等待生产审批者使用独立身份处理。
3. 审批通过后调用 `cube_task_submit(plan_ref)`，只用
   `cube_task_status(task_ref)` 查询状态。
4. 任务终态后调用 `cube_task_result(task_ref)`。该操作会校验白名单输出并销毁
   MicroVM。只报告指标、清单、模型摘要、清理状态和 Receipt 签名信息。
5. 需要取消时调用 `cube_task_cancel`；已完成任务可用 `cube_task_receipt` 再取 Receipt。

不要申请更大的 Action 权限，也不要泄露生产凭据、输入行、命令正文或非白名单结果。
