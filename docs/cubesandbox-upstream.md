# CubeSandbox upstream status / 上游状态

Checked on 2026-09-23 / 核对日期：2026-09-23.

## Release channels / 发布通道

[CubeSandbox v0.7.1](https://github.com/TencentCloud/CubeSandbox/releases/tag/v0.7.1)
remains the latest stable release. It was released on 2026-09-11 with 70
commits from 24 contributors and is primarily a control-plane HA, storage and
correctness release:

- 独立的 CubeTemplateCenter 支持多副本 CubeMaster，生命周期管理支持主备；
- 带 Host Mount 或 Volume Plugin 的 Sandbox 可以执行 Snapshot、Restore、
  Rollback 和 Clone；
- 模板构建产物可以存入 S3/MinIO，并修复并发构建竞争；
- 同 Bucket 跨节点恢复加速，Snapshot 删除和回收语义得到完善；
- 增加 Kubernetes CubeS3lvol、外部 PostgreSQL 和统一的 `redis.db` 配置；
- 修复恢复后日志、非法 CPU/内存、Resume 超时、S3 增量快照、LLM 长响应、
  Registry 凭据日志泄露等问题。

[CubeSandbox v0.7.2-rc1](https://github.com/TencentCloud/CubeSandbox/releases/tag/v0.7.2-rc1)
was published as a pre-release on 2026-09-21. It is 53 commits ahead of v0.7.1.
The release page currently contains only the version string, so the following
assessment is based on the signed tag and its commit range rather than on a
stable-release compatibility promise:

- Kubernetes `cube-node` now defaults to `hostNetwork: true`. Existing releases
  must either pin `cubeNode.hostNetwork: false`, or drain every compute node and
  set the one-time `cubeNode.hostNetworkChangeAck: true` migration acknowledgement.
- Host networking preserves the sandbox TAP devices and cubevs hooks when the
  Big Pod is recreated, but Kubernetes NetworkPolicy no longer governs sandbox
  traffic. Host ports, Service/sandbox CIDR overlap, Cilium/eBPF interaction and
  Pod Security admission must be validated before adopting the new default.
- Snapshot correctness work freezes the VM across memory and rootfs capture and
  preserves rootfs artifacts referenced by snapshots. S3lvol gains hot-upgrade
  I/O pausing, faster restore reads and namespace-ID reuse.
- The Python SDK source now always uses CubeSandbox's direct Connect API for
  commands and adds optional user selection to more filesystem calls, removing
  the optional E2B command implementation path.
- The upstream repository now documents an OpenCode `tool.execute.before` hook
  that redirects the built-in Bash tool. This Adapter's OpenCode integration is
  different: it exposes explicit MCP tools, server-owned policies, approval,
  durable audit and receipts instead of transparently rewriting Bash.

The sr1 acceptance cluster deliberately remains on stable v0.7.1. Its current
`cube-node` uses the Pod network and the cluster uses Cilium, so adopting the RC
default is a network-boundary migration, not a routine image bump. A v0.7.2 RC
canary must explicitly choose the network mode and must not be described as a
production upgrade.

### Backend release and SDK package are different versions

The v0.7.1 and v0.7.2-rc1 Git tags both declare Python package version `0.7.0`,
and PyPI's latest `cubesandbox` package was also `0.7.0` when rechecked on
2026-09-23. Therefore this
Adapter deliberately keeps `cubesandbox==0.7.0`; there is no published Python
SDK `0.7.1` or `0.7.2-rc1` to pin. The RC's SDK changes are source-only and its
public methods used by this Adapter remain source compatible. This was also
verified by installing the SDK directly from the signed v0.7.2-rc1 source tag
in an isolated environment: all 48 Adapter Python tests passed (the optional
Redis integration test was skipped because no test Redis was configured), and
the installed command implementation contained the direct Connect API path
without the removed E2B path. CubeMaster continues to proxy the existing
template APIs to CubeTemplateCenter, so the upstream HA split does not require
an Adapter API change.

On 2026-09-12, the Kubernetes acceptance environment was upgraded from v0.7.0
to v0.7.1 while the Adapter kept the published `cubesandbox==0.7.0` SDK. All
CubeSandbox workloads became Ready, all eight upstream Helm test groups passed,
and the Adapter/backend suite passed 30/30 checks. The publication captures and
the mounted-volume result are recorded in the
[trusted-execution guide](trusted-execution.md#live-acceptance-evidence).

The same mounted-volume scenario was rerun on 2026-09-23 without upgrading the
backend. It again passed snapshot creation, rootfs rollback, external-volume
current-state semantics, clone/remount, referenced-snapshot deletion and all
six cleanup checks. Before and after the run the cluster had zero active
sandboxes, zero volumes and the same two pre-existing snapshots. The redacted
machine-readable result is
[`snapshot-results-2026-09-23.json`](assets/cubesandbox-upstream/snapshot-results-2026-09-23.json).

### Mounted-workspace checkpoint semantics

v0.7.1 closes [#1599](https://github.com/TencentCloud/CubeSandbox/issues/1599),
but a mounted-workspace checkpoint is an **external-reference checkpoint**:

- VM memory and rootfs are restored to the checkpoint state;
- files below a Host Mount or Volume Plugin mount are not copied into the
  checkpoint and remain at their latest external state;
- upstream preserves origin-node affinity for snapshots with Host Mounts and
  external volumes; clones can deliberately share writable mounted data;
- point-in-time data consistency still needs a storage-provider snapshot or an
  application freeze coordinated outside this Adapter.

The Adapter therefore keeps `allow_checkpoint_with_mounts: false` as the safe
default. On a v0.7.1-or-newer backend, an operator may enable it only after
validating node affinity, volume remounting, read-only behavior and the desired
data-consistency model:

```yaml
profiles:
  durable-checkpoint-code:
    checkpoints_enabled: true
    allow_checkpoint_with_mounts: true
    workspace:
      mode: per-session-volume
      retain_on_kill: true
```

The Adapter does not currently perform a backend version handshake. Enabling
the gate against an older backend will pass the request upstream and fail there.

### Upgrade notes that matter to operators

- CubeS3lvol is now opt-in. Existing nodes that used S3 implicitly must set
  `[cow.s3] enable = true`, or `ONE_CLICK_ENABLE_S3LVOL=1` for one-click
  deployments, before relying on cross-node S3 restore.
- CubeTemplateCenter becomes the template build path and needs durable
  S3/MinIO artifact storage. Existing CubeMaster template endpoints are proxied
  for client compatibility.
- A Kubernetes compute-plane image or Pod-template upgrade can recreate the
  `cube-node` DaemonSet Pod and interrupt existing sandboxes. Isolate and drain
  nodes according to the upstream upgrade guide; do not treat v0.7.1 control-
  plane HA as compute-plane live migration.
- The new top-level Helm `redis.db` should be set explicitly when several
  CubeSandbox clusters share one Redis instance.

### Findings from the live v0.7.0 to v0.7.1 upgrade

- The first Helm upgrade hit an immutable Redis StatefulSet
  `volumeClaimTemplates` change when v0.7.1 introduced stable labels. The test
  environment recorded the Redis Pod/PVC identities, orphan-deleted only the
  StatefulSet, reran the forward upgrade, and verified that the same Pod and PVC
  were adopted. Back up Redis first and repeat those identity checks before and
  after the change; do not delete the PVC.
- Database migrations had already advanced when the first upgrade failed. The
  v0.7.0 CubeMaster could not start against that newer schema, so automatic
  binary rollback was not a usable recovery path. Take a database backup and
  prepare a forward-fix procedure before upgrading.
- Templates retain their agent and shim component versions. A template built on
  v0.7.0 could still start a sandbox, but v0.7.1 rejected a snapshot clone whose
  metadata named the older agent. Rebuild the template after the control and
  compute planes are upgraded, verify its component annotations, then move the
  stable alias. Keep the old template unaliased until rollback decisions are
  complete.
- Private-image template builds require registry credentials. Supply them at
  execution time and keep them out of values files, command transcripts and
  screenshots.
- Treat CubeMaster and CubeTemplateCenter startup output as sensitive. This run
  observed effective configuration values in startup logs, so raw logs were not
  retained or published. Restrict log access and retention, inspect masking in
  the exact release, and rotate any credential whose value has entered an
  exported log.

## Issues to follow / 建议持续跟进的 Issue

State was re-checked through the GitHub API on 2026-09-23.

| Issue | State / 状态 | Why it matters here / 对本项目的影响 | Current handling / 当前处理 |
| --- | --- | --- | --- |
| [#1395 Declarative runtime profiles](https://github.com/TencentCloud/CubeSandbox/issues/1395) | Open; last updated 2026-08-27. | Upstream-native policy profiles could reduce duplicated policy mapping. | Adapter profiles remain operator-owned YAML and translate into SDK arguments. |
| [#1599 Snapshots with volumes/host mounts](https://github.com/TencentCloud/CubeSandbox/issues/1599) | Closed 2026-09-08; delivered by [#1654](https://github.com/TencentCloud/CubeSandbox/pull/1654) and [#1656](https://github.com/TencentCloud/CubeSandbox/pull/1656). | Mounted checkpoint operations now work, but external data stays current rather than rolling back. | Keep the explicit opt-in gate and validate the external-reference semantics on v0.7.1 before enabling it. |
| [#1598 Restored stdout/stderr logs](https://github.com/TencentCloud/CubeSandbox/issues/1598) | Closed 2026-09-02; the fix is listed in v0.7.1. | Restored sandbox log output is repaired upstream. | Adapter durable jobs still persist their own stdout/stderr/exit files for independent recovery. |
| [#1414 CubeAPI default unauthenticated](https://github.com/TencentCloud/CubeSandbox/issues/1414) | Closed 2026-09-03. | Control-plane authentication remains a deployment boundary even after the upstream issue closes. | Keep `CUBE_API_KEY`, private reachability, TLS/mTLS or OIDC and restrictive NetworkPolicy. |
| [#1484 Non-positive SDK command timeout](https://github.com/TencentCloud/CubeSandbox/issues/1484) | Closed 2026-08-28; v0.7.1 contains Node/Python source fixes. | The fix has not been published as a Python 0.7.1 package. | Adapter request and profile validation already require positive command timeouts, so this path is not used. |
| [#1521 Pause/resume stuck edge](https://github.com/TencentCloud/CubeSandbox/issues/1521) | Closed 2026-09-02. | Older deployments could leave leases stuck after failed lifecycle operations. | `/readyz`, status refresh, last-error state and GC visibility remain in place. |
| [#1565 Concurrent scheduling overcommit](https://github.com/TencentCloud/CubeSandbox/issues/1565) | Closed 2026-09-17 after design discussion; the linked consolidation PR [#1595](https://github.com/TencentCloud/CubeSandbox/pull/1595) was closed without merge. | Closing the report is not evidence that concurrent admission or compounded overcommit was changed in a release. | Keep tenant lease/job quotas and node-capacity alerts; load-test the exact scheduler configuration before raising concurrency. |

## Integration guide follow-up / 集成指南跟进

[#244](https://github.com/TencentCloud/CubeSandbox/issues/244) remains open. The
Hermes Agent and DeepSeek Harness guide slots were claimed on 2026-08-29, and
bilingual guide drafts have been prepared against the upstream template. They
currently cite the v0.2.0 evidence. The v0.4.0 release adds refreshed real
CubeSandbox acceptance evidence for trusted tasks across OpenClaw, DSH, Codex,
and Hermes; update the upstream drafts to cite it before opening an external
pull request. No external pull request is created from this working tree
automatically.

The v0.5.0 follow-up additionally validates Claude Code through both the
trusted-task MCP flow and the Codex-equivalent acquire/exec/status/release
flow. Its light native-client captures can be cited from the main README.

## Feature decisions / 功能决策

- Persistent Volume and Checkpoint are separate profile capabilities; neither
  silently implies the other.
- Redis durability protects Adapter ownership data, not CubeSandbox's internal
  scheduler state.
- Kubernetes HA improves the Adapter and control-plane operations, but cannot
  hide compute-node pause/resume, snapshot or overcommit defects.
- `CUBE_API_KEY`, TLS/mTLS or OIDC, strict egress, and centralized audit should
  be treated as a deployment set rather than independent checkboxes.

Re-check the open issues before raising tenant quotas substantially. Before
enabling `allow_checkpoint_with_mounts`, run restore, rollback, clone, read-only
mount and cleanup tests on the exact storage driver and node topology. Before a
production upgrade, also follow the upstream
[Kubernetes upgrade guide](https://github.com/TencentCloud/CubeSandbox/blob/v0.7.1/docs/zh/guide/kubernetes/upgrade.md)
and the [v0.7.1 release notes](https://github.com/TencentCloud/CubeSandbox/releases/tag/v0.7.1).
