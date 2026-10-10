---
name: auth-session-consistency
description: Apply when any requirement ancestor or leaf in the current batch mentions login, registration, logout, session, account, or authenticated state (case-insensitive: auth, login, log in, sign in, register, registration, logout, log out, session, account, 登录, 注册, 退出).
---

# auth-session-consistency

Loaded only when the current batch's requirements mention authentication.
Authentication state is a cross-cutting system contract, not a page-local
success message.

- One owner for session state: header/navigation, protected/public route
  behavior and current-user display all consume the same global session
  state. Never duplicate page-local auth flags, fake user objects, local
  fallback arrays or hardcoded header text.
- Reuse existing boundaries first: shell/header, API client, auth routes,
  service and persistence owners. Prefer them over new owners.
- Login/registration success must update the global authenticated state
  and set a server-honored session (cookie/token) so a reload restores
  the authenticated UI.
- Provide a session-loading boundary (e.g. `GET /api/session`) the app
  awaits before rendering auth-dependent chrome; do not flash
  unauthenticated UI on refresh.
- Routes, redirects, labels and session lifetime come from the
  requirements; keep authentication state isolated between users.
- Failed authentication must not create a session or mutate protected
  data; the app stays anonymous with a visible error. Failed login is
  never a signed-in state.
- Logout must revoke the session server-side (not just clear the UI) so
  the user is anonymous after refresh and after a backend restart.
- Keep cookies/tokens/session records consistent across UI client, API
  responses, service logic and the store.
- Bad input, network failure or corrupt persistence leaves observable
  state consistent: anonymous stays anonymous; recovery uses only
  durable session data; a corrupt store is a startup error, never
  silently reseeded.
- Tests verify observable global transitions: unauthenticated chrome
  before the action, authenticated chrome/current-user state after,
  session recovery after refresh where the scenario requires it.
- Inspect auth-owned files in path order: login/register/account page,
  shared header/navigation, session provider/store, auth API client,
  auth route, auth service, session persistence/schema. Do not scan
  unrelated directories.

Source: distilled from auth-session-consistency, web-implementation (MIT).
