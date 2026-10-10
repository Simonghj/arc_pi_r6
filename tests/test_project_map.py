import tempfile
import unittest
from pathlib import Path


class ProjectMapTests(unittest.TestCase):
    def test_map_lists_backend_and_frontend_with_exports_and_routes(self):
        from arc_pi.runner import _file_listing
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "backend/src/routes.js": "app.get('/api/items', h);\nrouter.post(\"/api/items/:id\", h);\n"
                                         "module.exports = { registerRoutes, handle };\n",
                "backend/data/db.json": "{}",
                "backend/node_modules/x/index.js": "export const hidden = 1;",
                "frontend/src/App.jsx": "<Route path=\"/items/:id\" element={<Item/>} />\n"
                                        "export default function App() {}\n",
                "frontend/package.json": "{}",
            }
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            lines = _file_listing(root).splitlines()
        self.assertEqual([line.split(" ")[0] for line in lines], ["backend/src/routes.js", "frontend/src/App.jsx"])
        self.assertIn("exports registerRoutes, handle", lines[0])
        self.assertIn("api GET /api/items, POST /api/items/:id", lines[0])
        self.assertIn("exports App", lines[1])
        self.assertIn("routes /items/:id", lines[1])


class RateLimitRetryTests(unittest.TestCase):
    def test_a_rate_limited_prompt_waits_and_retries_instead_of_failing_the_batch(self):
        from unittest.mock import MagicMock, patch
        from arc_pi import runner
        from helpers import make_config
        limited = {"stopReason": "error", "errorCode": "provider", "summary": "",
                   "errorMessage": "provider error HTTP 429"}
        bridge = MagicMock()
        bridge.totals = {}
        bridge.prompt.side_effect = [limited, limited, {"stopReason": "stop", "summary": "done"}]
        waits = []
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(runner.time, "sleep", side_effect=waits.append), \
                patch.object(runner, "_relabel_watchdog_abort", side_effect=lambda raw, *rest: raw):
            response = runner._prompt(bridge, make_config(pi_prompt_seconds=60), "work",
                                      runner.time.monotonic() + 3600, Path(directory))
        self.assertEqual(response["stopReason"], "stop")
        self.assertEqual(waits, [30, 60])
