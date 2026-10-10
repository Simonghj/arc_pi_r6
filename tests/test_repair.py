import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from arc_pi import runner, verify
from helpers import make_config


PROJECT = Path(__file__).resolve().parents[1]


def report_for(source):
    registered = 'username' in source and 'link' in source
    navigation = 'broken' not in source
    tests = [
        {'id': 'registration', 'requirementId': 'REQ-1', 'file': 'registration.spec.ts',
         'title': 'register and sign out', 'status': 'passed' if registered else 'failed',
         'error': '' if registered else ('missing link' if 'username' in source else 'missing username')},
        {'id': 'navigation', 'requirementId': 'REQ-1', 'file': 'navigation.spec.ts',
         'title': 'open registration', 'status': 'passed' if navigation else 'failed',
         'error': '' if navigation else 'registration inaccessible'},
    ]
    passed = sum(test['status'] == 'passed' for test in tests)
    return {'build': 'passed', 'startup': 'passed',
            'acceptance': 'passed' if passed == 2 else 'failed',
            'tests': tests, 'passed': passed, 'failed': 2 - passed, 'total': 2}


class RepairFlowTests(unittest.TestCase):
    def exercise(self, second_repair, stop_early=False, crash=False, first_repair_idle=False):
        with tempfile.TemporaryDirectory(prefix='arc-repair-') as directory:
            root = Path(directory)
            requirements = root / 'input' / 'requirements.yaml'
            requirements.parent.mkdir()
            requirements.write_text('id: REQ-1\ntype: ATOMIC\ndescription: Register and sign out.\n',
                                    encoding='utf-8')
            output = root / 'app'
            source = output / 'frontend' / 'src' / 'repair-state.txt'
            config = make_config(
                project_dir=PROJECT, openai_api_key='offline-test',
                openai_base_url='http://127.0.0.1:1/v1', model='offline', pi_reasoning='off',
                pi_time_budget_seconds=3600, pi_max_model_calls=20,
                run_token_budget=1000000, pi_max_output_tokens=1000,
                pi_context_bytes=8388608, pi_repair_rounds=2,
                acceptance_tests_dir=None,
                pi_smoke_port=None, pi_bash_path=None, pi_openspec_skill=False,
            )
            bridge = MagicMock()
            bridge.needs_compaction.return_value = False
            bridge.totals = {}
            bridge.context_overhead_bytes = None
            prompts = []
            states = []

            def prompt(text, deadline):
                prompts.append(text)
                states.append(source.read_text(encoding='utf-8') if source.exists() else '')
                if first_repair_idle and len(prompts) == 2:
                    return {'stopReason': 'turn_limit'}
                step = len(prompts) - (1 if first_repair_idle and len(prompts) > 2 else 0)
                if step == 1:
                    source.write_text('base', encoding='utf-8')
                elif step == 2:
                    source.write_text('username', encoding='utf-8')
                elif crash:
                    source.write_text('broken', encoding='utf-8')
                    raise RuntimeError('interrupted repair')
                elif second_repair is not None:
                    source.write_text(source.read_text(encoding='utf-8') + second_repair,
                                      encoding='utf-8')
                return {'stopReason': 'stop'}

            def verify(**kwargs):
                if kwargs.get('build_only'):
                    return {'build': 'passed'}
                return report_for(source.read_text(encoding='utf-8'))

            bridge.prompt.side_effect = prompt
            gate = [True, False] if stop_early else [True, True]
            with patch.object(runner, 'PiBridge', return_value=bridge), \
                    patch.object(runner, 'AgentRuntime'), \
                    patch.object(runner, 'verify_application', side_effect=verify), \
                    patch.object(runner, '_can_repair_dispatch', side_effect=gate):
                result = runner.run_task(config, requirements, output, 3000)
            return result, source.read_text(encoding='utf-8'), prompts, states

    def test_second_repair_builds_on_partial_fix(self):
        result, source, prompts, states = self.exercise(' link')
        self.assertEqual(states[2], 'username')
        self.assertIn('missing link', prompts[2])
        self.assertNotIn('missing username', prompts[2])
        self.assertEqual(source, 'username link')
        self.assertEqual(result['verification']['passed'], 2)
        self.assertEqual(result['outcome'], 'completed')
        self.assertEqual([row['status'] for row in result['progress']['checks']
                          if row['type'] == 'repair'], ['provisional', 'accepted'])

    def test_repair_that_only_reads_gets_a_continuation_to_edit(self):
        result, source, prompts, _ = self.exercise(' link', first_repair_idle=True)
        self.assertEqual(prompts[2], runner.REPAIR_CONTINUE_PROMPT)
        self.assertEqual(source, 'username link')
        self.assertEqual(result['verification']['passed'], 2)

    def test_second_repair_regression_restores_best_source(self):
        result, source, _, _ = self.exercise(' broken')
        self.assertEqual(source, 'base')
        self.assertEqual(result['verification']['passed'], 1)
        self.assertEqual(result['verification']['tests'][0]['error'], 'missing username')

    def test_no_change_after_partial_fix_restores_best_source(self):
        result, source, _, _ = self.exercise(None)
        self.assertEqual(source, 'base')
        self.assertEqual(result['verification']['passed'], 1)

    def test_deadline_after_partial_fix_restores_best_source(self):
        result, source, prompts, _ = self.exercise(None, stop_early=True)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(source, 'base')
        self.assertEqual(result['verification']['passed'], 1)

    def test_exception_after_partial_fix_restores_best_source(self):
        result, source, _, _ = self.exercise(None, crash=True)
        self.assertEqual(source, 'base')
        self.assertEqual(result['exitCode'], 1)
        self.assertEqual(result['verification']['passed'], 1)

    def test_provisional_candidate_requires_same_executed_suite(self):
        best = report_for('base')
        candidate = report_for('username')
        self.assertTrue(runner._repair_can_continue(best, candidate))
        for change in ('lost-pass', 'missing-test', 'skipped-test', 'failed-build'):
            with self.subTest(change=change):
                changed = copy.deepcopy(candidate)
                if change == 'lost-pass':
                    changed['tests'][1]['status'] = 'failed'
                elif change == 'missing-test':
                    changed['tests'].pop()
                elif change == 'skipped-test':
                    changed['tests'][0]['status'] = 'skipped'
                else:
                    changed['build'] = 'failed'
                self.assertFalse(runner._repair_can_continue(best, changed))

    def test_backend_error_stack_reaches_repair_prompt_once(self):
        log = ("Backend listening at http://127.0.0.1:3000\n"
               + "DataCloneError: #<Promise> could not be cloned.\n"
               "    at new DOMException (node:internal/per_context/domexception:53:5)\n"
               "    at serializeBoard (/workspace/template/backend/src/boardService.js:638:17)\n"
               "    at Layer.handleRequest (/workspace/template/backend/node_modules/router/lib/layer.js:152:17)\n"
               ) * 3
        report = {'build': 'passed', 'startup': 'passed', 'acceptance': 'not_run',
                  'serverErrors': verify.server_errors(log)}
        self.assertTrue(runner._needs_repair(report))
        lines = [line for line in runner._failure_lines(report, 10) if 'SERVER ERRORS' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('DataCloneError: #<Promise> could not be cloned.', lines[0])
        self.assertIn('boardService.js:638', lines[0])
        self.assertNotIn('node_modules', lines[0])
        self.assertEqual(lines[0].count('DataCloneError'), 1)

    def test_repair_is_kept_for_a_net_gain_even_if_one_old_pass_flips(self):
        def report(passing):
            return {'build': 'passed', 'startup': 'passed',
                    'tests': [{'id': name, 'status': 'passed' if name in passing else 'failed'}
                              for name in 'abcde']}
        best = report('ab')
        self.assertTrue(runner._repair_accepted(best, report('acde')))
        self.assertFalse(runner._repair_accepted(best, report('ac')))
        self.assertFalse(runner._repair_accepted(best, report('a')))


if __name__ == '__main__':
    unittest.main()
