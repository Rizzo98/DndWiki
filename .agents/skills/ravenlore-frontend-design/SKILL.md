---
name: ravenlore-frontend-design
description: Visual/component style guide (colors, typography, and chrome for panels, cards, buttons, tags, audio player, list rows, etc.) for "Ravenlore", a D&D-campaign wiki app built by uploading session audio recordings (the wiki auto-fills from transcripts/summaries). Use this skill whenever writing or styling ANY frontend component for this product — panels, cards, buttons, tags, the audio player, timestamp markers, nav items, stat tiles, list rows, etc. Always consult this skill before hand-rolling colors, radius, or component chrome from scratch, even for something that looks like "just a small panel" or "just a list item" — the point is to keep every component visually consistent with the reference mockups it was extracted from. This skill intentionally does NOT dictate page layout, composition, number of items, or where things are placed — that stays the coding agent's call; only apply it to how an individual component looks once the agent has decided to use one. Trigger on words like sidebar, wiki page, NPC card, session, audio player, timestamp marker, campaign dashboard, dark panel, stat card, breadcrumb, or any request to build/redesign UI in this app.
---

# Ravenlore Frontend Design System

Ravenlore is a dark-fantasy, parchment-and-firelight themed wiki builder for tabletop RPG campaigns. The core workflow is: DM uploads a session audio recording → the app transcribes/summarizes it → wiki pages (NPCs, places, factions, items) get created or updated from it. Because of that, **the audio player and timestamp-marker list are first-class UI citizens**, not an afterthought — treat them with the same care as the sidebar nav.

This skill encodes the visual language extracted from the two canonical mockups (campaign home dashboard, session detail page) so that every new screen "feels" like it belongs in the same app. Don't invent a new palette, radius scale, or card style for a new feature — extend the patterns below.

## Design philosophy in one paragraph

Two zones, two temperatures. **Dark, warm-black chrome** (sidebar, hero banners, audio player, "create new" panels) reads like leather-bound tomes and firelight — it's for navigation, branding, and media. **Warm parchment/cream** is the content surface — it's for reading and scanning wiki content. Never mix these randomly: a component is either "on dark" or "on parchment", and each has its own text/border rules (see tokens below). Accents are a single rust/terracotta orange used sparingly for primary actions, active states, and category emphasis — it should never be the dominant color of a screen, only the spark in it.

## Design tokens

Use these as CSS custom properties (or Tailwind theme extensions) — don't inline raw hex values in components.

```css
:root {
  /* Dark chrome (sidebar, hero, audio player, dark panels) */
  --rl-bg-dark-900: #18120d;   /* deepest bg: sidebar, audio player, dark panels */
  --rl-bg-dark-800: #221a14;   /* raised surface on dark, e.g. hero banner base */
  --rl-bg-dark-700: #2c221a;   /* hover / active nav item background */
  --rl-border-dark: #3a2c22;   /* hairline borders on dark surfaces */

  /* Parchment (main content surface) */
  --rl-bg-parchment: #f1e6d3;      /* app content background */
  --rl-bg-card: #faf4e8;           /* cards, panels sitting on parchment */
  --rl-border-parchment: #e3d5bb;  /* hairline borders on parchment */

  /* Accent — the single "fire" color, used sparingly */
  --rl-accent-500: #bd5a3a;   /* primary buttons, active nav text/icon, links */
  --rl-accent-600: #a44b2e;   /* accent hover/pressed */
  --rl-accent-100: #f3ddd0;   /* accent tint background (badges, active nav bg) */

  /* Text */
  --rl-text-on-dark-primary: #f3ece0;
  --rl-text-on-dark-muted: #a89484;
  --rl-text-on-parchment-primary: #2a2016;
  --rl-text-on-parchment-muted: #7d6d58;

  /* Category / tag colors (semantic, used for content-type labels & icons) */
  --rl-cat-villain: #b5442f;   /* villains / threats */
  --rl-cat-place: #7a4a33;    /* luoghi */
  --rl-cat-npc: #6c5a9e;      /* PNG / personaggi */
  --rl-cat-item: #c07a2e;     /* oggetti */
  --rl-cat-faction: #8a7130;  /* fazioni */
  --rl-cat-neutral: #4f7a5c;  /* stato neutrale / positivo */

  /* Radius & elevation */
  --rl-radius-sm: 8px;   /* buttons, tags, inputs */
  --rl-radius-md: 14px;  /* cards, list rows */
  --rl-radius-lg: 20px;  /* hero banners, big panels, thumbnails */
  --rl-shadow-card: 0 2px 10px rgba(30, 20, 10, 0.08);
  --rl-shadow-raised: 0 8px 24px rgba(20, 12, 6, 0.25); /* dark panels floating on parchment */
}
```

