import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const source = resolve(here, '../../docs/design/architecture-plan.md');
const target = resolve(here, '../src/content/docs/docs/design/architecture.md');

const body = readFileSync(source, 'utf8').replace(/^# .*\n/, '');

const frontmatter = `---
title: Architecture plan
description: The full Multihull architecture plan, mirrored from docs/design/architecture-plan.md.
tableOfContents: { maxHeadingLevel: 2 }
---

:::note
Mirrored at build time from \`docs/design/architecture-plan.md\` in the repository. Edit the source, not this page.
:::

`;

mkdirSync(dirname(target), { recursive: true });
writeFileSync(target, frontmatter + body);
console.log(`synced ${source} -> ${target}`);
