# OpenCode integration

OpenCode connects to the Adapter through its local MCP stdio support. The
recommended installation keeps the bearer token in a mode-0600 file and writes
only the file path into OpenCode configuration:

```bash
./scripts/install.sh opencode \
  --adapter-url https://adapter.example.internal \
  --token-file /absolute/path/to/secrets/opencode.token \
  --profile trusted-training
```

The default `trusted` mode denies the complete `cubesandbox_*` tool namespace,
then allows only plan, submit, status, result, cancel, and receipt. Approval is
not exposed to the Agent. Use `--mode full` only when interactive approval for
raw lease, command, file, job, PTY, artifact, and checkpoint tools is intended.

The installer detects OpenCode V1 and V2 configuration formats. It safely
merges strict JSON; if an existing file contains JSONC comments, point
`--config-file` at a dedicated JSON file and start OpenCode with the printed
`OPENCODE_CONFIG=...` command.

[`opencode.example.json`](opencode.example.json) is the OpenCode V1 equivalent
of the generated trusted-task configuration. Model-provider credentials remain
an OpenCode concern and are not needed by the Adapter.
