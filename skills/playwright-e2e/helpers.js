'use strict';
// Helpers for reviewer scenario tests: const h = require('./helpers');
const fs = require('node:fs');
const { test, expect } = require('@playwright/test');

// Unique display text so repeated runs never collide with earlier data.
function unique(prefix) {
  return `${prefix} ${Date.now()}${Math.floor(Math.random() * 1000)}`;
}

// Every scenario starts at the home page; this is the only navigation by URL.
async function openHome(page) {
  await page.goto('/');
}

// Click something that starts a download and return the file text.
async function downloadText(page, trigger) {
  const [download] = await Promise.all([page.waitForEvent('download'), trigger()]);
  return fs.readFileSync(await download.path(), 'utf8');
}

// Put an in-memory file into an <input type="file"> locator.
async function uploadText(input, name, text, mimeType = 'text/plain') {
  await input.setInputFiles({ name, mimeType, buffer: Buffer.from(text, 'utf8') });
}

// Separate browser session (another user) that still resolves relative URLs.
async function isolatedPage(browser) {
  const context = await browser.newContext({ baseURL: test.info().project.use.baseURL });
  return context.newPage();
}

// Answer the next alert/confirm/prompt dialog.
function onNextDialog(page, accept = true, promptText) {
  page.once('dialog', (dialog) => (accept ? dialog.accept(promptText) : dialog.dismiss()));
}

// Reload and assert that the locator is still there (persistence check).
async function expectAfterReload(page, locator) {
  await page.reload();
  await expect(locator).toBeVisible();
}

module.exports = { unique, openHome, downloadText, uploadText, isolatedPage, onNextDialog, expectAfterReload };
