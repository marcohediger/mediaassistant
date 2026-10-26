# Design-System — MediaAssistant Web-UI

Ein gemeinsames Aussehen für alle Seiten. Vorbild sind die Listen und der
Set-Editor in den Einstellungen (Dockhand-Stil: ruhige Karten, luftige
Zeilen, Icons vor Namen, gedämpfte Nebentexte, Status mit Icon + Farbe,
rahmenlose Icon-Buttons).

**Referenzseite:** <http://localhost:8000/static/styleguide.html> — zeigt jede
Komponente in Dunkel und Hell (Umschalter oben rechts, `?theme=light`).

| Datei | Inhalt |
|---|---|
| `backend/static/css/style.css` | Theme-Farben, Element-Defaults, die bestehenden Klassen (`.btn`, `.form-group`, `.switch`, `.settings-section`, `.alert`, `.log-table` …) — modernisiert, ohne dass Markup angepasst werden muss |
| `backend/static/css/ui.css` | Design-Tokens (`--ui-*`) und alle gemeinsamen Komponenten (`.ui-*`) |
| `backend/static/css/light.css` | Hell-Theme: definiert nur die Farb-Variablen neu |
| `backend/templates/_icons.html` | SVG-Sprite, einmal pro Seite in `base.html` eingebunden |
| `backend/static/styleguide.html` | Statische Referenzseite (enthält eine Kopie des Sprites) |
| `lists.css`, `editor.css` | Nur Einstellungsseite (Listen, Set-Editor). Nicht für andere Seiten verwenden — dafür gibt es die `ui-*`-Gegenstücke. |

Ladereihenfolge in `base.html`: `style.css` → `ui.css` → (`light.css`).
Eine eigenständige Seite (z. B. Login) muss `style.css` **und** `ui.css`
laden, sonst fehlen die Tokens.

**Neue oder umgebaute Seiten nehmen die `ui-*`-Komponenten.** Die alten
Klassen in `style.css` bleiben für Bestandsmarkup.

---

## Regeln

1. **Nur Theme-Variablen und `--ui-*`-Tokens** für Farben, Radien, Abstände,
   Schatten. Keine Hex-Werte im Markup oder in Seiten-CSS. Ausnahme: `#fff`
   als Textfarbe auf gefüllten Buttons.
2. **Dunkel und Hell prüfen.** Hell-spezifische Werte stehen in `ui.css`
   unter `body[data-theme="light"]`.
3. **Icon-only-Buttons brauchen `aria-label`** (und `title` als Tooltip).
   Bei Listen-Aktionen den Namen anhängen: `aria-label="Löschen: IMG_3140.HEIC"`.
4. **Inline-SVG-Icons** aus dem Sprite, `stroke="currentColor"` (steckt im
   Symbol), immer `class="ui-i"`, `aria-hidden="true" focusable="false"`,
   Grösse über `width`/`height`. Neue Icons in `_icons.html` ergänzen
   (Lucide-Pfade, ohne `stroke-width`) **und** in `styleguide.html` nachziehen.
5. **Nutzerdaten per `textContent`, nie per `innerHTML`.** Dateinamen, Pfade,
   Tags, Fehlermeldungen kommen von aussen. `innerHTML` nur für feste,
   eigene Snippets (z. B. das `<svg><use>` eines Icons).
6. **Kein seitlicher Scroll der Seite bei 390 px.** Tabellen scrollen in
   ihrer Karte (`.ui-table-card`) oder werden mit `.ui-table-stack` zu
   Karten. Prüfen: `document.documentElement.scrollWidth <= innerWidth`.
7. **Sichtbarer Fokus.** Alle Komponenten haben einen `:focus-visible`-Ring
   (`--ui-focus-ring`). Nicht mit `outline: none` ohne Ersatz überschreiben.
8. **Texte über i18n:** `{{ t.bereich.schluessel | default('Deutscher Text') }}`,
   neue Schlüssel in `i18n/de.json` und `i18n/en.json`.
9. **Beschreibung neben Bedienelement:** Textspalte flexibel und umbrechend,
   Bedienelement `flex: none`, mindestens 1.5rem Abstand, Beschreibung max.
   60ch. `.ui-option` erledigt das — nicht nachbauen.
