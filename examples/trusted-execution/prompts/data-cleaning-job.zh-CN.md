# 本地 Code Agent 数据清洗任务 Prompt

请只使用 `cube-trusted-data-cleaning` MCP Server 暴露的可信任务工具。不要用本地
Shell、SSH、kubectl、本地文件、`cube_exec`、原始 Job、文件或 Artifact 工具，也不要
向模型返回生产数据原文或敏感字段。

任务目标：通过服务端 `clean-csv` 任务清洗 `/workspace/input/raw.csv`，要求
`id,event_time` 非空，删除 `email,phone`，不额外哈希字段，最多处理 100 万行，只返回
聚合报告和清洗结果摘要。

1. 调用 `cube_task_plan`，模板为 `clean-csv`，参数包括 `input`、
   `required_columns=id,event_time`、`drop_columns=email,phone`、
   `hash_columns=""`、`max_rows=1000000`。
2. 若等待审批，只报告 Plan 引用、摘要和过期时间；Agent 身份不得自行审批。
3. 独立审批通过后提交 Plan，只用 `cube_task_status` 查询。
4. 使用 `cube_task_result` 完成收口；只报告白名单聚合结果、清洗文件 SHA-256/大小、
   清理状态和签名 Receipt，绝不读取清洗 CSV 正文。
5. 用户要求或任务异常时才调用 `cube_task_cancel`。

不要修改 Profile、枚举数据集、读取凭据或申请更大的 Action 权限。
