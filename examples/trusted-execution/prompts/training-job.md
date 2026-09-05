# Local code-agent training job prompt

Use only the trusted-task tools exposed by the `cube-trusted-training` MCP
server. Do not use local shell, SSH, kubectl, local files, `cube_exec`, raw job,
file, or artifact tools to access production resources.

Goal: run the server-owned `train-logistic` task against the approved
`/workspace/input/training.csv` dataset and return only allowlisted metrics,
manifest data, artifact digests, and the signed execution receipt.

1. Call `cube_task_plan` with template `train-logistic` and these parameters:
   `input=/workspace/input/training.csv`, `label=label`,
   `features=feature_a,feature_b`, `epochs=300`, and `learning_rate=0.1`.
2. If the plan is `pending_approval`, report its `plan_ref`, template,
   parameter/command digests, and expiry. Do not attempt to approve it; wait for
   the production approver to use their separate identity.
3. Once approved, call `cube_task_submit(plan_ref)`. Poll only with
   `cube_task_status(task_ref)`.
4. When terminal, call `cube_task_result(task_ref)`. This validates allowlisted
   outputs and destroys the MicroVM. Report metrics, manifest, model digest,
   cleanup state, and receipt signature metadata; never request raw data.
5. Use `cube_task_cancel` if cancellation is requested. Use
   `cube_task_receipt` to retrieve an already-finalized receipt.

Never request broader actions or reveal production credentials, input rows,
command text, or non-allowlisted output.
