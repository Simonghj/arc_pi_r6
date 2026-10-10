const fs = require('fs');
const path = require('path');

/**
 * Single-process JSON store.
 *
 * - `seed` supplies the initial JSON ONLY when the file does not exist.
 * - Corrupt existing data is a startup error, never a reason to reseed.
 * - Mutations are serialized and acknowledged only after persistence
 *   (temp file + atomic rename); a thrown mutator or failed write leaves the
 *   prior committed state intact and must not poison the queue.
 * - `read` exposes the committed state to a synchronous read-only selector.
 * - `mutate` applies a synchronous mutator to one transaction draft, persists
 *   it, then publishes it as the committed state and returns the mutator
 *   result.
 *
 * All mutation callers must share one store instance; there is no per-request
 * reload/reset.
 *
 * @param {{ filePath: string, seed?: object }} options
 * @returns {Promise<{ read: (selector: (state: any) => any) => any, mutate: (mutator: (draft: any) => any) => Promise<any> }>}
 */
async function openStore({ filePath, seed }) {
  const resolvedPath = path.resolve(filePath);
  const dir = path.dirname(resolvedPath);
  let committed = null;
  let queue = Promise.resolve();

  async function ensureInitialized() {
    if (committed !== null) return;
    if (!fs.existsSync(resolvedPath)) {
      if (seed === undefined) {
        throw new Error(`store does not exist and no seed was provided: ${resolvedPath}`);
      }
      fs.mkdirSync(dir, { recursive: true });
      const tmp = `${resolvedPath}.tmp-${process.pid}-${Date.now()}`;
      fs.writeFileSync(tmp, JSON.stringify(seed, null, 2), 'utf8');
      fs.renameSync(tmp, resolvedPath);
    }
    const raw = fs.readFileSync(resolvedPath, 'utf8');
    try {
      committed = JSON.parse(raw);
    } catch (error) {
      throw new Error(
        `corrupt store data at ${resolvedPath}; refusing to reseed: ${error.message}`,
      );
    }
  }
  await ensureInitialized();

  return {
    read(selector) {
      if (committed === null) {
        throw new Error(`store not initialized: ${resolvedPath}`);
      }
      return selector(committed);
    },
    async mutate(mutator) {
      const run = queue.then(async () => {
        await ensureInitialized();
        const draft = structuredClone(committed);
        const result = mutator(draft);
        fs.mkdirSync(dir, { recursive: true });
        const tmp = `${resolvedPath}.tmp-${process.pid}-${Date.now()}`;
        fs.writeFileSync(tmp, JSON.stringify(draft, null, 2), 'utf8');
        fs.renameSync(tmp, resolvedPath);
        committed = draft;
        return result;
      });
      queue = run.catch(() => {});
      return run;
    },
  };
}

module.exports = { openStore };
