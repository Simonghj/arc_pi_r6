import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from arc_pi import runner, verify
from helpers import make_config


PROJECT = Path(__file__).resolve().parents[1]

LEAF = """      - id: {id}
        name: {name}
        type: ATOMIC
        dependencies: []
        description: Show the {name}.
        scenarios:
          - name: visible
            steps:
              - keyword: THEN
                content: The {name} is visible.
"""
FOLDER = """  - id: {id}
    name: {name}
    type: FOLDER
    dependencies: []
    description: {name} area.
    children:
"""
YAML = ("id: ROOT\nname: Demo\ntype: FOLDER\ndependencies: []\ndescription: Demo.\nchildren:\n"
        + FOLDER.format(id="REQ-1", name="Projects") + LEAF.format(id="REQ-1-1", name="project page")
        + FOLDER.format(id="REQ-2", name="Repos") + LEAF.format(id="REQ-2-1", name="repo page"))


class BuildFixFlowTests(unittest.TestCase):
    def run_two_batches(self, first_reply=None, crash_on_start=False):
        with tempfile.TemporaryDirectory(prefix='arc-buildfix-') as directory:
            root = Path(directory)
            requirements = root / 'input' / 'requirements.yaml'
            requirements.parent.mkdir()
            requirements.write_text(YAML, encoding='utf-8')
            output = root / 'app'
            app = output / 'frontend' / 'src' / 'App.jsx'
            config = make_config(
                project_dir=PROJECT, openai_api_key='offline-test',
                openai_base_url='http://127.0.0.1:1/v1', model='offline', pi_reasoning='off',
                pi_time_budget_seconds=3600, pi_max_model_calls=50,
                run_token_budget=10 ** 12, pi_max_output_tokens=1000,
                pi_context_bytes=8388608, pi_repair_rounds=0,
                acceptance_tests_dir=None,
                pi_smoke_port=None, pi_bash_path=None, pi_openspec_skill=False,
            )
            bridge = MagicMock()
            bridge.needs_compaction.return_value = False
            bridge.totals = {}
            bridge.context_overhead_bytes = None
            prompts = []

            crash = output / 'backend' / 'src' / 'crash.js'

            def prompt(text, deadline):
                prompts.append(text)
                if crash_on_start:
                    if len(prompts) == 1:
                        app.write_text('export default function App() { return null }\n', encoding='utf-8')
                        crash.write_text('module.exports = item.id;\n', encoding='utf-8')
                    elif crash.exists():
                        crash.unlink()
                    return {'stopReason': 'stop'}
                if len(prompts) == 1:
                    if first_reply is not None:
                        return first_reply
                    app.write_text('import Project from "./pages/ProjectPage.jsx";\n'
                                   'import Board from "./pages/BoardPage";\n'
                                   'export default Project;\n', encoding='utf-8')
                    return {'stopReason': 'turn_limit'}
                return {'stopReason': 'stop'}

            def fake_verify(**kwargs):
                if kwargs.get('build_only'):
                    error = verify._check_frontend_imports(Path(kwargs['output_dir']) / 'frontend')
                    if error:
                        return {'build': 'failed', 'error': error}
                    if kwargs.get('start_backend') and crash.exists():
                        return {'build': 'passed', 'startup': 'failed',
                                'error': 'backend did not become HTTP-ready: ReferenceError: item is not defined'}
                    return {'build': 'passed'}
                return {'build': 'passed', 'startup': 'passed', 'acceptance': 'unavailable',
                        'tests': [], 'passed': 0, 'failed': 0, 'total': 0}

            bridge.prompt.side_effect = prompt
            with patch.object(runner, 'PiBridge', return_value=bridge), \
                    patch.object(runner, 'AgentRuntime'), \
                    patch.object(runner, 'verify_application', side_effect=fake_verify):
                runner.run_task(config, requirements, output, 3000)
            return prompts

    def test_interrupted_reply_gets_every_unresolved_import(self):
        follow_up = self.run_two_batches()[1]
        self.assertIn('cut off (turn_limit)', follow_up)
        self.assertIn('unresolved import "./pages/ProjectPage.jsx" from "src/App.jsx"', follow_up)
        self.assertIn('unresolved import "./pages/BoardPage" from "src/App.jsx"', follow_up)

    def test_unfixed_build_error_is_carried_into_next_batch(self):
        batch_two = next(text for text in self.run_two_batches() if 'REQ-2-1' in text)
        self.assertIn('## The application build currently FAILS', batch_two)
        self.assertIn('## Section contracts for this batch', batch_two)
        self.assertIn('./pages/ProjectPage.jsx', batch_two)

    def test_prompt_deadline_budget_stop_does_not_skip_later_batches(self):
        prompts = self.run_two_batches({'stopReason': 'budget', 'errorCode': None})
        self.assertTrue(any('REQ-2-1' in text for text in prompts))

    def test_backend_that_crashes_on_start_gets_a_fix_follow_up(self):
        prompts = self.run_two_batches(crash_on_start=True)
        self.assertIn('backend failed to start', prompts[1])
        self.assertIn('ReferenceError: item is not defined', prompts[1])
        self.assertTrue(any('REQ-2-1' in text and 'currently FAILS' not in text for text in prompts[2:]))


if __name__ == '__main__':
    unittest.main()