10. **Töne statt Farben:** `is-success`, `is-warning`, `is-error`, `is-info`,
    `is-accent`, `is-violet`, `is-neutral` auf tonfähigen Komponenten
    (`ui-status`, `ui-badge`, `ui-tag`, `ui-dot`, `ui-alert`, `ui-toast`,
    `ui-progress`, `ui-stat`, `ui-empty-icon`, `ui-icon-tile`, `ui-thumb`).
11. Statusfarben für **Text** über `--ui-ok-text`, `--ui-warn-text`,
    `--ui-err-text`, `--ui-info-text` (im Hell-Theme dunkler, 4.5:1).
    Für **Flächen/Punkte** die Basisfarben `--success`, `--warning`,
    `--error`, `--info`.

---

## Tokens (`ui.css`, `:root`)

| Gruppe | Tokens |
|---|---|
| Schrift | `--ui-font`, `--ui-mono` · Grössen `--ui-fs-2xs` 11 · `-xs` 12 · `-sm` 13 · `-base` 14 · `-md` 15 · `-lg` 17 · `-xl` 20 · `-2xl` 26 · `-stat` 30 px · `--ui-lh` 1.55, `--ui-lh-tight` 1.3 |
| Abstand | `--ui-space-1` … `-10` (4-px-Raster: 4, 8, 12, 16, 20, 24, 32, 40) |
| Radius | `--ui-radius-xs` 4 · `-sm` 6 · `--ui-radius` 8 · `-md` 10 · `-lg` 12 (Karten) · `-xl` 14 (Dialoge) · `-pill` |
| Höhen | `--ui-ctl-h` 2.5rem (Formularfelder) · `-sm` 2.25rem (Buttons, Toolbar) · `-xs` 2rem (kleine Buttons, Icon-Buttons) |
| Linien/Flächen | `--ui-line` (Tabellen-/Zeilentrenner) · `--ui-card-border` · `--ui-border-strong` (Hover) · `--ui-hover` · `--ui-row-hover` · `--ui-sidebar` · `--ui-track` (Schalter aus) |
| Akzent/Töne | `--ui-accent-soft`, `--ui-accent-ring`, `--ui-danger-soft`, `--ui-danger-ring`, `--ui-danger-solid`, `--ui-ok-text`, `--ui-warn-text`, `--ui-err-text`, `--ui-info-text`, `--ui-violet`, `--ui-teal` |
| Schatten | `--ui-shadow-card` (dunkel: keiner) · `--ui-shadow-sm` · `--ui-shadow` (Menüs, Toasts) · `--ui-shadow-lg` (Dialoge) · `--ui-backdrop` · `--ui-focus-ring` |
| Layout | `--ui-content-w` 1200px · `--ui-gutter` 1.5rem (Handy 1rem) |

Theme-Farben (`style.css`, in `light.css` überschrieben): `--bg`, `--bg-card`,
`--bg-item`, `--bg-input`, `--border`, `--text`, `--text-muted`, `--primary`,
`--primary-hover`, `--success`, `--warning`, `--error`, `--info`.

---

## Komponenten

Snippets sind kopierfertig; Texte in Templates über i18n ersetzen.
`{I:name}` steht kurz für
`<svg class="ui-i" width="16" height="16" aria-hidden="true" focusable="false"><use href="#ui-i-name"/></svg>`,
`{I:name 18}` für dasselbe mit `width="18" height="18"`.

### Icons
```html
<svg class="ui-i" width="16" height="16" aria-hidden="true" focusable="false"><use href="#ui-i-trash"/></svg>
```
Grössen: 14 klein (in `ui-btn-sm`, Badges 12), 16 Standard, 18 Aktions-Buttons,
20–22 gross. `.ui-spin` dreht (`ui-i-loader`). Vorhandene Namen: siehe
Styleguide › Icons (u. a. dashboard, activity, images, image, video, folder,
copy, trash, pencil, check, x, alert-triangle, alert-circle, check-circle,
info, clock, search, filter, refresh, retry, play, pause, download, upload,
external-link, chevron-left/right/down/up, more-horizontal, eye, tag,
file-text, list, grid, cloud, cloud-up/down, cpu, key, user, globe, settings,
scroll, wrench, star, map-pin, merge, loader).

`.ui-icon-tile` (+ `-sm`, `-lg`, Ton) = Icon in getöntem Quadrat.

