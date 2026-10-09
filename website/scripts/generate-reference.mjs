import { spawnSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const pythonProject = resolve(here, '../../python');
const referenceDir = resolve(here, '../src/content/docs/docs/reference');

const helpEnv = {
  ...process.env,
  TERM: 'dumb',
  NO_COLOR: '1',
  COLUMNS: '88',
  _TYPER_FORCE_DISABLE_TERMINAL: '1',
};

function hull(args) {
  const result = spawnSync('uv', ['run', '--project', pythonProject, 'hull', ...args], {
    encoding: 'utf8',
    env: helpEnv,
  });
  if (result.error?.code === 'ENOENT') {
    throw new Error(
      'uv is not on PATH. Install it from https://docs.astral.sh/uv/ and run `uv sync` in python/ before building the website.',
    );
  }
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`hull ${args.join(' ')} exited with ${result.status}\n${result.stderr}`);
  }
  return result.stdout;
}

const ansi = /\u001b\[[0-9;]*[A-Za-z]/g;

function cleanHelp(text) {
  return text
    .replace(ansi, '')
    .split('\n')
    .map((line) => line.trimEnd())
    .join('\n')
    .replace(/^\n+/, '')
    .replace(/\n+$/, '');
}

function commandNames(rootHelp) {
  const block = rootHelp.split('Commands')[1] ?? '';
  return [...block.matchAll(/^│\s+([a-z][a-z0-9-]*)\b/gm)].map((m) => m[1]);
}

function code(text, lang = '') {
  return `\`\`\`${lang}\n${text}\n\`\`\``;
}

function inline(value) {
  const text = String(value);
  const longestRun = Math.max(0, ...[...text.matchAll(/`+/g)].map((run) => run[0].length));
  const fence = '`'.repeat(longestRun + 1);
  const pad = text.startsWith('`') || text.endsWith('`') ? ' ' : '';
  return `${fence}${pad}${text}${pad}${fence}`;
}

function cell(text) {
  return text.replace(/\|/g, '\\|');
}

function refName(ref) {
  return ref.split('/').pop();
}

function anchor(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '-');
}

function typeOf(prop, defs) {
  if (prop.$ref) {
    const name = refName(prop.$ref);
    return `[${name}](#${anchor(name)})`;
  }
  if (prop.anyOf) {
    const options = prop.anyOf.filter((option) => option.type !== 'null');
    const nullable = options.length !== prop.anyOf.length;
    const rendered = options.map((option) => typeOf(option, defs)).join(' or ');
    return nullable ? `${rendered}, nullable` : rendered;
  }
  if (prop.const !== undefined) return `const ${inline(JSON.stringify(prop.const))}`;
  if (prop.enum) return prop.enum.map((value) => inline(JSON.stringify(value))).join(' | ');
  if (prop.type === 'array') {
    return `array of ${prop.items ? typeOf(prop.items, defs) : 'any'}`;
  }
  if (prop.type === 'object' && prop.additionalProperties && prop.additionalProperties !== true) {
    return `map of string to ${typeOf(prop.additionalProperties, defs)}`;
  }
  return prop.type ?? 'any';
}

const constraintKeys = {
  minimum: 'min',
  maximum: 'max',
  exclusiveMinimum: 'greater than',
  exclusiveMaximum: 'less than',
  minLength: 'min length',
  maxLength: 'max length',
  minItems: 'min items',
  maxItems: 'max items',
  pattern: 'pattern',
};

function constraintsOf(prop) {
  const parts = [];
  for (const [key, label] of Object.entries(constraintKeys)) {
    if (prop[key] !== undefined) parts.push(`${label} ${inline(prop[key])}`);
  }
  return parts.join(', ');
}

function defaultOf(prop, required) {
  if (required) return 'required';
  if (prop.default === undefined) return '';
  return inline(JSON.stringify(prop.default));
}

function objectTable(schema, defs) {
  const required = new Set(schema.required ?? []);
  const rows = Object.entries(schema.properties ?? {});
  const hasDescription = rows.some(([, prop]) => prop.description);
  const header = ['Field', 'Type', 'Default', 'Constraints'];
  if (hasDescription) header.push('Description');
  const lines = [`| ${header.join(' | ')} |`, `|${header.map(() => '---').join('|')}|`];
  for (const [name, prop] of rows) {
    const cells = [
      inline(name),
      cell(typeOf(prop, defs)),
      cell(defaultOf(prop, required.has(name))),
      cell(constraintsOf(prop)),
    ];
    if (hasDescription) cells.push(cell(prop.description ?? ''));
    lines.push(`| ${cells.join(' | ')} |`);
  }
  if (rows.length === 0) lines.push('| _no fields_ | | | |');
  return lines.join('\n');
}

function enumSection(name, schema) {
  const values = schema.enum.map((value) => `- ${inline(JSON.stringify(value))}`).join('\n');
  return `### ${name}\n\n${schema.description ? `${schema.description}\n\n` : ''}${values}`;
}

function objectSection(name, schema, defs, intro = '') {
  const parts = [`### ${name}`];
  if (intro) parts.push(intro);
  if (schema.description) parts.push(schema.description);
  parts.push(objectTable(schema, defs));
  return parts.join('\n\n');
}

function renderSchemaPage(schema) {
  const defs = schema.$defs ?? {};
  const objects = Object.entries(defs).filter(([, def]) => def.type === 'object');
  const enums = Object.entries(defs).filter(([, def]) => def.enum);
  const sections = [
    objectSection(
      schema.title ?? 'Root',
      schema,
      defs,
      `Top-level document. Schema id: ${inline(schema.$id ?? '')}.`,
    ),
    ...objects.map(([name, def]) => objectSection(name, def, defs)),
  ];
  const enumSections = enums.map(([name, def]) => enumSection(name, def));
  return `---
title: Spec schema
description: JSON Schema for multihull.yaml, generated from the pydantic model by hull schema.
tableOfContents: { maxHeadingLevel: 3 }
---

:::note
Generated at build time from \`hull schema\`. Edit \`python/multihull/spec.py\`, not this page.
:::

Export the schema and point your editor at it for completion and validation:

\`\`\`sh
hull schema --out multihull.schema.json
\`\`\`

\`\`\`yaml
# yaml-language-server: $schema=./multihull.schema.json
apiVersion: multihull/v1
name: llama-8b
\`\`\`

See [The spec](/docs/concepts/spec/) for the section-by-section description.

## Objects

${sections.join('\n\n')}

## Enums

${enumSections.join('\n\n')}

## Raw schema

<details>
<summary>multihull.schema.json</summary>

${code(JSON.stringify(schema, null, 2), 'json')}

</details>
`;
}

function renderCliPage(rootHelp, commands) {
  const sections = commands.map(
    ([name, help]) => `### hull ${name}\n\n${code(help, 'text')}`,
  );
  return `---
title: CLI
description: The hull command, rendered from its own --help output.
tableOfContents: { maxHeadingLevel: 3 }
---

:::note
Generated at build time from \`hull --help\`. Edit \`python/multihull/cli.py\`, not this page.
:::

Install with \`uv tool install multihull\`. Every command accepts \`--help\`.

## hull

${code(rootHelp, 'text')}

## Commands

${sections.join('\n\n')}
`;
}

const schema = JSON.parse(hull(['schema']));
const rootHelp = cleanHelp(hull(['--help']));
const commands = commandNames(rootHelp).map((name) => [name, cleanHelp(hull([name, '--help']))]);
if (commands.length === 0) throw new Error('no commands found in hull --help output');

mkdirSync(referenceDir, { recursive: true });
const schemaTarget = resolve(referenceDir, 'spec-schema.mdx');
const cliTarget = resolve(referenceDir, 'cli.mdx');
writeFileSync(schemaTarget, renderSchemaPage(schema));
writeFileSync(cliTarget, renderCliPage(rootHelp, commands));
console.log(`generated ${schemaTarget}`);
console.log(`generated ${cliTarget} (${commands.length} commands)`);
