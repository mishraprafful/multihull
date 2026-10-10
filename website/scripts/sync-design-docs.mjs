import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

const pages = [
  {
    source: '../../docs/design/architecture-plan.md',
    target: '../src/content/docs/docs/design/architecture.md',
    title: 'Architecture plan',
    description: 'The full Multihull architecture plan, mirrored from docs/design/architecture-plan.md.',
    note: 'Mirrored at build time from `docs/design/architecture-plan.md` in the repository. Edit the source, not this page.',
  },
  {
    source: '../../docs/releases/0.2.0.md',
    target: '../src/content/docs/docs/reference/release-notes.md',
    title: 'Release notes',
    description: 'What 0.2.0 ships, what is proven and what is not, mirrored from docs/releases/0.2.0.md.',
    note: 'Mirrored at build time from `docs/releases/0.2.0.md` in the repository. Edit the source, not this page.',
  },
  {
    source: '../../docs/releases/0.1.0.md',
    target: '../src/content/docs/docs/reference/release-notes/0.1.0.md',
    slug: 'docs/reference/release-notes/0.1.0',
    title: 'Release notes 0.1.0',
    description: 'What 0.1.0 shipped, what was proven and what was not, mirrored from docs/releases/0.1.0.md.',
    note: 'Mirrored at build time from `docs/releases/0.1.0.md` in the repository. Edit the source, not this page.',
  },
];

for (const page of pages) {
  const source = resolve(here, page.source);
  const target = resolve(here, page.target);
  const body = readFileSync(source, 'utf8').replace(/^# .*\n/, '');
  const slug = page.slug ? `slug: ${page.slug}\n` : '';
  const frontmatter = `---
title: ${page.title}
description: ${page.description}
${slug}tableOfContents: { maxHeadingLevel: 2 }
---

:::note
${page.note}
:::

`;
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, frontmatter + body);
  console.log(`synced ${source} -> ${target}`);
}
