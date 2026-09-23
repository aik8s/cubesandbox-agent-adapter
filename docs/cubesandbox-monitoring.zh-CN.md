# CubeSandbox Prometheus 与 Grafana 监控

CubeSandbox v0.7.1 原生暴露 Prometheus 文本格式指标。2026-09-23 已在 sr1
完成 kube-prometheus-stack 接入和 Grafana 面板验证。

## 已验证端点

| 组件 | 端点 | 主要内容 |
| --- | --- | --- |
| CubeMaster | `:8089/metrics` | Snapshot commit/rollback/delete、孤儿副本、存储模式、Go/process |
| CubeTemplateCenter | `:8090/metrics` | 模板中心 Snapshot 状态、Go/process |
| Cubelet | `:9998/v1/metrics` | MicroVM 数量、调度配额、镜像、containerd、进程指标 |
| Cubelet | `:9998/v1/metrics/resource` | 按 `sandbox_id` 的 host/guest CPU 与内存 |

资源指标端点返回 HTTP 200 但内容为空不一定是故障：没有运行中 Sandbox、采集插件关闭，
或尚未完成首轮采样时都可能为空。sr1 的插件已启用，当前配置为每 5 秒采集一次
`host_sandbox`；该口径包含 VMM、CubeShim 等 host cgroup 开销，不等于 guest working set。

这些端点没有内置认证或 TLS，不要用 NodePort、Ingress 或公网 LoadBalancer 暴露。
本方案由 Prometheus 在集群内部直接访问 Service/Pod IP。

## sr1 接入

应用监控资源：

```bash
kubectl --context sr1 apply \
  -f deploy/monitoring/cubesandbox-sr1-monitors.yaml
```

清单包含两个 `ServiceMonitor` 和一个有两条抓取路径的 `PodMonitor`。它使用 sr1
Prometheus 实例要求的标签 `monitoring.aik8s.run/instance: aibrix`，并只保留面板需要的
指标族，避免采集 Cubelet 全量 gRPC/containerd 指标带来的不必要基数。

验证四条目标：

```promql
up{
  cluster="sr1",
  cubesandbox_component=~"master|templatecenter|cubelet|cubelet-resource"
}
```

2026-09-23 实测结果为 4/4 `up=1`。同时确认：

- `cube_cubebox_scheduler_mvm_running_num{cluster="sr1"}` 可查询；
- Master 和 TemplateCenter 都能查询 `cube_snapshot_orphan_count`，当前值为 0；
- `/v1/metrics/resource` 抓取目标在线；无运行中 Sandbox 时没有 `cubesandbox_*` 序列。

## Grafana 面板

面板文件为
[`cubesandbox-sr1.json`](assets/grafana/cubesandbox-sr1.json)，数据源 UID 是
`sr1-prometheus`。可以从 Grafana 的 **Dashboards → New → Import** 导入，也可以在
Grafana Pod 内使用已有管理员环境变量导入，避免把密码写入命令或仓库：

```bash
jq -n --slurpfile dashboard docs/assets/grafana/cubesandbox-sr1.json \
  '{dashboard:$dashboard[0],folderId:0,overwrite:true}' | \
kubectl --context sr1 -n monitoring exec -i \
  deploy/aibrix-monitoring-grafana -- sh -c \
  'curl -fsS -u "$GF_SECURITY_ADMIN_USER:$GF_SECURITY_ADMIN_PASSWORD" \
  -H "Content-Type: application/json" --data-binary @- \
  http://127.0.0.1:3000/api/dashboards/db'
```

sr1 已导入 UID `cubesandbox-sr1`，路径为
`/d/cubesandbox-sr1/cubesandbox-sr1`。面板展示采集状态、MicroVM、Snapshot、Cubelet
配额、组件 RSS、抓取耗时，以及有运行中 Sandbox 时的逐沙箱 CPU/内存。

下面是临时无外网 Sandbox 运行期间的 Light 模式实测。图中 4 个目标全部在线，CPU
rate 和内存 gauge 均已产生序列；公开素材只显示 Sandbox ID 的前 8 位。截图完成后
临时 Sandbox 已释放，CubeMaster 再次报告 `SANDBOX_COUNT 0`。脱敏结果见
[`cubesandbox-sr1-result.json`](assets/grafana/cubesandbox-sr1-result.json)。

![CubeSandbox sr1 Prometheus 与 Grafana Light 模式实测](assets/grafana/cubesandbox-sr1-light.png)

## 生产化建议

- 每个 CubeSandbox 集群设置独立 `cluster` 标签，Dashboard 用变量选择集群；
- 生产告警至少覆盖目标下线、Snapshot 孤儿数、Snapshot 存储模式和资源采集持续为空；
- 高并发环境先核对序列基数，再决定是否保留 `sandbox_id` 以及 guest/host 两套指标；
- `guest_workload` 依赖模板内的 cube-agent 与 cgroup v2 能力，旧模板可能只有
  `host_sandbox`；
- Grafana Dashboard 的长期交付应纳入监控 Helm values 或 GitOps，不只依赖一次 API
  导入。
