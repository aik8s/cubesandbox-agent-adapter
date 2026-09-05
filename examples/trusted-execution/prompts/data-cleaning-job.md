# Local code-agent data-cleaning job prompt

Use only the trusted-task tools exposed by `cube-trusted-data-cleaning`. Do not
use local shell, SSH, kubectl, local files, `cube_exec`, raw job, file, or
artifact tools. Never return raw production data or sensitive fields.

Goal: run the server-owned `clean-csv` task over
`/workspace/input/raw.csv`, require `id,event_time`, drop `email,phone`, hash no
additional columns, cap input at one million rows, and return only an aggregate
report plus the cleaned-data digest.

1. Call `cube_task_plan` with template `clean-csv` and parameters `input`,
   `required_columns=id,event_time`, `drop_columns=email,phone`,
   `hash_columns=""`, and `max_rows=1000000`.
2. If approval is pending, report the opaque plan reference, digests, and
   expiry. Never approve with the Agent identity.
3. After independent approval, submit the plan and poll with
   `cube_task_status` only.
4. Finalize with `cube_task_result`. Report the allowlisted aggregate report,
   cleaned-data SHA-256/size, cleanup state, and signed receipt. Never request
   the cleaned CSV content.
5. Use `cube_task_cancel` only when requested or necessary.

Do not alter the profile, enumerate datasets, read credentials, or request
broader actions.
