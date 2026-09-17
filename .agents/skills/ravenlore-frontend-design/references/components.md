# Ravenlore component snippets

Reference markup/CSS for the patterns described in SKILL.md. These are starting points to adapt to the actual framework in use (React/Vue/plain HTML) — keep the class names as a naming convention if nothing better exists yet, but the important part is the token usage and structure, not the exact class names.

Assumes the CSS custom properties from SKILL.md's "Design tokens" section are already defined on `:root`.

**Note on the examples below:** the specific content shown (item counts, labels, which panels exist, where they'd sit on a page) is illustrative only, taken from the reference mockups. What's fixed is the chrome — colors, radius, spacing rhythm, states. Number of items, grid vs. list vs. stack, and page placement are all for the agent to decide per feature.

## Sidebar nav item

```html
<a class="rl-nav-item rl-nav-item--active">
  <svg class="rl-nav-icon">...</svg>
  <span>Home Campagna</span>
</a>
<a class="rl-nav-item">
  <svg class="rl-nav-icon">...</svg>
  <span>Sessioni</span>
</a>
```

```css
.rl-nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: var(--rl-radius-sm);
  color: var(--rl-text-on-dark-muted);
  font-size: 14px;
  font-weight: 500;
  transition: background-color .15s ease, color .15s ease;
}
.rl-nav-item:hover { background: var(--rl-bg-dark-800); }
.rl-nav-item--active {
  background: var(--rl-bg-dark-700);
  color: var(--rl-accent-500);
}
.rl-nav-group-label {
  font-size: 11px;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--rl-text-on-dark-muted);
  font-weight: 600;
  margin: 18px 12px 6px;
}
```

## Stat card row

```html
<div class="rl-stat-row">
  <div class="rl-stat-card">
    <div class="rl-stat-icon" style="--tint: var(--rl-cat-villain)">📄</div>
    <div>
      <div class="rl-stat-number">86</div>
      <div class="rl-stat-label">Pagine wiki</div>
    </div>
  </div>
  <!-- repeat for other stats -->
</div>
```

```css
.rl-stat-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  background: var(--rl-bg-card);
  border-radius: var(--rl-radius-md);
  box-shadow: var(--rl-shadow-card);
}
.rl-stat-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 18px 20px;
  border-right: 1px solid var(--rl-border-parchment);
}
.rl-stat-card:last-child { border-right: none; }
.rl-stat-icon {
  width: 36px; height: 36px;
  display: grid; place-items: center;
  border-radius: var(--rl-radius-sm);
  background: color-mix(in srgb, var(--tint) 15%, transparent);
  color: var(--tint);
  font-size: 16px;
}
.rl-stat-number { font-weight: 700; font-size: 18px; color: var(--rl-text-on-parchment-primary); }
.rl-stat-label { font-size: 13px; color: var(--rl-text-on-parchment-muted); }
```

## Content / wiki card

```html
<article class="rl-card">
  <img class="rl-card-thumb" src="..." alt="" />
  <div class="rl-card-body">
    <span class="rl-tag" style="--cat: var(--rl-cat-villain)">Villain</span>
    <h3 class="rl-card-title">Re Drago Morvain</h3>
    <p class="rl-card-meta">Modificato 2 ore fa da Marco</p>
  </div>
</article>
```

```css
.rl-card {
  background: var(--rl-bg-card);
  border-radius: var(--rl-radius-md);
  box-shadow: var(--rl-shadow-card);
  overflow: hidden;
}
.rl-card-thumb { width: 100%; aspect-ratio: 16/10; object-fit: cover; }
.rl-card-body { padding: 14px 16px; }
.rl-card-title { font-weight: 700; color: var(--rl-text-on-parchment-primary); margin: 4px 0 2px; }
.rl-card-meta { font-size: 12px; color: var(--rl-text-on-parchment-muted); }
.rl-tag {
  display: inline-block;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .04em;
  text-transform: uppercase;
  color: var(--cat);
}
```

## Dark utility panel

A dark panel is just a container: title + some content, flat dark fill, no card shadow (it gets `--rl-shadow-raised` instead since it usually floats on parchment). What goes inside — a grid of quick-create buttons, a single action, a short list — is entirely up to the feature. The example below shows a grid of N action buttons purely to illustrate the button chrome; don't treat "2×2" or these specific labels as a requirement.

```html
<div class="rl-dark-panel">
  <h4 class="rl-dark-panel-title">{{ panel title, e.g. "Crea Nuovo" }}</h4>
  <!-- Layout of the content below (grid, row, stack, single button...) is up to the agent. -->
  <div class="rl-dark-actions">
    <button class="rl-dark-action">{{ icon }} {{ label }}</button>
    <!-- ...as many or as few actions as the feature needs -->
  </div>
</div>
```

```css
.rl-dark-panel {
  background: var(--rl-bg-dark-900);
  border-radius: var(--rl-radius-md);
  padding: 16px;
  box-shadow: var(--rl-shadow-raised);
}
.rl-dark-panel-title {
  font-size: 12px; letter-spacing: .08em; text-transform: uppercase;
  color: var(--rl-text-on-dark-muted); margin-bottom: 12px;
}
/* .rl-dark-actions: choose whatever layout (grid/flex/stack) fits the content */
.rl-dark-actions { display: grid; grid-template-columns: repeat(auto-fit, minmax(90px, 1fr)); gap: 10px; }
.rl-dark-action {
  background: var(--rl-bg-dark-800);
  border-radius: var(--rl-radius-sm);
  color: var(--rl-text-on-dark-primary);
  padding: 16px 8px;
  text-align: center;
  font-size: 13px;
  transition: background-color .15s ease;
}
.rl-dark-action:hover { background: var(--rl-bg-dark-700); }
```

## List row with avatar (Il Gruppo / Incontri)

```html
<ul class="rl-list-panel">
  <li class="rl-list-row">
    <img class="rl-avatar" src="..." alt="" />
    <div class="rl-list-row-text">
      <div class="rl-list-row-title">Aeliana Duskwalker</div>
      <div class="rl-list-row-sub">Ranger Elfa · Lv 9</div>
    </div>
    <svg class="rl-chevron">...</svg>
  </li>
  <!-- more rows, divided by hairline -->
</ul>
```

```css
.rl-list-panel { background: var(--rl-bg-card); border-radius: var(--rl-radius-md); box-shadow: var(--rl-shadow-card); }
.rl-list-row {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--rl-border-parchment);
}
.rl-list-row:last-child { border-bottom: none; }
.rl-avatar { width: 36px; height: 36px; border-radius: 50%; object-fit: cover; }
.rl-list-row-title { font-weight: 600; font-size: 14px; color: var(--rl-text-on-parchment-primary); }
.rl-list-row-sub { font-size: 12px; color: var(--rl-text-on-parchment-muted); }
.rl-chevron { margin-left: auto; color: var(--rl-text-on-parchment-muted); }
```

## Audio player + timestamp markers

This is the product-defining component — the whole app starts from an uploaded session recording, so treat it as a primary surface, not a generic `<audio>` tag.

```html
<div class="rl-audio-panel">
  <div class="rl-audio-header">
    <button class="rl-play-btn" aria-label="Play">▶</button>
    <div>
      <div class="rl-audio-title">Registrazione Integrale</div>
      <div class="rl-audio-sub">Audio della sessione · 324 MB · MP3</div>
    </div>
    <div class="rl-audio-utils">
      <button aria-label="Volume">🔊</button>
      <button aria-label="Download">⬇️</button>
    </div>
  </div>

  <div class="rl-scrubber">
    <span class="rl-time">01:12:45</span>
    <input type="range" class="rl-scrubber-track" min="0" max="100" value="30" />
    <span class="rl-time">03:45:12</span>
  </div>

  <ul class="rl-marker-list">
    <li class="rl-marker-row">
      <span class="rl-marker-time">00:15:20</span>
      <span class="rl-marker-label">L'incontro con la spia nella taverna</span>
      <button class="rl-marker-play" aria-label="Vai a questo punto">▶</button>
    </li>
    <li class="rl-marker-row">
      <span class="rl-marker-time">01:45:00</span>
      <span class="rl-marker-label">Inizio dell'assedio alle Torri Grigie</span>
      <button class="rl-marker-play" aria-label="Vai a questo punto">▶</button>
    </li>
    <li class="rl-marker-row rl-marker-row--add">
      <span>+ Aggiungi marcatore DM</span>
    </li>
  </ul>
</div>
```

```css
.rl-audio-panel {
  background: var(--rl-bg-dark-900);
  border-radius: var(--rl-radius-lg);
  padding: 20px;
}
.rl-audio-header { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; }
.rl-play-btn {
  width: 48px; height: 48px; border-radius: 50%;
  background: var(--rl-text-on-dark-primary);
  color: var(--rl-bg-dark-900);
  display: grid; place-items: center; font-size: 16px;
  flex-shrink: 0;
}
.rl-audio-title { font-weight: 700; color: var(--rl-text-on-dark-primary); }
.rl-audio-sub {
  font-size: 11px; letter-spacing: .06em; text-transform: uppercase;
  color: var(--rl-text-on-dark-muted); margin-top: 2px;
}
.rl-audio-utils { margin-left: auto; display: flex; gap: 10px; color: var(--rl-text-on-dark-muted); }

.rl-scrubber { display: flex; align-items: center; gap: 10px; margin-bottom: 18px; }
.rl-time { font-size: 11px; color: var(--rl-text-on-dark-muted); font-variant-numeric: tabular-nums; }
.rl-scrubber-track { flex: 1; accent-color: var(--rl-accent-500); }

.rl-marker-list { border-top: 1px solid var(--rl-border-dark); padding-top: 10px; }
.rl-marker-row {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 6px; border-radius: var(--rl-radius-sm);
}
.rl-marker-row:hover { background: var(--rl-bg-dark-800); }
.rl-marker-time {
  font-variant-numeric: tabular-nums;
  color: var(--rl-accent-500);
  font-size: 12px; font-weight: 600;
  min-width: 56px;
}
.rl-marker-label { color: var(--rl-text-on-dark-primary); font-size: 13px; flex: 1; }
.rl-marker-play { color: var(--rl-text-on-dark-muted); font-size: 12px; }
.rl-marker-row--add {
  border: 1px dashed var(--rl-border-dark);
  color: var(--rl-text-on-dark-muted);
  justify-content: center;
  font-size: 13px;
  margin-top: 4px;
}
```

## Hero banner

```html
<header class="rl-hero" style="background-image: url('...')">
  <div class="rl-hero-overlay">
    <span class="rl-hero-eyebrow">Campagna Attiva · Sessione 14</span>
    <h1 class="rl-hero-title">Le Ceneri di Valdrun</h1>
    <p class="rl-hero-desc">Un regno diviso dalla guerra civile...</p>
  </div>
</header>
```

```css
.rl-hero {
  position: relative;
  border-radius: var(--rl-radius-lg);
  background-size: cover;
  background-position: center;
  min-height: 220px;
  display: flex; align-items: flex-end;
}
.rl-hero-overlay {
  width: 100%;
  padding: 24px 28px;
  background: linear-gradient(to top, var(--rl-bg-dark-900) 0%, rgba(24,18,13,0.4) 60%, transparent 100%);
  border-radius: var(--rl-radius-lg);
}
.rl-hero-eyebrow {
  font-size: 11px; letter-spacing: .08em; text-transform: uppercase;
  color: var(--rl-accent-500); font-weight: 700;
}
.rl-hero-title {
  font-family: var(--rl-font-serif, 'Playfair Display', serif);
  font-size: 32px; color: var(--rl-text-on-dark-primary); margin: 6px 0 8px;
}
.rl-hero-desc { color: var(--rl-text-on-dark-muted); font-size: 14px; max-width: 60ch; }
```