### Seitenkopf
```html
<div class="ui-page-head">
  <div class="ui-page-head-text">
    <a class="ui-page-back" href="/logs">{I:chevron-left}Logs</a>   <!-- optional -->
    <h1 class="ui-page-title">Duplikate <span class="ui-badge is-warning">211 offen</span></h1>
    <p class="ui-page-sub">Eine Zeile, was die Seite tut.</p>
  </div>
  <div class="ui-page-actions">
    <button type="button" class="ui-btn ui-btn-secondary">{I:refresh}Neu laden</button>
    <button type="button" class="ui-btn ui-btn-primary">Jetzt scannen</button>
  </div>
</div>
```

### Abschnittskopf
```html
<section class="ui-section">
  <div class="ui-section-head">
    <div class="ui-section-head-text">
      <h2 class="ui-section-title">Letzte Verarbeitungen</h2>
      <p class="ui-section-desc">Kurzbeschreibung.</p>
    </div>
    <a class="ui-btn ui-btn-ghost ui-btn-sm" href="/logs">Alle Logs</a>
  </div>
  …
</section>
```

### Karte
```html
<div class="ui-card">
  <div class="ui-card-head">
    <div class="ui-card-head-text">
      <h3 class="ui-card-title">{I:tag}Tag-Aufräumen</h3>
      <p class="ui-card-desc">Eine Zeile Beschreibung.</p>
    </div>
    <div class="ui-card-actions">…</div>
  </div>
  <div class="ui-card-body">…</div>
  <div class="ui-card-foot ui-card-foot-end">…Buttons…</div>
</div>
```
Varianten: `a.ui-card` (klickbar), `.ui-card-dashed`, `.ui-card-body-lg`.
Layout: `.ui-stack` (`-xs/-sm/-lg/-xl`), `.ui-row`, `.ui-spacer`, `.ui-grid`
(auto-fit, Mindestbreite über `style="--ui-min:18rem"`), `.ui-grid-2/3/4`.

### Tabelle
```html
<div class="ui-table-card">
  <table class="ui-table ui-table-nowrap" aria-label="…">
    <thead><tr>
      <th scope="col">Datei</th><th scope="col">Status</th>
      <th scope="col" class="ui-num">Grösse</th>
      <th scope="col" class="ui-col-actions">Aktionen</th>
    </tr></thead>
    <tbody>
      <tr>
        <th scope="row"><div class="ui-cell-name">{I:image}<strong>IMG_3140.HEIC</strong></div></th>
        <td><span class="ui-status is-success">{I:check-circle}Fertig</span></td>
        <td class="ui-num">3.4 MB</td>
        <td class="ui-col-actions"><div class="ui-actions">
          <button type="button" class="ui-icon-btn" aria-label="Bearbeiten: IMG_3140.HEIC" title="Bearbeiten">{I:pencil 18}</button>
          <button type="button" class="ui-icon-btn is-danger" aria-label="Löschen: IMG_3140.HEIC" title="Löschen">{I:trash 18}</button>
        </div></td>
      </tr>
      <tr class="ui-table-empty"><td colspan="4">Keine Einträge</td></tr>
    </tbody>
  </table>
</div>
```
- Zellen sind standardmässig gedämpft; Hauptinhalt `td.ui-cell-text`,
  Namen in `.ui-cell-name strong`.
- Zellbausteine: `.ui-cell-meta` (Icon + Text), `.ui-cell-stack`,
  `.ui-cell-sub`, `.ui-cell-thumb` (`<img>` 36 px), `.ui-cell-wrap`
  (lange Texte umbrechen), `.ui-num`, `.ui-col-check`, `.ui-col-icon`.
- Zeilen: `.is-inactive`, `.is-selected`, `.is-link`.
- Modifikatoren: `.ui-table-nowrap`, `.ui-table-compact` (Logs),
  `.ui-table-stack` (Handy: jede Zeile eine Karte, Beschriftung aus
  `data-label` an jeder `td`).
- In einer Karte mit Kopf: `<div class="ui-table-scroll"><table class="ui-table">`.

