import { copyFile, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { createHash } from 'node:crypto';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = path.join(root, 'dist/site');
const version = (await readFile(path.join(root, 'VERSION'), 'utf8')).trim();
if (!/^\d+\.\d+\.\d+(?:-[\w.-]+)?$/.test(version)) throw new Error('Invalid VERSION');
await rm(output, { recursive: true, force: true });
await mkdir(path.join(output, 'assets'), { recursive: true });
const cacheVersions = new Map();
for (const file of ['styles.css', 'trust.css', 'app.js']) {
  cacheVersions.set(file, createHash('sha256').update(await readFile(path.join(root, 'site', file))).digest('hex').slice(0, 12));
}
for (const file of ['index.html', 'styles.css', 'trust.css', 'app.js', 'favicon.svg']) {
  let source = await readFile(path.join(root, 'site', file), 'utf8');
  if (file === 'index.html') {
    for (const [asset, digest] of cacheVersions) source = source.replaceAll(`"${asset}"`, `"${asset}?v=${digest}"`);
  }
  await writeFile(path.join(output, file), source.replaceAll('{{VERSION}}', version));
}
// Explicit public asset allowlist: never publish the repository or runtime state.
for (const file of ['01-openclaw-trusted-task.jpg', '02-dsh-trusted-task.jpg', '03-codex-trusted-task.png', '04-hermes-trusted-task.jpg', '05-claude-code-trusted-task.png']) {
  await copyFile(path.join(root, 'docs/assets/trusted-execution-apps', file), path.join(output, 'assets', file));
}
for (const file of ['02-plan-and-approval.jpg', '03-execution-output-cleanup.jpg', '04-signed-receipt.jpg', '05-failure-handling.jpg', '07-cubesandbox-v071.jpg']) {
  await copyFile(path.join(root, 'docs/assets/trusted-execution-acceptance', file), path.join(output, 'assets', file));
}
for (const file of ['02-persistence-restart.png', '03-fail-closed-recovery.png']) {
  await copyFile(path.join(root, 'docs/assets/audit-durability-acceptance', file), path.join(output, 'assets', file));
}
console.log(`Built public site for v${version} at ${output}`);