**Rule of thumb:** if a component sits directly on `--rl-bg-parchment`, its own background is `--rl-bg-card` with a `--rl-border-parchment` hairline and `--rl-shadow-card`. If a component sits on dark chrome, it's a flat fill from the dark scale with a `--rl-border-dark` hairline — dark-on-dark panels don't get drop shadows, they get a subtle lighter border instead.

## Typography

- **Display/headings** (page titles like "LE CENERI DI VALDRUN", session titles): a serif display face (e.g. Playfair Display / Cormorant / similar), often set in a mix of small-caps or letter-spaced uppercase for eyebrow labels above it. Headings are the *only* place serif appears.
- **Everything else** (nav, body copy, buttons, meta text, data): a clean grotesque sans (e.g. Inter / Söhne). Never use serif for UI chrome or body paragraphs.
- **Eyebrow labels** (section kickers like "CAMPAGNA ATTIVA · SESSIONE 14", "PAGINE RECENTI", nav group headers "PANORAMICA"/"IL MONDO"/"STRUMENTI"): uppercase, small (11–12px), letter-spacing ~0.08em, accent or muted color, sans-serif, semi-bold.
- Body/meta text stays muted (`--rl-text-on-*-muted`) — reserve full-strength text color for titles and primary labels.

## Layout is not this skill's job

This skill does **not** prescribe page structure: whether a screen has a sidebar, where a search bar lives, how many columns there are, what goes in a right rail, or how many buttons a panel contains. That composition is the coding agent's call, driven by the actual feature and content at hand — the reference mockups are one example arrangement, not a template to clone on every screen.

What this skill *does* govern is narrower and more durable: **whenever the agent decides to render a sidebar, a panel, a card, a button, a tag, an audio player, etc., that element should use the tokens and chrome rules below**, so that two screens built at different times by different prompts still look like the same product. Think of it as a component style guide, not a wireframe.

The one structural idea worth keeping in mind (not enforcing) is the dark/parchment split described in "Design philosophy" above — it's a coloring rule for individual surfaces, not a mandate that every page must have a dark sidebar or a fixed three-column shell.

## Component patterns

### Nav item (sidebar, tabs, or any dark navigation list — if/where the agent chooses to build one)
- Default: transparent background, icon + label in `--rl-text-on-dark-muted`.
- Active: background `--rl-bg-dark-700` (or a soft accent tint), full-radius (`--rl-radius-sm`) pill/row, icon + label switch to `--rl-accent-500` or `--rl-text-on-dark-primary`.
- Hover (non-active): background `--rl-bg-dark-800`.
- Group labels above each cluster of items, if the nav is grouped: eyebrow style, `--rl-text-on-dark-muted`, extra top margin to separate groups. Grouping itself, number of groups, and number of items are content-driven, not fixed by this skill.

### Buttons
- **Primary** (e.g. "+ Nuova Pagina", "Modifica Sessione"): filled `--rl-accent-500`, white/cream text, `--rl-radius-sm`, medium padding, hover → `--rl-accent-600`. Use for the single most important action in its context — there should rarely be more than one primary button visible at once in the same area.
- **Secondary/outline** (e.g. "Condividi"): transparent fill, 1px border in `--rl-border-parchment` (on parchment) or `--rl-border-dark` (on dark), text in primary text color.
- **Dark utility buttons** (e.g. quick-action buttons inside a dark panel): flat `--rl-bg-dark-800` fill, icon above label, center-aligned, `--rl-radius-sm`, hover lightens one step. How many there are, what they're labeled, and whether they're arranged in a grid, a row, or a stack is up to whatever the panel is for — the chrome rule is just fill/radius/hover, not a fixed count or grid shape.

### Stat card (any small "number + label" summary tile, wherever one is needed)
Small horizontal card: colored square icon chip (24–32px, `--rl-radius-sm`, one of the category colors at ~15% opacity tint as bg + full color icon) on the left, bold number + muted label stacked on the right. When several sit together, group them on a shared `--rl-bg-card` surface with hairline dividers or small gaps rather than individual heavy shadows — but whether/how many appear, and where, is up to the agent.