### Status, Badge, Tag, Punkt, Zähler
```html
<span class="ui-status is-error">{I:alert-circle}Fehler</span>
<span class="ui-status-stack">
  <span class="ui-status is-error">{I:wifi-off}Nicht erreichbar</span>
  <span class="ui-status-reason" title="…">Connection refused</span>
</span>
<span class="ui-badge">XMP</span>                 <!-- neutral = Rahmen -->
<span class="ui-badge is-success"><span class="ui-dot is-success"></span>Aktiv</span>
<span class="ui-badge ui-badge-lg is-success">…</span>
<div class="ui-tags"><span class="ui-tag">Berg</span><span class="ui-tag is-success">+ GPS</span></div>
<span class="ui-dot is-warning"></span>  <span class="ui-dot is-hollow"></span>  <span class="ui-dot is-info is-pulse"></span>
<span class="ui-count">12</span>
```
Job-Status-Zuordnung: done → success (`check-circle`), queued → warning
(`clock`), processing → info (`loader` + `ui-spin`), error → error
(`alert-circle`), duplicate → warning (`copy`), review → violet (`eye`),
skipped/inaktiv → neutral (`pause-circle`).

### Buttons
```html
<button type="button" class="ui-btn ui-btn-primary">Speichern</button>
<button type="button" class="ui-btn ui-btn-secondary">{I:refresh}Neu laden</button>
<button type="button" class="ui-btn ui-btn-ghost">Zurücksetzen</button>
<button type="button" class="ui-btn ui-btn-danger">{I:trash}Löschen</button>
<button type="button" class="ui-btn ui-btn-danger-solid">Endgültig löschen</button>
<button type="button" class="ui-btn ui-btn-ghost-danger">Entfernen</button>
<button type="button" class="ui-btn ui-btn-new">{I:plus}Neues Set</button>
```
Grössen `.ui-btn-lg`, `.ui-btn-sm`, `.ui-btn-xs`, `.ui-btn-block`; Gruppe
`.ui-btn-row`. Zustände: `disabled` / `aria-disabled="true"`,
`aria-busy="true"` (dazu `ui-i-loader` mit `ui-spin`). Funktioniert auch auf `<a>`.

### Icon-Buttons
```html
<button type="button" class="ui-icon-btn" aria-label="Duplizieren: Standard" title="Duplizieren">
  <svg class="ui-i" width="18" height="18" aria-hidden="true" focusable="false"><use href="#ui-i-copy"/></svg>
</button>
```
`.is-danger` (rot beim Hover), `aria-pressed="true"` (Umschalter),
`.ui-icon-btn-sm`, `.ui-icon-btn-outline` (mit Rahmen, neben Eingabefeldern),
`.ui-icon-btn-spacer` (Platzhalter, damit Spalten bündig bleiben).
Mehrere rechtsbündig: `<div class="ui-actions">`.

### Toolbar / Filter
```html
<form class="ui-toolbar" method="get" role="search">
  <label class="ui-search"><span class="ui-sr-only">Suchen</span>{I:search}
    <input class="ui-input" type="search" name="q" placeholder="Suchen …"></label>
  <select class="ui-select" name="level" aria-label="Level">…</select>
  <div class="ui-segmented" role="group" aria-label="Status">
    <a class="ui-seg" href="?s=all" aria-current="page">Alle <span class="ui-count">2085</span></a>
    <a class="ui-seg" href="?s=error">Fehler</a>
  </div>
  <button type="submit" class="ui-btn ui-btn-secondary">{I:filter}Filtern</button>
  <span class="ui-toolbar-meta">1428 Einträge</span>
</form>
```
Pagination: `<nav class="ui-pagination">` mit `ui-btn ui-btn-secondary ui-btn-sm`
und `<span class="ui-pagination-info">Seite 1 / 29</span>`.

### Formularfelder
```html
<div class="ui-fields">                              <!-- 2 Spalten, Handy 1 -->
  <div class="ui-field">
    <label class="ui-label" for="url">Immich URL <span class="ui-req" aria-hidden="true">*</span></label>
    <input class="ui-input" id="url" type="url">
    <p class="ui-hint">Hinweis unter dem Feld.</p>
  </div>
  <div class="ui-field">
    <label class="ui-label" for="re">Muster</label>
    <input class="ui-input ui-input-mono" id="re" aria-invalid="true" aria-describedby="re-err">
    <p class="ui-field-error" id="re-err">{I:alert-circle}Klammer nicht geschlossen.</p>
  </div>
  <div class="ui-field ui-span-all">
    <label class="ui-label" for="p">Prompt</label>
    <textarea class="ui-textarea ui-input-mono" id="p" rows="6"></textarea>
  </div>
</div>
```
- `.ui-select` (eigener Pfeil), `.ui-textarea`, `.ui-input-sm`,
  `.ui-input-mono`, `.ui-input-narrow` (Zahl + Einheit:
  `<div class="ui-inline"><input class="ui-input ui-input-narrow" type="number"><span class="ui-muted">px</span></div>`).
