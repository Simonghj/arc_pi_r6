const { createApp } = require('./app');

const port = Number(process.env.PORT || 3000);

async function start() {
  // Open shared resources (for example the JSON store) here, then register
  // every API route inside the callback so it precedes the fallbacks.
  const app = createApp((api) => {
    // api.post('/api/example', handler);
  });

  app.listen(port, '0.0.0.0', () => {
    console.log(`Backend listening at http://127.0.0.1:${port}`);
  });
}

start().catch((error) => {
  console.error('Failed to start backend:', error);
  process.exit(1);
});
