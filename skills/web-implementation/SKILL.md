---
name: web-implementation
description: Always loaded for every batch. Layout, serving, persistence, UI and data rules for the generated app.
---

# web-implementation

Standing contract for the generated app; loaded every batch.
Code it right the FIRST time.

## Plan the batch before writing

- Before editing, write a short plan in your reply: files to create or
  change (one responsibility each), API endpoints (method, path, JSON),
  store changes, pages and routes, and the exact names/values copied
  from the requirement text. Implement store/API before UI.
- Write every imported module in the same reply, before or with the
  importer; never import a file you have not written.
- One route registry: every page route lives in `frontend/src/App.jsx`;
  a page exists only when a `<Route>` there renders it.
- Every page is reachable from the home page via visible links/buttons
  and has a heading with the page title; replace the starter home page.
- A scenario that "opens" a page or record by name without a click path
  ("opens the Reports page", "opens that record") is clicked by that exact
  name on the start page. List those pages and the seeded records the
  scenarios open (titles as links) in the home page body; not in the
  global header, so each name stays unique on every page.
- "clicks `A` or `B`": render only one of the two buttons at a time.
- First render: a page shows its heading, section links/tabs and its
  route-only action links immediately, so the user can act at once
  after a click; never a whole-page "Loading" return while data loads.
  Loading text goes inside the waiting list only; cache fetched data in
  memory for revisits.
- Role words are exact: textbox = `<input type="text">`, searchbox =
  `<input type="search">`, combobox = `<select>` (or the ARIA listbox
  pattern when the requirement says options use the option role, have
  visible names or are clicked); a "textbox" must not
  be `type="search"` (that is the searchbox role).
- Before finishing, self-check against the requirement: walk every
  GIVEN/WHEN/THEN of the batch's scenarios and name the element or
  endpoint that satisfies each THEN; implement anything missing now.

## Runtime environment

- The harness builds the app from a fresh copy: `npm install`, then
  `npm run build` (frontend) and `npm run start` (backend, honors
  `PORT`); every dependency must be in `package.json`, and `/` must
  answer soon after start.
- Keep it light: no heavy dependencies, no native modules, no work at
  startup beyond opening the JSON store and seeding.
- The app serves every user from ONE server and store: pages must work
  with many extra records created by other users.
- Pages render immediately (no artificial delays, no polling loops);
  every action gives visible feedback at once.

## Validation on both layers

- Invalid input the requirement mentions (empty, duplicate, wrong
  format, not permitted) is rejected in the UI with the specified
  message AND by the API (4xx + error JSON); rejected actions change
  nothing in the store.

## Workspace layout (the runner depends on this EXACT layout)

- `frontend/`: `npm run build` produces `frontend/dist/`. Plain CSS,
  native `fetch`, minimal pure-JS deps.
- `backend/`: Node + Express; `npm run start` honors `PORT`.
  Persistence is a JSON file (e.g. `backend/data/db.json`) loaded at
  startup, written back on every mutation. Hash passwords with Node's
  built-in `crypto` (scrypt).
- The backend serves `frontend/dist/` and exposes JSON APIs under
  `/api/`. No second fixed port; only `PORT`.
- Register every API route inside the `createApp((api) => { ... })`
  callback in `backend/src/index.js`, after opening the store; routes
  added after `createApp` returns sit behind the SPA fallback and
  `/api` 404.
- This agent is text-only: use textual requirements.

## UI behavior and data rules

- Exact accessible names and roles: keep the role the requirement names
  (link vs button vs heading vs textbox) and its literal text. A named
  link/button/field appears exactly once per page.
- Native label association only: `<label htmlFor>` paired with the
  control's `id`, or the control wrapped in its `<label>`;
  placeholders, prose and DOM order are not association. Labels must be
  unique so `getByLabel` is unambiguous ("Code" vs
  "Repeat code", never two "Code"); group multi-part forms
  into sections with unique headings.
- Dynamic values (meters, counters, progress bars) live in their own
  element and update value/attributes/classes/text in the input handler
  on every relevant event, never only on blur/submit; identical markup
  each render is not dynamic.
- Grids, tabs, menus, dialogs use ARIA roles, not styled divs:
  `tablist`/`tab` with `aria-selected`, `grid`/`gridcell`,
  `menu`/`menuitem`, `dialog`, so users and assistive technology can
  address elements by role.