- Zeit: `<input class="ui-input" type="time">` (nativer Picker; nicht in
  `.ui-input-icon` stecken, sonst zwei Uhren).
- `.ui-input-group` (Feld + Button), `.ui-input-icon` (Icon links im Feld),
  `.ui-check` (native Checkbox + Text), `.ui-form` (Spalte mit 1.25rem Abstand),
  `.ui-fields-3`.
- Nicht mit `.form-group` mischen (deren Regeln sind spezifischer).

### Optionszeilen (Titel + Beschreibung links, Bedienelement rechts)
```html
<div class="ui-option-list">                  <!-- in einer Karte ohne Rahmen: class="ui-option-list is-flush" -->
  <label class="ui-option">
    <span class="ui-option-text">
      <span class="ui-option-title">JPG+RAW Paare beide übernehmen</span>
      <span class="ui-option-desc">Aus = Paare landen im Duplikat-Review.</span>
    </span>
    <span class="ui-switch"><input type="checkbox" role="switch" name="…"><span class="ui-switch-track"></span></span>
  </label>
  <div class="ui-option">                     <!-- andere Bedienelemente: div + label for -->
    <span class="ui-option-text">
      <label class="ui-option-title" for="scale">Skalierung</label>
      <span class="ui-option-desc">…</span>
    </span>
    <div class="ui-option-control ui-option-control-wide"><select class="ui-select" id="scale">…</select></div>
  </div>
</div>
```
Auf dem Handy rutscht ein breites Bedienelement unter den Text.
`.is-disabled` auf der Zeile für gesperrte Optionen.

### Schalter
```html
<span class="ui-switch"><input type="checkbox" role="switch" aria-label="Geocoding"><span class="ui-switch-track"></span></span>
<label class="ui-switch-label">
  <span class="ui-switch"><input type="checkbox" role="switch" checked><span class="ui-switch-track"></span></span>
  <span>In XMP-Sidecars bereinigen</span>
</label>
```

### Auswahlkarten (Radio mit Beschreibung)
```html
<div class="ui-choices ui-choices-2" role="radiogroup" aria-label="Metadaten schreiben">
  <label class="ui-choice"><input type="radio" name="mode" value="file" checked>
    <span class="ui-choice-body"><span class="ui-choice-radio"></span>
      <span class="ui-choice-text"><span class="ui-choice-title">In die Datei</span>
        <span class="ui-choice-desc">ExifTool schreibt direkt in das Original.</span></span></span></label>
  …
</div>
```
`.ui-choices` (auto), `-2`, `-3`, `-4`; `.ui-choices.is-compact` ohne Beschreibung.

### Segmentierte Auswahl
```html
<div class="ui-segmented" role="group" aria-label="Ansicht">
  <button type="button" class="ui-seg" aria-pressed="true">{I:grid}Raster</button>
  <button type="button" class="ui-seg" aria-pressed="false">{I:list}Liste</button>
</div>
<!-- als Formularfeld -->
<div class="ui-segmented is-block" role="radiogroup" aria-label="Medientyp">
  <label class="ui-seg"><input type="radio" name="media" value="all" checked><span>Alle</span></label>
  <label class="ui-seg"><input type="radio" name="media" value="photo"><span>Fotos</span></label>
</div>
```
Aktiv: `aria-pressed="true"`, `aria-selected="true"`, `aria-current="page"`,
`.is-active` oder angehakter Radio.

### Kennzahlen
```html
<div class="ui-stats">
  <div class="ui-stat is-success">
    <span class="ui-stat-label">{I:check-circle}Fertig</span>
    <span class="ui-stat-value">1224</span>
    <span class="ui-stat-sub"><span class="ui-trend is-up">{I:trending-up 12}179</span>letzte 24 Std</span>
  </div>
  <a class="ui-stat is-error" href="/logs?status=error">…</a>   <!-- klickbar -->
</div>
```
Einheit: `<span class="ui-stat-value">179<span class="ui-stat-unit">/Std</span></span>`.
`.ui-stat.is-compact` für kleinere Kacheln. Der Ton färbt Zahl und Label-Icon.

### Fortschritt
```html
<div class="ui-progress-meta"><strong>Verwaiste aufräumen</strong><span class="ui-tabular">412 / 1000</span></div>
<div class="ui-progress" role="progressbar" aria-valuenow="41" aria-valuemin="0" aria-valuemax="100" aria-label="Fortschritt">
  <div class="ui-progress-bar" style="width:41%"></div>
</div>
```
`.ui-progress-sm`, `-lg`, Ton (`is-success` …), `.is-indeterminate` (unbekannte Dauer).

