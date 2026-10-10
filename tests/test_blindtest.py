import tempfile
import unittest
from pathlib import Path

from arc_pi.blindtest import spec_issues

NAMES = ["REQ-2-1-1: List Saved Projects - Scenario 1",
         "REQ-2-1-1: List Saved Projects - Scenario 2"]


def spec(directory: str, body: str) -> Path:
    path = Path(directory) / "REQ-2-1-1.spec.js"
    path.write_text("const { test, expect } = require('@playwright/test');\n"
                    "const h = require('./helpers');\n" + body, encoding="utf-8")
    return path


class SpecContractTests(unittest.TestCase):
    def test_a_contract_spec_with_scenario_titles_has_no_issues(self):
        with tempfile.TemporaryDirectory() as directory:
            path = spec(directory, "".join(
                f"test('{name}', async ({{ page }}) => {{\n  await h.openHome(page);\n"
                f"  await h.clickNamed(page, 'Projects');\n}});\n" for name in NAMES))
            self.assertEqual(spec_issues(path, NAMES), [])

    def test_ui_bypassing_shortcuts_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = spec(directory, f"test('{NAMES[0]}', async ({{ page }}) => {{\n"
                                   "  await page.goto('/demo-group');\n"
                                   "  await fetch('/api/repos');\n"
                                   "  await expect(page).toHaveURL(/demo/);\n"
                                   "  await page.locator('.repo-row').click();\n});\n")
            issues = spec_issues(path, NAMES)
        self.assertEqual(len(issues), 4)
        self.assertTrue(all("uses " in issue for issue in issues))

    def test_a_title_that_is_not_a_scenario_name_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = spec(directory, "test('REQ-2-1-1: user filters projects', async ({ page }) => {\n"
                                   "  await h.openHome(page);\n});\n")
            issues = spec_issues(path, NAMES)
        self.assertEqual(len(issues), 1)
        self.assertIn("titles not equal to a scenario name", issues[0])


if __name__ == "__main__":
    unittest.main()
