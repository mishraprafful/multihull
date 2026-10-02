# Website refresh: a quieter Open water

Proposal for the docs site landing page and chrome. Keeps the Open water palette, Inter, JetBrains Mono, and the hero and fan-out illustrations. Changes how much is on screen at once and how loudly each element speaks.

## What Modal does well

- One ground colour per theme. The header, body and code share a background; sections are separated by whitespace, not panels.
- One accent, used for one job. Green marks the primary action and little else. Everything else is text colour or grey.
- A short type scale. A large headline, a quiet body size, small mono labels. Few sizes in between, so nothing competes.
- Code sits in the page. A plain monospace block with a thin edge and no chrome, so the code reads as content rather than a widget.
- Rhythm comes from gaps, not rules. Section spacing is wide and consistent; rules and borders appear only where two things must be told apart.

## What the current site does that fights minimalism

- Two hero visuals stacked. `website/src/content/docs/index.mdx` sets `hero.image` to a 25rem mark, then `HeroFailover` follows. The mark repeats the header logo at a hundred times the size.
- Accent everywhere. `website/src/styles/theme.css` puts teal on the dividers (2px gradient at 0.7 opacity in `.mh-horizon`), on every card (`border-top: 3px` in `.mh-value-props > div`), on the site title, on links and on the button. No single thing is emphasised.
- Cards with full chrome. `.mh-value-props > div` has fill, border, accent bar and radius. Three identical boxes beside a large illustration read as clutter.
- Sand carries too much in light mode. In `theme.css` the same `#e9dcc3` is the nav, the sidebar, the inline code, the card surface, the code block and `gray-6`, so the light page has little contrast between ground and surface.
- Uniform, tight spacing and oversized headings. Starlight's `1.5rem` between blocks is unchanged, so the illustration, cards, divider and h2 run together, while `## A minimal spec` renders at 42px (`--sl-text-h2` at desktop) and `hull status` in the h2 wears an inline-code box.

## Proposed changes

| Change | Why | Where |
|---|---|---|
| Drop `hero.image`; the hero is title, tagline and two actions | One hero visual (the failover illustration), no duplicated mark | `website/src/content/docs/index.mdx` |
| Section gap `clamp(3.5rem, 8vw, 6rem)` between landing sections via `.mh-section` | Rhythm from whitespace; each block gets its own air | `index.mdx`, `theme.css` |
| Landing h2 at `--sl-text-2xl`, weight 600, tracking -0.01em, plus a one-line lead in `gray-2` | Headings label sections instead of shouting | `theme.css` |
| Inline code inside headings: no background, no padding | Removes the box around `hull status` | `theme.css` |
| Value cards become three plain columns with a 1px hairline top, base-size h3, small grey body | Repeated elements without card chrome; copy shortened to one line each | `theme.css`, `index.mdx` |
| Horizon divider: 1px, hairline colour with a faint teal centre, one use on the landing page | Keeps the motif, stops it competing with content | `theme.css`, `Horizon.astro` usage in `index.mdx` |
| Code block: hairline border, `0.5rem` radius, surface background, title tab as a mono grey label, no accent indicator, more inner padding | Code reads as content; one surface tone | `theme.css` (Expressive Code `--ec-*` vars) |
| Status table in mono at `--sl-text-sm`, uppercase 12px grey headers with tracking, hairline rows, pills at 12px with 50% borders | CLI output as brand, quieter rows | `theme.css` |
| Header 3.5rem at all widths, background equals page background, hairline bottom border, site title in text colour | One ground; teal reserved for actions and links | `theme.css` |
| Sidebar 17rem, links at `--sl-text-sm`, current page as 14% teal tint with teal text instead of a solid teal block | Less weight in the chrome | `theme.css` |
| Links underlined with 1px at 45% opacity, full on hover; focus ring 2px teal, 3px offset | Visible but calm; accessible focus | `theme.css` |
| Buttons `0.5rem` radius, weight 500; minimal variant underlines on hover | Fewer pills, consistent with code and card radii | `theme.css` |
| Landing content width `62rem` on `[data-has-hero]` | One consistent measure for every landing section | `theme.css` |
| Token: dark `--sl-color-bg-nav` and `--sl-color-bg-sidebar` `#0d2235` to `#0b1d2e` | Header and sidebar share the navy ground | `theme.css` |
| Token: dark `--mh-surface` `#122839` to `#0f2436` | Surfaces step up less from the ground | `theme.css` |
| Token: dark `--sl-color-hairline` `#223548` to `#1c3042`; `--sl-color-hairline-shade` `#0a1826` to `#1c3042` | Softer rules, one hairline tone | `theme.css` |
| Token: new neutral `--mh-slate`, `#8495a3` dark and `#647685` light, used as `gray-3` (was `#8c9aa6` and `#6a7c8b`) | A deliberate blue-grey for captions and labels | `theme.css` |
| Token: light `--sl-color-bg-nav` `#e9dcc3` to `#faf7f0`; `--sl-color-bg-sidebar` `#f1ead9` to `#f6f1e6` | Light chrome shares the foam ground | `theme.css` |
| Token: light `--mh-surface` `#e9dcc3` to `#f3ede0`; `--sl-color-gray-6` `#e9dcc3` to `#f1ead9`; `--sl-color-gray-7` `#f1ead9` to `#f6f1e6`; `--sl-color-bg-inline-code` `#e9dcc3` to `#efe6d2` | Sand retreats to borders and small surfaces, contrast between ground and surface grows | `theme.css` |
| Token: light `--sl-color-hairline` `#d9ccb2` to `#e0d5bd`; `--sl-color-hairline-shade` `#cfc1a4` to `#e0d5bd` | Softer rules in light | `theme.css` |

Teal `#1fb6a6` (light `#158f83`), coral `#ff6b57`, amber `#f5b841`, navy `#0b1d2e`, foam `#f4f1ea` and sand `#e9dcc3` keep their values. Emphasis moves: teal marks the primary action, links and the healthy state only.

## What stays the same

- Palette names and values; Inter and JetBrains Mono; dark default with light as a first-class theme.
- The splash tagline, the sidebar structure and `astro.config.mjs`.
- `HeroFailover.astro` and `SpecToProviders.astro` internals. They inherit the new surface and hairline tokens and nothing else changes.
- Status pills and asides keep their semantic colours.
- Docs pages keep Starlight's layout; they only pick up the token, header, sidebar, link, code and table changes.

## How to evaluate on the preview

1. Open the home page in dark, then light. Count the teal elements above the fold: the Quickstart button, links and the illustration's healthy state should be the only ones.
2. Scroll the landing page. Each section should sit in its own space; no two blocks should touch.
3. Check the light theme: header, body and code should read as one ground, with sand only on borders, inline code and the code block.
4. Resize to 360px. The hero stacks, the illustration scales, the columns stack, the table scrolls inside itself and the page does not scroll sideways.
5. Open Quickstart. The sidebar current page is a tint, the code blocks have a thin edge, links underline, and Tab shows a visible focus ring.
6. Compare with `main` for anything that got worse: contrast of grey text on both grounds, pill legibility, illustration labels.
