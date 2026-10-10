'use strict';
// Helpers for requirement-only ("blind") scenario tests: the only navigation is openHome
// (page.goto('/')); everything else is a click or a fill by accessible name. Names are
// case-insensitive patterns with flexible whitespace.
const { expect } = require('@playwright/test');

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function toPattern(value) {
  if (value instanceof RegExp) return value;
  return new RegExp(escapeRegExp(String(value)).replace(/\s+/g, '\\s+'), 'i');
}

function exact(value) {
  return new RegExp(`^\\s*${escapeRegExp(String(value)).replace(/\s+/g, '\\s+')}\\s*$`, 'i');
}

// Each helper waits for the first visible element among the roles that may carry the name, so a
// control that renders a moment late (after a client-side route change) is still found by its
// role; this keeps these tests about names, roles and reachability.
function anyOf(locators) {
  return locators.reduce((all, locator) => all.or(locator)).first();
}

async function openHome(page) {
  await page.goto('/');
}

async function clickNamed(page, value) {
  const name = toPattern(value);
  await anyOf([
    page.getByRole('button', { name }),
    page.getByRole('link', { name }),
    page.getByRole('tab', { name }),
    page.getByRole('menuitem', { name }),
  ]).click();
}

async function fillField(scope, labelOrPlaceholder, value) {
  const name = toPattern(labelOrPlaceholder);
  await anyOf([
    scope.getByLabel(name),
    scope.getByPlaceholder(name),
    scope.getByRole('textbox', { name }),
    scope.getByRole('searchbox', { name }),
  ]).fill(value);
}

async function expectTextsVisible(page, values) {
  for (const value of values) {
    const name = toPattern(value);
    await expect(anyOf([
      page.getByRole('heading', { name }),
      page.getByRole('button', { name }),
      page.getByRole('link', { name }),
      page.getByRole('tab', { name }),
      page.getByText(name),
      page.getByLabel(name),
      page.getByPlaceholder(name),
    ])).toBeVisible();
  }
}

async function expectAbsent(page, value) {
  const name = toPattern(value);
  await expect(page.getByRole('heading', { name })
    .or(page.getByRole('button', { name }))
    .or(page.getByRole('link', { name }))
    .or(page.getByText(name))).toHaveCount(0);
}

function unique(prefix) {
  return `${prefix}-${Date.now().toString(36)}${Math.floor(Math.random() * 1000)}`;
}

module.exports = {
  toPattern, exact, openHome, clickNamed, fillField, expectTextsVisible, expectAbsent,
  unique,
};