- Per-item controls: use the name the requirement gives (e.g. a
  "Delete <name>" pattern). Otherwise do NOT put the item's name
  into a button's name: a button whose name contains another control's
  whole name ("More options for Monthly report" next to the "Monthly
  report" link) makes that name ambiguous for the user and for
  assistive technology. Use a short name ("More options") inside the
  item's row.
- Permissions: build ONE table from the requirement text, operation ->
  roles allowed (`{ createItem: ['editor', 'owner'], ... }`), and check it
  on the server for every request. Roles are not a ladder unless the
  requirement says so. Controls a user may not use are absent from the
  page and the API answers 403.
- Derived values (totals, computed fields, counts, summaries, diffs): store the
  user's raw input and compute derived values from it on every read, so
  everything that depends on a changed value is current at once.
- A create flow ends on the new record, ready for the next step; an edit
  acts on exactly the element the user selected.
- Every page has its own route that survives a refresh or a direct visit.
- No placeholders or fake data: starter text, lorem ipsum, invented
  rows, fixed sample accounts are failures.

## ARIA patterns (W3C ARIA Authoring Practices)

How Playwright matches: `getByRole(role, { name })` uses the computed
accessible name (label text, `aria-label`, `aria-labelledby`, or the
element's text); without `exact` it is a case-insensitive substring, so
"Save" also matches "Save copy". `getByLabel` matches `<label>` text the
same way. Any locator matching two visible elements fails (strict mode).

- One shell, one of each control: the global header (`<header>` =
  banner) and nav hold different links. "Sign in", "New", "Settings",
  the search box: render each in ONE place per page, never both in
  the banner and a sidebar/nav or twice for mobile/desktop layouts.
  Lists render each record once (no invisible duplicate for another view).
- Page title: exactly one `<h1>` with the page/record name; section
  titles are `<h2>`.
- Buttons vs links: navigation to a route is `<a href>` (link); an
  action on the current page is `<button>` (button). Icon-only buttons
  need `aria-label` with the requirement's word.
- Forms: every field has a `<label htmlFor>`; errors are text in the
  page (`role="alert"` for form-level errors) next to the field, shown
  all together on submit; submit is a `<button type="submit">`.
- Dialog: `<div role="dialog" aria-modal="true" aria-labelledby=titleId>`
  with a heading as its title; confirm/cancel are buttons inside it;
  confirmation prompts may use `role="alertdialog"`. Prefer in-page
  dialogs over `window.confirm` unless the requirement says browser
  dialog.
- Tabs: `role="tablist"` > `role="tab"` (`aria-selected="true"` on the
  active one, name = tab text) + `role="tabpanel"` labelled by its tab.
- Menu button: `<button aria-haspopup="menu" aria-expanded>` opens
  `role="menu"` > `role="menuitem"` items named by their text.
- Data grid (interactive cells): `role="grid"` > `role="row"` >
  `role="columnheader"` / `role="rowheader"` / `role="gridcell"`; each
  cell's accessible name is the name the requirement uses to refer to
  it; the active cell or row has `aria-selected="true"`. Keyboard as in
  the ARIA grid pattern: arrow keys move the active cell, and typing or
  Enter acts on the active cell.
- Data table (lists of records): `<table>` with `<th scope="col">`; each
  row names its record in a link or cell so row-scoped locators work.
- Checkbox/switch: native `<input type="checkbox">` with a label;
  `role="switch"` only when the requirement says toggle/switch.
- Combobox/select: native `<select>` with a label unless the
  requirement describes a searchable list or clickable options with the
  option role (`role="combobox"` +
  `role="listbox"`/`role="option"`).
- A menu the requirement attaches to an item without naming a trigger
  opens on right-click of the item and from a small visible button
  beside it.
- Pop-up menus (account menus, per-row menus) close on every route
  change, on choosing an item and on outside click; a test that opens
  the menu again later must find it closed, so one click opens it.
- Status messages ("Saved", "Done") are visible text,
  `role="status"`, kept on screen until the next action.

## Persistence and seed data

- Records described as "existing"/"predefined"/"seeded" must be created
  by the backend on first start (new store only), then served from the
  store; later startups preserve user edits, deletions and archive
  state (a missing record does not mean a new store).
- Seeded roles: tests sign in only with credentials the requirement
  prints. If seed data supplies an owner/admin/member for a record and
  names no other account with a password, the given account holds that
  role. Never put a seeded role on an invented account.
- Every mutation persists across reload: write the JSON store on every
  create/update/delete; the UI re-renders persisted state, not only
  memory.
- Derive state ownership from requirements (per-view/per-session/shared);
  never reset persisted user data on startup (reset only when demanded);
  show a loading state while initializing.
- Treat examples as examples unless the requirement fixes initial
  records or enumerated values; handle other valid inputs generally;
  do not invent broad lists.

## Data and API honesty

- No fake backend data: serve real persisted state; never hardcoded
  rows, invented accounts or page-local stand-ins.
- No test-specific shortcuts: never branch on test ids, fixtures or
  locators, never code that exists only to satisfy a test.
  Requirements are authoritative: implement only the routes, text,
  redirects and state changes they specify.

## Code style and tool discipline

- Extend the existing app with small, plain functions; inspect source
  first, reuse its store/session helpers; never rewrite working
  features.
- Paths relative to workspace root, forward slashes
  (`frontend/src/pages/HomePage.jsx`); never absolute Windows paths.
- Never run npm/npx commands yourself: the harness installs, builds
  and runs its own scenario checks.
- Bash: short, read-only commands only. Never start servers or long
  builds; never retry a command more than twice; fix source, not the
  environment.
- Never start a server on the app's run port (default 3000) or probe
  it; smoke checks may only use the port named in the current prompt.
- Never hand-edit `package-lock.json`, `node_modules`, `.env`, `.git`,
  `.arc`.
- Never claim a test passed or endpoint verified: the harness runs its
  own scenario checks. End with a brief summary of changed files and
  unresolved requirements.

Source: distilled from web-implementation, writing-plans, defense-in-depth (MIT).