### Leerer Zustand
```html
<div class="ui-empty">
  <span class="ui-empty-icon is-success">{I:check-circle 22}</span>
  <p class="ui-empty-title">Keine offenen Duplikate</p>
  <p class="ui-empty-text">Neue Gruppen erscheinen hier, sobald die Pipeline sie findet.</p>
  <div class="ui-empty-actions"><a class="ui-btn ui-btn-secondary ui-btn-sm" href="/">Zum Dashboard</a></div>
</div>
<div class="ui-empty-inline">{I:key}<span>Keine Tokens vorhanden.</span></div>
```

### Hinweis / Banner
```html
<div class="ui-alert is-warning" role="status">
  <svg class="ui-i" width="18" height="18" aria-hidden="true" focusable="false"><use href="#ui-i-pause-circle"/></svg>
  <div class="ui-alert-body">
    <p class="ui-alert-title">Pipeline pausiert</p>
    <p class="ui-alert-text">Worker im Leerlauf (1 wartend).</p>
  </div>
  <div class="ui-alert-actions"><button type="button" class="ui-btn ui-btn-secondary ui-btn-sm">Fortsetzen</button></div>
</div>
```
Standardton ist `info`; `role="alert"` nur für Fehler, die sofort
vorgelesen werden sollen. `.ui-alert-sm`. Schliessen-Knopf: `ui-icon-btn
ui-icon-btn-sm` als letztes Kind.

### Toast
Einmal pro Seite:
```html
<div class="ui-toast-region" id="toasts" aria-live="polite"></div>
```
```js
function toast(text, tone) {            // tone: "success" | "error" | "warning" | "info"
  const icons = {success: "check-circle", error: "alert-circle", warning: "alert-triangle", info: "info"};
  const t = document.createElement("div");
  t.className = "ui-toast is-" + (tone || "info");
  t.setAttribute("role", tone === "error" ? "alert" : "status");
  t.innerHTML = '<svg class="ui-i" width="16" height="16" aria-hidden="true" focusable="false"><use href="#ui-i-'
              + (icons[tone] || "info") + '"/></svg>';            // fixed markup only
  const msg = document.createElement("span");
  msg.className = "ui-toast-text";
  msg.textContent = text;                                      // user data: textContent
  t.appendChild(msg);
  document.getElementById("toasts").appendChild(t);
  requestAnimationFrame(() => t.classList.add("is-shown"));
  setTimeout(() => { t.classList.remove("is-shown"); setTimeout(() => t.remove(), 250); }, 4000);
}
```

### Tabs und Seitennavigation
```html
<nav class="ui-tabs" aria-label="Logs">
  <a class="ui-tab" href="/logs?tab=system" aria-current="page">{I:scroll}System-Log</a>
  <a class="ui-tab" href="/logs?tab=jobs">{I:list}Verarbeitungs-Log <span class="ui-count">1428</span></a>
</nav>
```
JS-Tabs: `<button class="ui-tab" role="tab" aria-selected="true">`.
Seitennavigation (lange Bereichslisten, wird auf dem Handy zur waagrechten Leiste):
```html
<div class="ui-layout-sidenav">
  <nav class="ui-sidenav" aria-label="Bereiche">
    <p class="ui-sidenav-title">Gruppe</p>
    <a class="ui-sidenav-item" href="#a" aria-current="true">{I:sliders}<span class="ui-sidenav-label">Allgemein</span><span class="ui-sidenav-meta">3</span></a>
  </nav>
  <div>…Inhalt…</div>
</div>
```

### Code, Pfade
```html
<code class="ui-code">/library</code>
<code class="ui-path">/inbox/Handy/2026/IMG_3140.HEIC</code>        <!-- bricht überall um -->
<span class="ui-mono ui-clip" style="--ui-clip:22rem" title="vollständiger Wert">9f2c4e…</span>
<pre class="ui-pre">{ "json": true }</pre>    <!-- .ui-pre-wrap: umbrechen statt scrollen -->
```