### Content/wiki card (any card representing a wiki entity — NPC, place, item, faction, session…)
- Rounded thumbnail image on top, if there is one (`--rl-radius-md`, full-bleed within card padding, fixed aspect ratio ~16:10).
- Below image: category eyebrow label in the matching `--rl-cat-*` color (uppercase, bold, small) → bold title → muted meta line ("Modificato 2 ore fa da Marco").
- Card bg `--rl-bg-card`, `--rl-radius-md`, `--rl-shadow-card`. Grid vs. list, column count, and where these cards appear are layout decisions left to the agent.

### Tag / badge
Small pill, `--rl-radius-sm` (fully rounded for pill look), uses a category or status color as either solid text-on-tint (bg = color at low opacity, text = full color) or, for status badges like "SESSIONE 14" / "CAMPAGNA ATTIVA", accent-on-tint uppercase small text.

### Hero / banner block
Dark image banner (`--rl-radius-lg` on the outer container, or full-bleed at top of main content), gradient overlay from transparent to `--rl-bg-dark-900` at the bottom/edges so text stays legible. Content stacked bottom-left: eyebrow badge line → big serif title → 1–2 lines of muted description in a lighter tone (not full white — slightly muted for hierarchy).

### List row with avatar (any panel listing people, NPCs, or items — group members, encounters, loot, etc.)
Horizontal row: circular avatar (or icon chip for non-person items like loot) → name/title (bold) + secondary line (role/class in muted or category color) stacked → optional chevron or action on the far right. Rows are separated by hairline dividers inside one panel, not individually boxed. Which lists exist and where they're placed is content-driven.

### Audio player (session recordings — important, product-defining component)
This is the app's signature component; give it weight and polish equal to the sidebar.
- Full-width dark panel (`--rl-bg-dark-900`, `--rl-radius-md` or `--rl-radius-lg`).
- Header row inside: large circular play/pause button (white fill, dark icon, most prominent element) + track title (bold, on-dark) + subtitle meta ("AUDIO DELLA SESSIONE · 324 MB · MP3", muted, uppercase eyebrow style) + utility icons (download, volume) aligned right, plus skip-forward/back icons flanking play.
- Scrubber: thin horizontal track, filled progress in `--rl-accent-500` or light cream, draggable handle, current/total time in monospace-ish small text at each end.
- **Timestamp markers list** directly below the player, inside the same panel or a connected one: each row = timestamp chip (accent color, tabular/monospace numerals, e.g. `00:15:20`) + short label describing the moment + a small play/jump button on the right that seeks the player to that time. End the list with a low-emphasis "+ Aggiungi marcatore DM" row (dashed border or ghost style) so DMs can add their own markers — this affordance should always be present on session pages, even when the list is empty.

### Breadcrumb
Muted text, `>` separators, only the final (current page) segment is bold/full-contrast. Renders on a parchment surface, wherever the agent places page-location context.

### Search input
Pill-shaped, light neutral fill slightly darker than parchment, placeholder muted, small search icon leading. Placement (topbar, sidebar, a command palette, etc.) is up to the agent.

## When building a new screen or component

1. Design the layout freely — number of columns, whether there's a sidebar/right rail, what goes where, how many items a panel holds. That's a product/UX decision for the agent to make per-feature, not something this skill dictates.
2. Whatever elements you do decide to render, reach for the matching pattern above instead of inventing new chrome. A new "NPC relationship graph" panel, for example, is still "a dark or light panel with a header row and content inside" — reuse the panel/card/list rules, not the specific example layouts shown for them.
3. Decide dark-chrome vs parchment-surface per element — that decision drives every other token choice for it.
4. Keep the accent color rare: one primary action, active/selected states, key badges/links. If a screen feels "orange everywhere," pull it back.
5. Serif is for titles only. Don't let it leak into buttons, nav, or body copy.
6. New content types need a `--rl-cat-*` color — pick a new hue consistent with the muted, earthy palette (avoid saturated neon colors; everything here is desaturated/warm).

See `references/components.md` for ready-to-adapt HTML/CSS snippets of the components above (sidebar nav, stat card, content card, audio player, timestamp marker row, list row).
