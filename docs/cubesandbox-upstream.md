# CubeSandbox upstream status / 上游状态

Checked on 2026-09-02 / 核对日期：2026-09-02.

## Latest release / 最新版本

[CubeSandbox v0.7.0](https://github.com/TencentCloud/CubeSandbox/releases/tag/v0.7.0)
was released on 2026-08-28. GitHub showed 24 additional commits on `master` at
the time of this review, so production consumers should follow a release tag
rather than an unpinned branch.

v0.7.0 的主要变化包括：

- 基于 S3 后端的跨节点 Pause/Resume 与从 Snapshot 创建 Sandbox（Preview）；
- 模板/快照记录组件版本，降低节点组件升级对存量对象的影响；
- 网络子系统重构，NetworkAgent 合并进 Cubelet，并优化 eBPF 策略下发；
- 运维面从 CubeMaster 拆到可多副本部署的 CubeOps；
- 动态更新运行中 Sandbox 网络策略；
- Go/Node Volume 能力补齐，Python 新增 `distribution_scope` 调度范围；
- 扩充 Python SDK 的 Filesystem、Rollback/Clone、Timeout 和异常生命周期 E2E。

For this Adapter, v0.7.0 is the minimum pinned SDK/API baseline. The Adapter
uses volumes, distribution scope, network updates, PTY, snapshots, rollback and
reconnect semantics from that line.

## Issues to follow / 建议持续跟进的 Issue

State was re-checked through the GitHub API on 2026-09-02.

| Issue | State / 状态 | Why it matters here / 对本项目的影响 | Current handling / 当前处理 |
| --- | --- | --- | --- |
| [#1395 Declarative runtime profiles](https://github.com/TencentCloud/CubeSandbox/issues/1395) | Open; the author said the feature will be migrated to the SDK. | Upstream-native policy profiles could reduce duplicated policy mapping. | Adapter profiles remain operator-owned YAML and translate into SDK arguments. |
| [#1599 Snapshots with volumes/host mounts](https://github.com/TencentCloud/CubeSandbox/issues/1599) | Open; last updated 2026-09-01. | Persistent workspaces cannot currently be snapshotted safely. | Checkpoint calls are rejected for mounted leases unless an operator explicitly opens the capability gate. |
| [#1598 Restored stdout/stderr logs](https://github.com/TencentCloud/CubeSandbox/issues/1598) | Open; maintainer confirmed a regression and plans a fix in the next release. | Restored sandbox process logs may not have expected output semantics. | Adapter durable jobs persist their own stdout/stderr/exit files and do not depend on restored command handles. |
| [#1414 CubeAPI default unauthenticated](https://github.com/TencentCloud/CubeSandbox/issues/1414) | Open. | A reachable unauthenticated CubeAPI is a control-plane exposure. | Helm/manifest support `CUBE_API_KEY`; NetworkPolicy restricts Adapter egress and CubeAPI must not be exposed publicly. |
| [#1521 Pause/resume stuck edge](https://github.com/TencentCloud/CubeSandbox/issues/1521) | Closed 2026-09-02; maintainer says to upgrade to v0.7.0. | Older deployments could leave leases stuck after failed lifecycle operations. | v0.7.0 is pinned; `/readyz`, status refresh, last-error state and GC visibility remain in place. |
| [#1565 Concurrent scheduling overcommit](https://github.com/TencentCloud/CubeSandbox/issues/1565) | Open; maintainer agrees the ratio should converge to one source of truth. | Adapter quotas do not replace compute-node admission correctness. | Tenant lease/job quotas limit this broker, but cluster capacity alerts and an upstream resolution are still required. |

## Integration guide follow-up / 集成指南跟进

[#244](https://github.com/TencentCloud/CubeSandbox/issues/244) remains open. The
Hermes Agent and DeepSeek Harness guide slots were claimed on 2026-08-29, and
bilingual guide drafts have been prepared against the upstream template. They
currently cite the v0.2.0 evidence. The v0.4.0 release adds refreshed real
CubeSandbox acceptance evidence for trusted tasks across OpenClaw, DSH, Codex,
and Hermes; update the upstream drafts to cite it before opening an external
pull request. No external pull request is created from this working tree
automatically.

## Feature decisions / 功能决策

- Persistent Volume and Checkpoint are separate profile capabilities; neither
  silently implies the other.
- Redis durability protects Adapter ownership data, not CubeSandbox's internal
  scheduler state.
- Kubernetes HA improves the Adapter and control-plane operations, but cannot
  hide compute-node pause/resume, snapshot or overcommit defects.
- `CUBE_API_KEY`, TLS/mTLS or OIDC, strict egress, and centralized audit should
  be treated as a deployment set rather than independent checkboxes.

Re-check these issues before enabling `allow_checkpoint_with_mounts`, before
raising tenant quotas substantially, and before upgrading CubeSandbox across a
production compute fleet.
