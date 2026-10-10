---
name: review-and-debug
description: Reviewer agent, loaded for every review prompt and for final repair after a verification run produced observed failures. Evidence-driven debugging and repair of the generated app.
---

# review-and-debug

Debug and repair from evidence. Review the work, not the author.

## Read evidence, find root cause, then edit

- Read the failure evidence completely: test titles, error messages,
  stack traces, expected vs received values, line numbers. They usually
  name the exact mismatch.
- Reproduce and locate the root cause before editing. State one
  hypothesis ("X fails because Y") and verify it against the code,
  ideally with a local repro, before changing anything.
- One hypothesis at a time. Make the smallest change that tests it. If
  it fails, form a new hypothesis; never stack fixes.
- Several failed fixes in a row means the design is wrong, not the last
  line: re-read the requirement text and reconsider the structure.

## Fix the app, not the symptom

- Fix the root cause; never special-case the failing assertion. Test
  failures are evidence; requirements are authoritative.
- Keep edits small and targeted: fix only the observed failure. Do not
  redesign, reformat or "improve" passing features; do not delete, reset
  or re-scaffold working code; persisted user data must survive.
- Never weaken, delete or skip a test to make it pass. Change a test
  only when it contradicts the requirement text; the requirement wins,
  and the app must then match the requirement.
- For each listed failure, re-read the referenced files before editing,
  then make the minimal edit. When a failure names a missing element
  (heading, labelled control, route link), add exactly that element:
  the heading text, the native `<label htmlFor>`/`id` pair, the link.

## Verify before claiming

- Verify with the harness check output. Never claim a test passed, an
  endpoint works or a failure is fixed without fresh verification
  output showing it. "Should pass" is not evidence.
- Report actual state: which failures are fixed, which remain, changed
  files. A requirement is verified only with acceptance evidence; a
  turn with no source changes repairs nothing.

## Check these first (the usual suspects)

- Frontend fetch <-> backend parity: route path, HTTP method, request
  body keys, response JSON shape and status codes must match exactly
  between the React fetch call and the Express route.
- Persistence: every mutation written to the store and re-read after
  reload; seed data created on first start and preserved.
- Names and roles: exact requirement strings, link vs button, unique
  labels, ARIA roles for tabs/grids/menus/dialogs.

Source: distilled from systematic-debugging, verification-before-completion, receiving-code-review, evidence-repair, openspec-workflow (MIT).
