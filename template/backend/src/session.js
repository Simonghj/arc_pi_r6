const crypto = require('crypto');

const SESSION_COOKIE = 'pi_session';

/**
 * Session and password helpers over an openStore-backed store.
 *
 * Session records live under the reserved `sessions` key, keyed by the
 * SHA-256 digest of the raw token; only the raw 32-byte random base64url
 * token is ever returned to the auth route. Expired/revoked/malformed tokens
 * load as null. Records survive backend restarts.
 */

function digestToken(rawToken) {
  return crypto.createHash('sha256').update(String(rawToken)).digest('hex');
}

function createSessionService(store) {
  async function create(userId, { expiresAt = null } = {}) {
    const rawToken = crypto.randomBytes(32).toString('base64url');
    const digest = digestToken(rawToken);
    await store.mutate((state) => {
      const sessions = state.sessions ?? (state.sessions = {});
      sessions[digest] = {
        userId,
        createdAt: new Date().toISOString(),
        expiresAt: expiresAt ? new Date(expiresAt).toISOString() : null,
      };
    });
    return rawToken;
  }

  async function load(rawToken) {
    if (typeof rawToken !== 'string' || !rawToken) return null;
    const record = store.read((state) => (state.sessions ?? {})[digestToken(rawToken)]);
    if (!record || typeof record !== 'object') return null;
    if (record.expiresAt && new Date(record.expiresAt).getTime() <= Date.now()) return null;
    return { userId: record.userId, createdAt: record.createdAt, expiresAt: record.expiresAt };
  }

  async function revoke(rawToken) {
    if (typeof rawToken !== 'string' || !rawToken) return;
    const digest = digestToken(rawToken);
    await store.mutate((state) => {
      const sessions = state.sessions ?? (state.sessions = {});
      delete sessions[digest];
    });
  }

  return { create, load, revoke };
}

function cookieOptions(secure, maxAge) {
  const options = { httpOnly: true, sameSite: 'lax', path: '/' };
  if (secure) options.secure = true;
  if (maxAge !== undefined) options.maxAge = maxAge;
  return options;
}

/** Set the session cookie: name `pi_session`, path `/`, HttpOnly, SameSite=Lax. */
function setSessionCookie(res, token, { secure = false, maxAge } = {}) {
  res.cookie(SESSION_COOKIE, token, cookieOptions(secure, maxAge));
}

function clearSessionCookie(res, { secure = false } = {}) {
  res.clearCookie(SESSION_COOKIE, cookieOptions(secure));
}

/**
 * Asynchronous scrypt hashing with Node default work factors and a 16-byte
 * random salt, encoded as `scrypt:<salt b64url>:<derived b64url>`.
 */
function hashPassword(password) {
  const salt = crypto.randomBytes(16);
  return new Promise((resolve, reject) => {
    crypto.scrypt(String(password), salt, 64, (error, derived) => {
      if (error) return reject(error);
      resolve(`scrypt:${salt.toString('base64url')}:${derived.toString('base64url')}`);
    });
  });
}

/** Timing-safe verification; malformed encodings verify as false. */
function verifyPassword(password, encoded) {
  const parts = String(encoded).split(':');
  if (parts.length !== 3 || parts[0] !== 'scrypt') return Promise.resolve(false);
  let salt;
  let expected;
  try {
    salt = Buffer.from(parts[1], 'base64url');
    expected = Buffer.from(parts[2], 'base64url');
  } catch {
    return Promise.resolve(false);
  }
  if (salt.length < 16 || expected.length < 16) return Promise.resolve(false);
  return new Promise((resolve, reject) => {
    crypto.scrypt(String(password), salt, expected.length, (error, derived) => {
      if (error) return reject(error);
      resolve(crypto.timingSafeEqual(derived, expected));
    });
  });
}

module.exports = {
  createSessionService,
  setSessionCookie,
  clearSessionCookie,
  hashPassword,
  verifyPassword,
  SESSION_COOKIE,
};
