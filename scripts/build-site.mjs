import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = path.join(root, 'dist/site');
const version = (await readFile(path.join(root, 'VERSION'), 'utf8')).trim();
if (!/^\d+\.\d+\.\d+(?:-[\w.-]+)?$/.test(version)) throw new Error('Invalid VERSION');
await mkdir(path.join(output, 'assets'), { recursive: true });
for (const file of ['index.html', 'styles.css', 'app.js', 'favicon.svg']) {
  const source = await readFile(path.join(root, 'site', file), 'utf8');
  await writeFile(path.join(output, file), source.replaceAll('{{VERSION}}', version));
}
// Explicit public asset allowlist: never publish the repository or runtime state.
for (const file of ['01-openclaw-trusted-task.jpg', '02-dsh-trusted-task.jpg', '03-codex-trusted-task.png', '04-hermes-trusted-task.jpg']) {
  await copyFile(path.join(root, 'docs/assets/trusted-execution-apps', file), path.join(output, 'assets', file));
}
console.log(`Built public site for v${version} at ${output}`);
