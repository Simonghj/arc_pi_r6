import tempfile
import unittest
from pathlib import Path

from arc_pi.verify import _dedupe_test_titles


class DuplicateTitleTests(unittest.TestCase):
    def test_repeated_scenario_titles_are_numbered_so_playwright_collects_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "REQ-1-2-2.spec.js"
            spec.write_text("test('REQ-1-2-2 -update the profile', async () => {});\n"
                            "test('REQ-1-2-2 -update the profile', async () => {});\n"
                            "test('REQ-1-2-2 -other', async () => {});\n", encoding="utf-8")
            _dedupe_test_titles(spec)
            text = spec.read_text(encoding="utf-8")
        self.assertIn("'REQ-1-2-2 -update the profile'", text)
        self.assertIn("'REQ-1-2-2 -update the profile (2)'", text)
        self.assertIn("'REQ-1-2-2 -other'", text)


class LocalUrlTests(unittest.TestCase):
    def test_pinned_grading_port_is_pointed_at_the_check_server(self):
        from arc_pi.verify import _point_at
        with tempfile.TemporaryDirectory() as directory:
            helpers = Path(directory) / "helpers.js"
            helpers.write_text("const BASE = process.env.BASE_URL || 'http://127.0.0.1:3000';\n"
                               "const other = 'http://localhost:3000/x';\n", encoding="utf-8")
            _point_at(helpers, "http://127.0.0.1:45123")
            text = helpers.read_text(encoding="utf-8")
        self.assertIn("'http://127.0.0.1:45123'", text)
        self.assertIn("'http://127.0.0.1:45123/x'", text)


class StopReasonTests(unittest.TestCase):
    def test_a_tool_call_cut_by_the_output_limit_is_continued_not_dropped(self):
        from arc_pi.runner import _stop_reason
        cut = {"stopReason": "error", "errorCode": "provider",
               "errorMessage": "provider returned invalid tool_call arguments JSON for call_01"}
        self.assertEqual(_stop_reason(cut), "length")
        truncated = {"stopReason": "error", "errorMessage": "model provider: provider returned "
                     "length-truncated tool calls; refusing to execute partial effects"}
        self.assertEqual(_stop_reason(truncated), "length")
        unknown = {"stopReason": "error", "errorMessage": "model provider: provider returned tool_call for unknown tool batch"}
        self.assertEqual(_stop_reason(unknown), "bad_tool")
        self.assertEqual(_stop_reason({"stopReason": "error", "errorMessage": "HTTP 500"}), "error")
        self.assertEqual(_stop_reason({}), "stop")


class SpecFreezeTests(unittest.TestCase):
    def test_a_repair_cannot_rewrite_or_add_specs(self):
        from arc_pi.runner import _freeze_specs, _restore_specs
        with tempfile.TemporaryDirectory() as directory:
            tests = Path(directory)
            (tests / "REQ-1.spec.js").write_text("expect(a).toBe(1)", encoding="utf-8")
            frozen = _freeze_specs(tests)
            (tests / "REQ-1.spec.js").write_text("expect(a).toBeTruthy()", encoding="utf-8")
            (tests / "REQ-2.spec.js").write_text("test.skip()", encoding="utf-8")
            _restore_specs(tests, frozen)
            self.assertEqual((tests / "REQ-1.spec.js").read_text(encoding="utf-8"), "expect(a).toBe(1)")
            self.assertFalse((tests / "REQ-2.spec.js").exists())

    def test_the_spec_prompt_names_the_missing_requirements(self):
        from arc_pi.runner import FINAL_SPECS_PROMPT
        text = FINAL_SPECS_PROMPT.format(ids="REQ-7, REQ-9")
        self.assertIn("REQ-7, REQ-9", text)

    def test_specs_of_changed_requirements_are_set_aside_and_current_ones_kept(self):
        from arc_pi.runner import _set_aside_stale_specs
        node = lambda node_id, *names: {"id": node_id, "scenarios": [{"name": name} for name in names]}
        with tempfile.TemporaryDirectory() as directory:
            tests, aside = Path(directory) / "tests", Path(directory) / "aside"
            tests.mkdir()
            (tests / "REQ-1.spec.js").write_text("test('REQ-1: a', f);\ntest(\"REQ-1: b\", f);", encoding="utf-8")
            (tests / "REQ-2.spec.js").write_text("test('REQ-2: old wording', f);", encoding="utf-8")
            stale = _set_aside_stale_specs(
                tests, [node("REQ-1", "REQ-1: a", "REQ-1: b"), node("REQ-2", "REQ-2: new wording"),
                        node("REQ-3", "REQ-3: new requirement")], aside)
            self.assertEqual(stale, ["REQ-2"])
            self.assertTrue((tests / "REQ-1.spec.js").is_file())
            self.assertFalse((tests / "REQ-2.spec.js").exists())
            self.assertTrue((aside / "REQ-2.spec.js").is_file())


class StagedRequirementTests(unittest.TestCase):
    def test_dependencies_on_an_earlier_stage_do_not_stop_the_run(self):
        from arc_pi.requirements import external_dependencies, topo_order, validate_requirement_tree
        atom = lambda node_id, deps: {"id": node_id, "name": node_id, "type": "ATOMIC", "dependencies": deps,
                                      "description": "d", "scenarios": [{"name": f"{node_id}: s", "steps": [
                                          {"keyword": "GIVEN", "content": "g"}, {"keyword": "WHEN", "content": "w"},
                                          {"keyword": "THEN", "content": "t"}]}]}
        tree = {"id": "ROOT", "name": "Stage 2", "type": "FOLDER", "description": "x",
                "children": [atom("REQ-3-1", ["REQ-1-1-2"]), atom("REQ-3-2", ["REQ-3-1", "REQ-2-3"])]}
        validate_requirement_tree(tree)
        self.assertEqual([n["id"] for n in topo_order(tree)], ["REQ-3-1", "REQ-3-2"])
        self.assertEqual(external_dependencies(tree), ["REQ-1-1-2", "REQ-2-3"])


if __name__ == "__main__":
    unittest.main()
