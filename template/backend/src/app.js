const express = require('express');
const fs = require('fs');
const path = require('path');

const frontendDistPath = path.resolve(__dirname, '../../frontend/dist');

/**
 * Build the Express app. `registerRoutes(app)` adds every task API route
 * before the SPA fallback and the JSON 404 for unknown `/api` paths; Express
 * matches in registration order, so routes added after this returns are
 * never reached.
 */
function createApp(registerRoutes = () => {}) {
  const app = express();

  app.use(express.json());

  app.get('/api/health', (req, res) => {
    res.json({ code: 200, message: 'Backend Ready' });
  });

  registerRoutes(app);

  if (fs.existsSync(frontendDistPath)) {
    app.use(express.static(frontendDistPath));

    // Keep API routes on the backend and serve the SPA for all other GET requests.
    app.get(/^(?!\/api(?:\/|$)).*/, (req, res) => {
      res.sendFile(path.join(frontendDistPath, 'index.html'));
    });
  } else {
    app.get('/', (req, res) => {
      res
        .status(503)
        .type('html')
        .send(`<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Frontend Build Missing</title>
  </head>
  <body>
    <main>
      <h1>Frontend build missing</h1>
      <p>The backend is running, but <code>frontend/dist</code> is not available yet.</p>
      <p>Build the frontend first, then start the backend so it can host the compiled site on the same port.</p>
    </main>
  </body>
</html>`);
    });
  }

  // Unknown API paths answer JSON 404, never the SPA shell.
  app.use('/api', (req, res) => {
    res.status(404).json({ error: 'not found' });
  });

  return app;
}

module.exports = { createApp };