### Kopieren (Dateiname, Pfad)
**Jeder angezeigte Dateiname und jeder Pfad / Ort** (auch `immich:<id>` und
Asset-IDs als Ort) bekommt direkt dahinter einen Kopieren-Knopf — nicht
aber ein Debug-Key allein. Macro in `templates/_copy.html`:
```html
{% from "_copy.html" import copy_btn with context %}
<!-- eine Zeile mit Ellipse: Wert + Knopf in .ui-copyable, der Wert behält seine Ellipse -->
<span class="ui-copyable"><a class="logs-name" href="…" title="IMG_3140.HEIC">IMG_3140.HEIC</a>{{ copy_btn(job.filename, 'name') }}</span>
<!-- umbrechender Pfad: Knopf folgt im Text; in einem Flex-Container beides in .ui-copyable.is-wrap -->
<code class="ui-path">/library/2026/IMG_3140.HEIC</code>{{ copy_btn(job.target_path) }}
```
`kind`: `'name'` („Dateiname kopieren") oder `'path'` („Pfad kopieren");
ohne Wert kein Knopf, `keep=True` rendert ihn versteckt (Wert kommt später,
`setCopyValue` in `app.js`). Gerendert wird
`<button class="ui-copy" data-copy="…" aria-label="Pfad kopieren: …">{I:copy 14}</button>`:
gedämpft, Hover/Fokus in `--primary`, 24 px Ziel ohne höhere Zeile.
Ein Klick-Handler für alle in `app.js` (Capture-Phase, `preventDefault` +
`stopPropagation`: öffnet keine Zeile, folgt keinem Link), kopiert über
`navigator.clipboard`, ohne HTTPS über ein verstecktes Textfeld und
`execCommand("copy")`; danach 1.5 s Haken und „Kopiert" (Tooltip und
Live-Region). In Skripten: `MA.copyable(node, value, kind, wrap)`,
`MA.copyButton(value, kind)`, für HTML-Strings `MA.copyButtonHTML(value, kind)`.

### Schlüssel-Wert-Liste (Job-Details)
```html
<div class="ui-card">
  <dl class="ui-kv is-inset">
    <div class="ui-kv-row"><dt>Original</dt><dd><code class="ui-path">/inbox/…</code></dd></div>
    <div class="ui-kv-row"><dt>Erstellt</dt><dd>22.10.2026 20:51</dd></div>
  </dl>
</div>
```
Ohne Karte: `<dl class="ui-kv">`; `.is-compact`; Schlüsselbreite über
`style="--ui-kv-key:9rem"`. Auf dem Handy steht der Schlüssel über dem Wert.

Kachelreihe für Kurzinfos:
```html
<div class="ui-facts">
  <div class="ui-fact"><span class="ui-fact-label">Status</span><span class="ui-fact-value">…</span></div>
</div>
```

### Pipeline-Schritte
```html
<div class="ui-steps">
  <details class="ui-step">
    <summary>
      <span class="ui-status is-success" title="OK">{I:check-circle}</span>
      <span class="ui-step-code">IA-01</span><span class="ui-step-label">EXIF lesen</span>
      <span class="ui-step-meta">0.4 s</span>
      <svg class="ui-i ui-step-chev" width="16" height="16" aria-hidden="true" focusable="false"><use href="#ui-i-chevron-down"/></svg>
    </summary>
    <div class="ui-step-body"><pre class="ui-pre">…</pre></div>
  </details>
</div>
```
Nicht aufklappbare Zeile: `<div class="ui-step"><div class="ui-step-head">…</div></div>`.

### Vorschaubilder
```html
<div class="ui-thumbs">                               <!-- style="--ui-thumb-min:15rem" für grössere Kacheln -->
  <article class="ui-thumb is-success">               <!-- Ton färbt den Rahmen; .is-selected = Auswahl -->
    <button type="button" class="ui-thumb-media lightbox-trigger" aria-label="Vorschau vergrössern: IMG_3140.HEIC">
      <img src="…" alt="" loading="lazy"></button>
    <div class="ui-thumb-overlay"><span class="ui-badge is-success">{I:star 12}Beste</span></div>
    <div class="ui-thumb-corner"><button type="button" class="ui-icon-btn ui-icon-btn-sm" aria-label="Mehr Aktionen">{I:more-vertical}</button></div>
    <div class="ui-thumb-body">
      <p class="ui-thumb-title" title="IMG_3140.HEIC">IMG_3140.HEIC</p>
      <div class="ui-thumb-meta"><span>4032 × 3024</span><span>3.4 MB</span></div>
      <div class="ui-tags">…</div>
    </div>
    <div class="ui-thumb-actions"><button type="button" class="ui-btn ui-btn-primary ui-btn-sm">Behalten</button></div>
  </article>
</div>
```
Ohne Bild: `<div class="ui-thumb-media"><span class="ui-thumb-placeholder">{I:video 28}Keine Vorschau</span></div>`;
`.ui-thumb-media.is-contain` zeigt das ganze Bild. Auf dem Handy zwei Kacheln nebeneinander.

