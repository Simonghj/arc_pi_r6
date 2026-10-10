---
name: playwright-e2e
description: Use when writing or fixing the agent's own Playwright scenario tests for the app. Selector, waiting and structure rules for reliable tests.
---

# playwright-e2e

The harness runs these specs in Chromium against the app's base URL
(use relative URLs), one worker, 10 s per test.

## Structure

- CommonJS: `const { test, expect } = require('@playwright/test');`
- Test titles start with the requirement id and a colon
  (`test('R-12: ...', ...)`).
- One test per GIVEN/WHEN/THEN scenario; assert every THEN in that
  scenario.
- Each test is independent: it creates what it needs and cleans up only
  its own data. Unique data via `Date.now()`; never delete or rename
  seed data; never depend on data another test created.
- Sign in only with seed accounts defined by the requirement.
- Each test must finish in under 10 s total.

## Acting like a user

- Start every test with `await h.openHome(page);` (it opens `/`), then
  navigate by clicking visible links and buttons.
- Never call `page.goto` in a spec: `h.openHome` is the only navigation
  by URL.

## Locators

- Prefer, in order: `getByRole` (with accessible name), `getByLabel`
  (form controls), `getByText` (visible content).
- Use the exact names quoted in the requirement:
  `page.getByRole('button', { name: 'Save changes' })`,
  `page.getByLabel('Password')`.
- Add `{ exact: true }` when a shorter name could match longer ones
  (`name: 'Save'` also matches "Save as").
- Strict-mode violations: narrow the locator, never `first()` blindly;
  use role + exact name, or scope inside the containing region/dialog,
  e.g. `dialog.getByRole('button', { name: 'Delete' })`.

## Waiting and asserting

- Web-first assertions, not sleeps: `expect(locator).toBeVisible()`,
  `toHaveText(...)`, `toHaveValue(...)`, `await page.waitForURL(...)`,
  `locator.waitFor(...)`. Playwright auto-waits on actions. No
  `waitForTimeout` or `setTimeout`; if timing is genuinely the behavior
  under test, document why.
- Assert observable behavior, not implementation: visible text, values,
  URLs, counts, enabled/disabled state. After an action, assert the
  visible outcome (a heading appears, a row has the new value), not
  just that nothing threw.
- Before writing a test, name the app change that should make it fail;
  if only a rewrite of the test itself could fail it, it verifies
  nothing.

## Reference patterns

`review-tests/helpers.js` is provided: `const h = require('./helpers');`

```js
// forms: fill by label, submit by button name
await page.getByLabel('Project name', { exact: true }).fill(h.unique('project'));
await page.getByRole('button', { name: 'Create project' }).click();
// keyboard: focus a control, type, confirm
await page.getByLabel('Note', { exact: true }).click();
await page.keyboard.type('hello'); await page.keyboard.press('Enter');
// persistence: reload and assert again
await h.expectAfterReload(page, page.getByRole('link', { name: 'Weekly plan', exact: true }));
// file upload / download
await h.uploadText(page.getByLabel('Attachment'), 'notes.txt', 'hello\n');
const text = await h.downloadText(page, () => page.getByRole('button', { name: 'Download' }).click());
expect(text).toContain('hello');
// confirm dialogs (register before the click)
h.onNextDialog(page, true);
// a second user in its own session
test('REQ-2-1: second user sees the project', async ({ page, browser }) => {
  const other = await h.isolatedPage(browser); await h.openHome(other);
});
// scope inside a row/region to avoid strict-mode duplicates
await page.getByRole('row', { name: /Weekly plan/ }).getByRole('button', { name: 'Rename' }).click();
```

Names above are examples only: always use the names the requirement quotes.

Source: distilled from playwright-skill (API reference), writing-good-tests (MIT).
