import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { readdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
const output = new URL('../dist/node/', import.meta.url);
rmSync(output, { recursive: true, force: true });
execFileSync(process.execPath, [fileURLToPath(new URL('../node_modules/typescript/bin/tsc', import.meta.url)), '-p', fileURLToPath(new URL('../sdk/typescript/tsconfig.json', import.meta.url))], { stdio: 'inherit' });
// TypeScript rewrites emitted JavaScript, but retains .ts specifiers in declarations.
for (const file of readdirSync(output).filter(name => name.endsWith('.d.ts'))) {
  const path = new URL(file, output);
  writeFileSync(path, readFileSync(path, 'utf8').replace(/(['"])(\.\/[^'"]+)\.ts\1/g, '$1$2.js$1'));
}