Zeile mit Bild (Review):
```html
<article class="ui-media">
  <div class="ui-media-thumb"><img src="…" alt=""></div>
  <div class="ui-media-body">…Titel, Meta, Tags…</div>
  <div class="ui-media-actions">…Buttons untereinander…</div>
</article>
```

### Menü
```html
<div class="ui-menu-wrap">
  <button type="button" class="ui-icon-btn" aria-haspopup="menu" aria-expanded="false" aria-label="Mehr">{I:more-horizontal 18}</button>
  <div class="ui-menu" role="menu" hidden>              <!-- .is-left: links ausrichten -->
    <button type="button" class="ui-menu-item" role="menuitem">{I:retry}Erneut verarbeiten</button>
    <div class="ui-menu-sep" role="separator"></div>
    <button type="button" class="ui-menu-item is-danger" role="menuitem">{I:trash}Löschen</button>
  </div>
</div>
```

### Dialog
```html
<dialog class="ui-dialog" id="dlg-x" aria-labelledby="dlg-x-title">   <!-- .is-wide -->
  <div class="ui-dialog-shell">
    <div class="ui-dialog-head">
      <div class="ui-dialog-head-text"><h2 class="ui-dialog-title" id="dlg-x-title">Gruppe auflösen?</h2>
        <p class="ui-dialog-sub">3 Dateien · 1 bleibt</p></div>
      <button type="button" class="ui-icon-btn" aria-label="Schliessen" data-close>{I:x 18}</button>
    </div>
    <div class="ui-dialog-body">…</div>
    <div class="ui-dialog-foot">
      <button type="button" class="ui-btn ui-btn-secondary" data-close>Abbrechen</button>
      <button type="button" class="ui-btn ui-btn-danger-solid">Auflösen</button>
    </div>
  </div>
</dialog>
```
Öffnen mit `dialog.showModal()`. Die Einstellungs-Popups (`dialog.modal`,
`editor.css`) bleiben wie sie sind.

### Hilfsklassen
`.ui-muted`, `.ui-strong`, `.ui-text-xs`, `.ui-text-sm`, `.ui-mono`,
`.ui-tabular`, `.ui-nowrap`, `.ui-truncate`, `.ui-wrap-anywhere`,
`.ui-sr-only`, `.ui-link`, `.ui-divider`, `.ui-skeleton` (Platzhalter beim Laden).

---

## Basis-Schicht (`style.css`) — was ohne Markup-Änderung gilt

- Schrift-Stack (System-Schriften, kein reines `system-ui`), 15 px Fliesstext,
  `h1`/`h2`/`h3`-Skala, Links in `--primary`, sichtbarer Fokus, `code` als Chip.
- Alle Text-Eingaben, `select`, `textarea`: 2.5rem hoch, Radius 8, Fokus-Ring;
  `select` mit eigenem Pfeil; `input[type=time|number]` mit Tabellenziffern.
- `.btn` + `-primary/-secondary/-danger/-small/-ghost/-success/-info/-warning/-delete`
  im Look der `ui-btn`.
- `.switch` und `.switch-inline` wie die Schalter im Set-Editor (blau = an);
  ein `<small>` direkt nach `.switch-inline` rückt unter den Labeltext ein.
- `.settings-section`: Beschreibung bricht vor dem Schalter um (max. 100ch,
  Schalter `flex: none`, 1.5rem Abstand).
- `.alert`, `.alert-error/-success/-warning/-info` mit Icon (CSS-Maske).
- `.log-table-wrap` ist eine Karte; `.log-table`, `.data-table`,
  `.detail-table` im Listen-Look; `.status-badge`, `.level-badge`,
  `.match-badge` als Pillen.
- `.tabs`/`.tab` (Links mit `.tab-active` oder Buttons mit `aria-selected`).
- Navigationsleiste: Links als Pillen; `aria-current="page"` hebt den
  aktiven Link hervor (muss in `base.html` gesetzt werden).
