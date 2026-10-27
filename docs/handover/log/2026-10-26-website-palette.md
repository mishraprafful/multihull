# 2026-10-26 Website palette (PR 180)

- Old palette: teal `#1fb6a6` was accent, `ok`, water tint and diagram decoration at once; light mode used beige `#faf7f0` and `#f0e9da` with a cool navy ink; Starlight's default Night Owl code themes added yellow, orange and magenta. Measured failures: dark `--mh-strong` 2.2:1 and hero hull outlines 2.1:1 against their backgrounds, the light logo 2.4:1, light muted text on panels 4.4:1.
- New palette: slate-navy neutrals at hue 210 to 215 in both modes, teal accent `#2ec4b6` dark and `#0b7266` light, `ok` green, `warn` amber, `down` coral used only by status pills, the hero status marks and asides. Starlight's orange (version notice) maps to `warn`.
- Code: `expressiveCode.themes` is `['nord', 'github-light-default']` with `useStarlightUiThemeColors: true`, so frames keep the site's colours.
- The demo GIF, launch video, poster and social preview were not regenerated; they keep navy `#0b1d2e` and teal `#1fb6a6`, close to the new dark background and accent.
