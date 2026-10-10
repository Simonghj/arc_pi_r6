import re
import subprocess
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from arc_pi import review, runner, verify
from helpers import make_config

LEAF = """  - id: {id}
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
YAML = ("id: ROOT\nname: Demo\ntype: FOLDER\ndependencies: []\ndescription: Quoted names are exact.\n"
        "children:\n" + "".join(LEAF.format(id=f"REQ-{i}", name=f"page {i}") for i in (1, 2, 3)))
SUITES = []


def fake_verify(**kwargs):
    error = verify._check_frontend_imports(Path(kwargs['output_dir']) / 'frontend')
    if error:
        return {'build': 'failed', 'startup': 'not_run', 'error': error}
    if kwargs.get('build_only'):
        return {'build': 'passed'}
    tests_dir = kwargs.get('tests_dir')
    known = kwargs.get('known_requirement_ids') or set()
    rows = []
    if tests_dir is not None:
        SUITES.append((Path(kwargs['output_dir']), Path(tests_dir)))
        rows = [{'id': spec.name, 'requirementId': spec.stem.replace('.spec', ''),
                 'file': spec.name, 'title': f"{spec.stem.replace('.spec', '')}: works",
                 'status': 'passed', 'error': None}
                for spec in sorted(Path(tests_dir).glob('*.spec.js'))
                if spec.stem.replace('.spec', '') in known]
    return {'build': 'passed', 'startup': 'passed',
            'acceptance': 'passed' if rows else 'not_run',
            'tests': rows, 'passed': len(rows), 'failed': 0, 'total': len(rows)}


class FakeReviewBridge:
    """Reviewer engine stand-in: fixes the missing page and writes one spec per node."""
    totals = {'attempts': 1}
    reasoning = []

    def __init__(self, evidence_dir=None):
        self.workspace = None

    def start(self, config, workspace, read_roots, system_prompt, deadline):
        self.workspace = Path(workspace)
        self.system_prompt = system_prompt

    def needs_compaction(self, limit):
        return False

    def prompt(self, text, deadline, reasoning=None):
        FakeReviewBridge.reasoning.append(reasoning)
        ids = re.search(r'requirement nodes ([^.]+)\.', text).group(1).split(', ')
        tests = self.workspace / 'review-tests'
        tests.mkdir(exist_ok=True)
        for node_id in ids:
            (tests / f'{node_id}.spec.js').write_text('// scenario\n', encoding='utf-8')
        pages = self.workspace / 'frontend' / 'src' / 'pages'
        pages.mkdir(parents=True, exist_ok=True)
        (pages / 'ProjectPage.jsx').write_text(
            'export default function ProjectPage() { return null }\n', encoding='utf-8')
        app = self.workspace / 'frontend' / 'src' / 'App.jsx'
        app.write_text(app.read_text(encoding='utf-8') + '// reviewer\n', encoding='utf-8')
        return {'stopReason': 'stop', 'summary': 'Created ProjectPage.jsx.'}

    def close(self):
        pass


class ReviewerFlowTests(unittest.TestCase):
    def test_reviewer_fix_merges_three_way_and_results_reach_sdk(self):
        with tempfile.TemporaryDirectory(prefix='arc-review-test-') as directory:
            root = Path(directory)
            requirements = root / 'input' / 'requirements.yaml'
            requirements.parent.mkdir()
            requirements.write_text(YAML, encoding='utf-8')
            output = root / 'app'
            src = output / 'frontend' / 'src'
            config = make_config(pi_reviewer=True)
            main_prompts = []
            main = MagicMock()
            main.totals = {}
            main.context_overhead_bytes = None
            main.needs_compaction.return_value = False
            reviewed = threading.Event()

            def main_prompt(text, deadline):
                main_prompts.append(text)
                if 'Batch 1: implement' in text:
                    (src / 'App.jsx').write_text(
                        'import Project from "./pages/ProjectPage.jsx";\nexport default Project;\n',
                        encoding='utf-8')
                elif 'Batch 2: implement' in text:
                    self.assertTrue(reviewed.wait(10), 'reviewer never ran')
                    time.sleep(0.5)
                    (src / 'App.jsx').write_text(
                        '// main batch 2\nimport Project from "./pages/ProjectPage.jsx";\n'
                        'export default Project;\n', encoding='utf-8')
                return {'stopReason': 'stop', 'summary': 'done'}

            main.prompt.side_effect = main_prompt

            def bridge_factory(evidence_dir=None):
                if evidence_dir is not None and Path(evidence_dir).name == 'bridge':
                    bridge = FakeReviewBridge(evidence_dir)
                    bridge.close = reviewed.set
                    return bridge
                return main

            SUITES.clear()
            FakeReviewBridge.reasoning = []
            with patch.object(runner, 'PiBridge', side_effect=bridge_factory), \
                    patch.object(runner, 'AgentRuntime') as agent_runtime, \
                    patch.object(runner, 'verify_application', side_effect=fake_verify), \
                    patch.object(review, 'verify_application', side_effect=fake_verify):
                runner.run_task(config, requirements, output, 3000)

            batch3 = next(text for text in main_prompts if 'Batch 3: implement' in text)
            self.assertIn('Created ProjectPage.jsx', batch3)
            self.assertNotIn('currently FAILS', batch3)
            app = (src / 'App.jsx').read_text(encoding='utf-8')
            self.assertIn('// main batch 2', app)   # main edit kept
            self.assertIn('// reviewer', app)       # non-overlapping reviewer edit merged
            self.assertIn('Quoted names are exact.', main.start.call_args.kwargs['system_prompt'])
            passed = {call.args[0] for call in
                      agent_runtime.from_env.return_value.events.mark_test_passed.call_args_list}
            self.assertIn('REQ-1', passed)
            self.assertFalse((output / 'review-tests').exists())
            self.assertTrue([tests for app_dir, tests in SUITES if app_dir == output],
                            'final verification did not use the reviewer tests')
            self.assertTrue(FakeReviewBridge.reasoning)
            self.assertEqual(set(FakeReviewBridge.reasoning), {review.REVIEW_REASONING})

    def test_batch_that_breaks_a_reviewer_passed_scenario_gets_a_restore_prompt(self):
        with tempfile.TemporaryDirectory(prefix='arc-review-test-') as directory:
            root = Path(directory)
            requirements = root / 'input' / 'requirements.yaml'
            requirements.parent.mkdir()
            requirements.write_text(YAML, encoding='utf-8')
            output = root / 'app'
            src = output / 'frontend' / 'src'
            main_prompts = []
            main = MagicMock()
            main.totals = {}
            main.context_overhead_bytes = None
            main.needs_compaction.return_value = False
            reviewed = threading.Event()

            def main_prompt(text, deadline):
                main_prompts.append(text)
                if 'Batch 1: implement' in text:
                    (src / 'App.jsx').write_text('export default function App() { return null }\n',
                                                 encoding='utf-8')
                elif 'Batch 2: implement' in text:
                    self.assertTrue(reviewed.wait(10), 'reviewer never ran')
                    time.sleep(0.5)
                    (src / 'Broken.jsx').write_text('// batch 2 broke page 1\n', encoding='utf-8')
                return {'stopReason': 'stop', 'summary': 'done'}

            def guarded_verify(**kwargs):
                report = fake_verify(**kwargs)
                suite = kwargs.get('tests_dir')
                if (suite is not None and Path(suite).name.startswith('arc-guard-')
                        and (Path(kwargs['output_dir']) / 'frontend' / 'src' / 'Broken.jsx').exists()):
                    for row in report['tests']:
                        row.update(status='failed', error="Error: page 1 heading not found")
                return report

            main.prompt.side_effect = main_prompt

            def bridge_factory(evidence_dir=None):
                if evidence_dir is not None and Path(evidence_dir).name == 'bridge':
                    bridge = FakeReviewBridge(evidence_dir)
                    bridge.close = reviewed.set
                    return bridge
                return main

            with patch.object(runner, 'PiBridge', side_effect=bridge_factory), \
                    patch.object(runner, 'AgentRuntime'), \
                    patch.object(runner, 'verify_application', side_effect=guarded_verify), \
                    patch.object(review, 'verify_application', side_effect=fake_verify):
                runner.run_task(make_config(pi_reviewer=True), requirements, output, 3000)

            restore = [text for text in main_prompts if 'broke scenario(s) that passed before' in text]
            self.assertEqual(len(restore), 1)
            self.assertIn('Batch 2 broke', restore[0])
            self.assertIn('REQ-1.spec.js :: REQ-1: works', restore[0])
            self.assertIn('page 1 heading not found', restore[0])

    def run_levels(self, writes_spec):
        with tempfile.TemporaryDirectory(prefix='arc-review-test-') as directory:
            root = Path(directory)
            app = root / 'app'
            (app / 'frontend' / 'src').mkdir(parents=True)
            (app / 'backend').mkdir()
            levels = []

            class Recording(FakeReviewBridge):
                def prompt(self, text, deadline, reasoning=None):
                    levels.append(reasoning)
                    assert (self.workspace / 'review-tests' / 'helpers.js').is_file()
                    if writes_spec:
                        (self.workspace / 'review-tests' / 'REQ-1.spec.js').write_text('// t', encoding='utf-8')
                    return {'stopReason': 'stop', 'summary': ''}

            def always_failing(**kwargs):
                return {'build': 'passed', 'startup': 'passed', 'acceptance': 'failed',
                        'tests': [{'requirementId': 'REQ-1', 'title': 'REQ-1: works',
                                   'status': 'failed', 'error': 'not found'}],
                        'passed': 0, 'failed': 1, 'total': 1}

            with patch.object(review, 'verify_application', side_effect=always_failing):
                reviewer = review.BatchReviewer(
                    config=make_config(), output=app, evidence_dir=root / 'evidence',
                    requirement_dir=root, deadline=time.monotonic() + 900,
                    bridge_factory=Recording, file_listing=lambda path: '', log=lambda message: None)
                reviewer.submit(1, [{'id': 'REQ-1', 'name': 'page', 'description': 'x'}], [], '')
                reviewer.drain(until=time.monotonic() + 10)
                reviewer.close()
            return levels

    def test_reviewer_stays_at_max_while_it_writes_scenario_tests(self):
        # combined review+tests prompt, then every fix round, all at max
        self.assertEqual(self.run_levels(writes_spec=True),
                         [review.REVIEW_REASONING] * (1 + review.MAX_REVIEW_FIX_ROUNDS))

    def test_max_pass_that_writes_no_tests_continues_at_high(self):
        levels = self.run_levels(writes_spec=False)
        self.assertEqual(levels[0], 'max')
        self.assertEqual(set(levels[1:]), {'high'})
        self.assertEqual(len(levels), 1 + review.MAX_REVIEW_FIX_ROUNDS)

    def test_specs_that_ran_are_published_when_the_last_check_runs_none(self):
        with tempfile.TemporaryDirectory(prefix='arc-review-test-') as directory:
            root = Path(directory)
            app = root / 'app'
            (app / 'frontend' / 'src').mkdir(parents=True)
            (app / 'backend').mkdir()

            class SpecWriter(FakeReviewBridge):
                def prompt(self, text, deadline, reasoning=None):
                    (self.workspace / 'review-tests' / 'REQ-1.spec.js').write_text('// spec', encoding='utf-8')
                    return {'stopReason': 'stop', 'summary': ''}

            runs = []

            def first_suite_only(**kwargs):
                if kwargs.get('tests_dir') is None:
                    return {'build': 'passed', 'startup': 'passed', 'tests': [], 'total': 0}
                runs.append(1)
                rows = [{'requirementId': 'REQ-1', 'title': 'REQ-1: works',
                         'status': 'failed', 'error': 'not found'}] if len(runs) == 1 else []
                return {'build': 'passed', 'startup': 'passed', 'acceptance': 'failed',
                        'tests': rows, 'passed': 0, 'failed': len(rows), 'total': len(rows)}

            with patch.object(review, 'verify_application', side_effect=first_suite_only):
                reviewer = review.BatchReviewer(
                    config=make_config(), output=app, evidence_dir=root / 'evidence',
                    requirement_dir=root, deadline=time.monotonic() + 900,
                    bridge_factory=SpecWriter, file_listing=lambda path: '', log=lambda message: None)
                reviewer.submit(1, [{'id': 'REQ-1', 'name': 'page', 'description': 'x'}], [], '')
                reviewer.drain(until=time.monotonic() + 10)
                reviewer.close()
            self.assertGreater(len(runs), 1)
            self.assertTrue(reviewer.has_tests())

    def test_stop_mid_prompt_discards_unverified_changes_without_error(self):
        with tempfile.TemporaryDirectory(prefix='arc-review-test-') as directory:
            root = Path(directory)
            app = root / 'app'
            (app / 'frontend' / 'src').mkdir(parents=True)
            (app / 'backend').mkdir()
            logs = []
            entered = threading.Event()
            released = threading.Event()

            class Blocking(FakeReviewBridge):
                def prompt(self, text, deadline, reasoning=None):
                    (self.workspace / 'frontend' / 'src' / 'half.js').write_text('x', encoding='utf-8')
                    entered.set()
                    released.wait(10)
                    raise RuntimeError('bridge closed')

                def close(self):
                    released.set()

            with patch.object(review, 'verify_application', side_effect=fake_verify):
                reviewer = review.BatchReviewer(
                    config=make_config(), output=app, evidence_dir=root / 'evidence',
                    requirement_dir=root, deadline=time.monotonic() + 900,
                    bridge_factory=Blocking, file_listing=lambda path: '', log=logs.append)
                reviewer.submit(1, [{'id': 'REQ-1', 'name': 'page', 'description': 'x'}], [], '')
                self.assertTrue(entered.wait(10))
                reviewer.stop()
                merged = reviewer.merge_ready()
                reviewer.close()
            self.assertEqual(merged.applied, [])
            self.assertFalse((app / 'frontend' / 'src' / 'half.js').exists())
            self.assertTrue(any('discarded unverified' in line for line in logs), logs)
            self.assertFalse(any('reviewer error' in line for line in logs), logs)

    def test_three_way_merge_applies_disjoint_edits_and_rejects_overlap(self):
        base = b'a\nb\nc\nd\ne\n'
        self.assertEqual(review.merge_three_way(base, b'A\nb\nc\nd\ne\n', b'a\nb\nc\nd\nE\n'),
                         b'A\nb\nc\nd\nE\n')
        self.assertIsNone(review.merge_three_way(base, b'X\nb\nc\nd\ne\n', b'Y\nb\nc\nd\ne\n'))

    def test_timeout_output_bytes_are_logged_as_text(self):
        with tempfile.TemporaryDirectory() as directory:
            log = verify._Log(Path(directory) / 'build.log')
            timeout = subprocess.TimeoutExpired(['npm'], 1, output=b'partial \xe2\x9c\x93', stderr=b'err')
            with patch.object(verify, 'run_command', side_effect=timeout):
                result = verify._run(['npm'], cwd=Path(directory), deadline=0, log=log)
            self.assertTrue(result.timed_out)
            self.assertEqual(result.stdout, 'partial \u2713')
            self.assertIn('command timed out', log.tail())


class CompactionTests(unittest.TestCase):
    def test_full_context_compacts_before_next_batch_and_keeps_summary(self):
        with tempfile.TemporaryDirectory(prefix='arc-compact-') as directory:
            root = Path(directory)
            requirements = root / 'input' / 'requirements.yaml'
            requirements.parent.mkdir()
            requirements.write_text(YAML, encoding='utf-8')
            output = root / 'app'
            bridge = MagicMock()
            bridge.totals = {}
            bridge.context_overhead_bytes = None
            bridge.needs_compaction.side_effect = [False, True, False]
            bridge.compact.return_value = 'Home page lists pages; store in backend/data.'
            prompts = []

            def prompt(text, deadline):
                prompts.append(text)
                (output / 'frontend' / 'src' / f'p{len(prompts)}.js').write_text('x', encoding='utf-8')
                return {'stopReason': 'stop'}

            bridge.prompt.side_effect = prompt
            with patch.object(runner, 'PiBridge', return_value=bridge), \
                    patch.object(runner, 'AgentRuntime'), \
                    patch.object(runner, 'verify_application', side_effect=fake_verify):
                runner.run_task(make_config(), requirements, output, 3000)
            self.assertEqual(bridge.compact.call_count, 1)
            batch2 = next(text for text in prompts if 'Batch 2: implement' in text)
            self.assertIn('Project state from before your context reset', batch2)
            self.assertIn('Home page lists pages', batch2)
            self.assertNotIn('context reset', next(t for t in prompts if 'Batch 1: implement' in t))


class GatedReviewTests(unittest.TestCase):
    def test_next_batch_waits_for_review_and_sees_its_logged_errors(self):
        with tempfile.TemporaryDirectory(prefix='arc-gate-') as directory:
            root = Path(directory)
            requirements = root / 'input' / 'requirements.yaml'
            requirements.parent.mkdir()
            requirements.write_text(YAML, encoding='utf-8')
            output = root / 'app'
            src = output / 'frontend' / 'src'
            reviewed = threading.Event()
            seen_at_batch2 = []
            main_prompts = []
            main = MagicMock()
            main.totals = {}
            main.context_overhead_bytes = None
            main.needs_compaction.return_value = False

            def main_prompt(text, deadline):
                main_prompts.append(text)
                if 'Batch 1: implement' in text:
                    (src / 'App.jsx').write_text('export default function App() { return null }\n',
                                                 encoding='utf-8')
                elif 'Batch 2: implement' in text:
                    seen_at_batch2.append(reviewed.is_set())
                    (src / 'Two.jsx').write_text('// batch 2\n', encoding='utf-8')
                return {'stopReason': 'stop', 'summary': 'done'}

            def failing_check(**kwargs):
                report = fake_verify(**kwargs)
                for row in report.get('tests') or []:
                    row.update(status='failed', error="Test timeout of 10000ms exceeded.\n"
                               "Error: expect(locator).toBeVisible() failed\n"
                               "Locator: getByRole('heading', { name: 'Page 1' })")
                if report.get('tests'):
                    report.update(acceptance='failed', passed=0)
                return report

            main.prompt.side_effect = main_prompt

            def bridge_factory(evidence_dir=None):
                if evidence_dir is not None and Path(evidence_dir).name == 'bridge':
                    bridge = FakeReviewBridge(evidence_dir)
                    bridge.close = reviewed.set
                    return bridge
                return main

            with patch.object(runner, 'PiBridge', side_effect=bridge_factory), \
                    patch.object(runner, 'AgentRuntime'), \
                    patch.object(runner, 'verify_application', side_effect=fake_verify), \
                    patch.object(review, 'verify_application', side_effect=failing_check):
                runner.run_task(make_config(pi_reviewer=True), requirements, output, 3000)

            self.assertEqual(seen_at_batch2, [True])  # the review ran before batch 2 started
            log = (output / '.arc' / 'error-log.md').read_text(encoding='utf-8')
            self.assertIn('reviewer batch 1', log)
            self.assertIn("toBeVisible() failed | Locator: getByRole('heading', { name: 'Page 1' })", log)
            self.assertEqual(log.count("REQ-1: works: Error: expect(locator).toBeVisible()"), 1)
            batch2 = next(text for text in main_prompts if 'Batch 2: implement' in text)
            self.assertIn('## Project error log', batch2)
            self.assertIn("getByRole('heading', { name: 'Page 1' })", batch2)

    def test_reviewer_sees_vision_before_its_first_prompt_and_after_each_fix(self):
        with tempfile.TemporaryDirectory(prefix='arc-gate-') as directory:
            root = Path(directory)
            (root / 'reference').mkdir()
            (root / 'reference' / 'home.png').write_bytes(b'png')
            app = root / 'app'
            (app / 'frontend' / 'src').mkdir(parents=True)
            (app / 'backend').mkdir()
            (app / 'frontend' / 'src' / 'App.jsx').write_text('', encoding='utf-8')
            prompts = []

            class Recording(FakeReviewBridge):
                def prompt(self, text, deadline, reasoning=None):
                    prompts.append(text)
                    return super().prompt('requirement nodes REQ-1.', deadline, reasoning)

            def with_shots(**kwargs):
                report = fake_verify(**kwargs)
                if kwargs.get('screenshot_routes'):
                    report['screenshots'] = {route: {'path': str(root / 'reference' / 'home.png'),
                                                     'url': route}
                                             for route in kwargs['screenshot_routes']}
                return report

            answers = iter(['- missing "Create" button', 'NO ISSUES'])
            with patch.object(review, 'verify_application', side_effect=with_shots), \
                    patch.object(review.BatchReviewer, '_ask_vision',
                                 side_effect=lambda *args: next(answers, 'NO ISSUES')):
                reviewer = review.BatchReviewer(
                    config=make_config(visual_model='vision-test'), output=app,
                    evidence_dir=root / 'evidence', requirement_dir=root,
                    deadline=time.monotonic() + 900, bridge_factory=Recording,
                    file_listing=lambda path: '', log=lambda message: None)
                reviewer.submit(1, [{'id': 'REQ-1', 'name': 'home',
                                     'description': 'Looks like reference/home.png'}], [], '')
                reviewer.drain(until=time.monotonic() + 10)
                reviewer.close()
            self.assertIn('missing "Create" button', prompts[0])
            self.assertEqual(len(prompts), 1)  # vision then says NO ISSUES and tests pass: gate open

    def test_max_reply_that_times_out_empty_falls_back_to_high(self):
        with tempfile.TemporaryDirectory(prefix='arc-gate-') as directory:
            root = Path(directory)
            app = root / 'app'
            (app / 'frontend' / 'src').mkdir(parents=True)
            (app / 'backend').mkdir()
            (app / 'frontend' / 'src' / 'App.jsx').write_text('', encoding='utf-8')
            levels = []

            class SlowMax(FakeReviewBridge):
                def prompt(self, text, deadline, reasoning=None):
                    levels.append(reasoning)
                    if reasoning == 'max':
                        return {'stopReason': 'budget', 'summary': ''}
                    return super().prompt('requirement nodes REQ-1.', deadline, reasoning)

            with patch.object(review, 'verify_application', side_effect=fake_verify):
                reviewer = review.BatchReviewer(
                    config=make_config(), output=app, evidence_dir=root / 'evidence',
                    requirement_dir=root, deadline=time.monotonic() + 900,
                    bridge_factory=SlowMax, file_listing=lambda path: '', log=lambda message: None)
                reviewer.submit(1, [{'id': 'REQ-1', 'name': 'page', 'description': 'x'}], [], '')
                reviewer.drain(until=time.monotonic() + 10)
                reviewer.close()
            self.assertEqual(levels[:2], ['max', 'high'])
            self.assertTrue(reviewer.has_tests())


class SelfReviewTests(unittest.TestCase):
    def test_single_agent_fixes_its_own_batch_before_the_next_batch(self):
        with tempfile.TemporaryDirectory(prefix='arc-self-') as directory:
            root = Path(directory)
            requirements = root / 'input' / 'requirements.yaml'
            requirements.parent.mkdir()
            requirements.write_text(YAML, encoding='utf-8')
            output = root / 'app'
            src = output / 'frontend' / 'src'
            calls = []  # (kind, reasoning)
            bridge = MagicMock()
            bridge.totals = {}
            bridge.context_overhead_bytes = None
            bridge.needs_compaction.return_value = False

            def prompt(text, deadline, reasoning=None):
                if 'Batch 1: implement' in text:
                    calls.append(('batch1', reasoning))
                    (src / 'App.jsx').write_text('export default function App() { return null }\n',
                                                 encoding='utf-8')
                elif 'Batch 2: implement' in text:
                    calls.append(('batch2', reasoning))
                elif 'Review the application for requirement nodes REQ-1' in text:
                    calls.append(('review1', reasoning))
                    (output / 'review-tests' / 'REQ-1.spec.js').write_text('// t', encoding='utf-8')
                elif 'Scenario tests: 0/1 passed' in text:
                    calls.append(('fix1', reasoning))
                    (src / 'Fixed.jsx').write_text('// fixed', encoding='utf-8')
                else:
                    calls.append(('other', reasoning))
                return {'stopReason': 'stop', 'summary': 'done'}

            def check(**kwargs):
                report = fake_verify(**kwargs)
                if report.get('tests') and not (src / 'Fixed.jsx').exists():
                    for row in report['tests']:
                        row.update(status='failed', error='Error: expect(locator).toBeVisible() failed')
                    report.update(acceptance='failed', passed=0)
                return report

            bridge.prompt.side_effect = prompt
            with patch.object(runner, 'PiBridge', return_value=bridge), \
                    patch.object(runner, 'AgentRuntime'), \
                    patch.object(runner, 'verify_application', side_effect=check), \
                    patch.object(review, 'verify_application', side_effect=check):
                runner.run_task(make_config(pi_self_review=True, pi_reasoning='high'),
                                requirements, output, 3000)

            kinds = [kind for kind, _ in calls]
            self.assertEqual(kinds[:4], ['batch1', 'review1', 'fix1', 'batch2'])
            batch_handoffs = [args for args in bridge.compact.call_args_list if args.args]
            self.assertEqual(batch_handoffs, [])  # batches continue in one session until the context is full
            self.assertEqual(dict(calls)['review1'], 'high')
            log = (output / '.arc' / 'error-log.md').read_text(encoding='utf-8')
            self.assertIn('toBeVisible() failed', log)

    def test_agent_extended_test_helpers_survive_later_batches(self):
        with tempfile.TemporaryDirectory(prefix='arc-self-') as directory:
            root = Path(directory)
            app = root / 'app'
            (app / 'frontend' / 'src').mkdir(parents=True)
            (app / 'backend').mkdir()
            helpers = app / 'review-tests' / 'helpers.js'

            def converse(text, fixing, until=None):
                if not fixing and 'requirement nodes REQ-1.' in text:  # batch 1 only
                    helpers.write_text(helpers.read_text(encoding='utf-8') + '\nexports.signIn = 1;\n',
                                       encoding='utf-8')
                return ''

            with patch.object(review, 'verify_application', side_effect=fake_verify):
                reviewer = review.SelfReview(config=make_config(), output=app,
                                             evidence_dir=root / 'evidence', requirement_dir=root,
                                             file_listing=lambda path: '', log=lambda message: None,
                                             leaf_ids=['REQ-1', 'REQ-2'])
                for index, node in ((1, 'REQ-1'), (2, 'REQ-2')):
                    reviewer.run(index, [{'id': node, 'name': 'p', 'description': 'x'}], '',
                                 time.monotonic() + 600, converse)
            self.assertIn('exports.signIn', helpers.read_text(encoding='utf-8'))

    def test_gate_suite_holds_only_this_batchs_specs_and_shared_files(self):
        with tempfile.TemporaryDirectory(prefix='arc-self-') as directory:
            root = Path(directory)
            tests = root / 'app' / 'review-tests'
            tests.mkdir(parents=True)
            for name in ('REQ-1-1.spec.js', 'REQ-1-2.spec.js', 'REQ-2-1.spec.js', 'helpers.js', 'seed.js'):
                (tests / name).write_text('//', encoding='utf-8')
            reviewer = review.SelfReview(config=make_config(), output=root / 'app',
                                         evidence_dir=root / 'ev', requirement_dir=root,
                                         file_listing=lambda path: '', log=lambda message: None,
                                         leaf_ids=['REQ-1-1', 'REQ-1-2', 'REQ-2-1'])
            job = review.ReviewJob([2], [{'id': 'REQ-2-1'}], [], '', root / 'app', {})
            suite = root / 'suite'
            suite.mkdir()
            reviewer._batch_suite(job, suite)
            self.assertEqual(sorted(p.name for p in suite.iterdir()),
                             ['REQ-2-1.spec.js', 'helpers.js', 'seed.js'])

    def test_env_limits_and_prices_reach_their_own_config_fields(self):
        from arc_pi import config as config_module
        values = {'OPENAI_API_KEY': 'k', 'MODEL': 'glm-5.3-flash', 'PI_MAX_RUN_COST_CNY': '40',
                  'PI_MAX_TURNS_PER_PROMPT': '24'}
        prices = ('PI_PRICE_INPUT', 'PI_PRICE_OUTPUT', 'PI_PRICE_CACHE_HIT')
        with patch.dict(os.environ, values), patch.object(config_module, 'load_dotenv'):
            for name in prices:
                os.environ.pop(name, None)
            loaded = config_module.load_config()  # prices come from the model's table entry
            os.environ.update({'MODEL': 'deepseek-v4-flash', 'PI_PRICE_OUTPUT': '10'})
            overridden = config_module.load_config()
        self.assertEqual((loaded.pi_max_run_cost_cny, loaded.pi_price_input, loaded.pi_price_output,
                          loaded.pi_price_cache, loaded.pi_max_turns_per_prompt), (40, 0.8, 2.8, 0.23, 24))
        self.assertEqual((overridden.pi_price_input, overridden.pi_price_output, overridden.pi_price_cache),
                         (3.0, 10.0, 0.1))
        with patch.dict(os.environ, {**values, 'MODEL': 'mystery-model'}), \
                patch.object(config_module, 'load_dotenv'):
            for name in prices:
                os.environ.pop(name, None)
            unknown = config_module.load_config()
        bridge = MagicMock()
        bridge.totals = {'attempts': 1, 'inputTokens': 11_000_000, 'cacheReadTokens': 0, 'outputTokens': 1_000_000}
        # unknown model priced at the fallback rate: 11M x 3 + 1M x 9 = 42 CNY, over the 40 cap
        self.assertAlmostEqual(runner._run_cost_cny(bridge, unknown), 42.0)
        self.assertFalse(runner._can_dispatch(bridge, unknown, time.monotonic() + 60))
        self.assertEqual(loaded.bridge_config(1, Path('.'))['PI_MAX_TURNS_PER_PROMPT'], 24)
        self.assertEqual(runner._final_phase_seconds(39600, 3600), (1800, 3600))  # 11 h: 1 h wrap-up
        self.assertEqual(runner._final_phase_seconds(2400, 3600), (600, 1200))  # short run keeps half
        self.assertEqual(runner._final_phase_seconds(21600), (900, 2400))  # default, scaled

    def test_cost_cap_counts_cache_hits_at_their_price(self):
        bridge = MagicMock()
        bridge.totals = {'attempts': 1, 'inputTokens': 10_000_000, 'cacheReadTokens': 9_000_000,
                         'outputTokens': 1_000_000}
        config = make_config(pi_max_run_cost_cny=10, pi_price_input=3, pi_price_output=9, pi_price_cache=0.1)
        self.assertAlmostEqual(runner._run_cost_cny(bridge, config), 3 + 0.9 + 9)  # 12.9 CNY
        self.assertFalse(runner._can_dispatch(bridge, config, time.monotonic() + 60))
        self.assertTrue(runner._can_dispatch(bridge, make_config(pi_max_run_cost_cny=13, pi_price_input=3,
                                                                 pi_price_output=9, pi_price_cache=0.1),
                                             time.monotonic() + 60))

    def test_final_review_compares_every_mapped_reference_and_fixes_at_max(self):
        with tempfile.TemporaryDirectory(prefix='arc-final-') as directory:
            root = Path(directory)
            (root / 'reference').mkdir()
            for name in ('home.png', 'tasks.png', 'notes.png'):
                (root / 'reference' / name).write_bytes(b'png')
            app = root / 'app'
            (app / 'frontend' / 'src').mkdir(parents=True)
            (app / 'backend').mkdir()
            calls = []

            def converse(text, fixing, until=None):
                calls.append((fixing, text))
                return ('SCREENSHOT: reference/tasks.png /p/q/tasks\n'
                        'SCREENSHOT: reference/notes.png /p/q/notes\nSCREENSHOT: reference/home.png /')

            def with_shots(**kwargs):
                report = fake_verify(**kwargs)
                report['screenshots'] = {r: {'path': str(root / 'reference' / 'home.png'), 'url': r, 'aria': ''}
                                         for r in kwargs.get('screenshot_routes') or []}
                return report

            asked = []

            def vision(self, reference, screenshot, route, context):
                asked.append(route)
                return '- missing "New task" button' if route == '/p/q/tasks' and len(asked) < 4 else 'NO ISSUES'

            nodes = [{'id': f'REQ-{i}', 'name': n, 'description': f'See reference/{n}.png'}
                     for i, n in enumerate(('home', 'tasks', 'notes'), 1)]
            with patch.object(review, 'verify_application', side_effect=with_shots), \
                    patch.object(review.BatchReviewer, '_ask_vision', vision):
                reviewer = review.SelfReview(config=make_config(visual_model='v'), output=app,
                                             evidence_dir=root / 'ev', requirement_dir=root,
                                             file_listing=lambda path: '', log=lambda message: None,
                                             leaf_ids=['REQ-1', 'REQ-2', 'REQ-3'])
                reviewer.final_review(nodes, time.monotonic() + 900, converse)
            self.assertEqual(sorted(asked[:3]), ['/', '/p/q/notes', '/p/q/tasks'])
            self.assertTrue(all(fixing for fixing, _ in calls))
            self.assertIn('## Final review\n', calls[0][1])
            self.assertIn('missing "New task" button', calls[1][1])
            self.assertEqual(len(calls), 2)  # second check: every page matches

    def test_engine_crash_restarts_the_engine_and_the_run_finishes(self):
        from arc_pi.bridge import BridgeError
        with tempfile.TemporaryDirectory(prefix='arc-crash-') as directory:
            root = Path(directory)
            requirements = root / 'input' / 'requirements.yaml'
            requirements.parent.mkdir()
            requirements.write_text(YAML, encoding='utf-8')
            output = root / 'app'
            app = output / 'frontend' / 'src' / 'App.jsx'
            bridge = MagicMock()
            bridge.totals = {}
            bridge.context_overhead_bytes = None
            bridge.needs_compaction.return_value = False
            bridge.compact.return_value = 'notes'
            crashed = []

            def prompt(text, deadline, reasoning=None):
                if 'Batch 1: implement' in text:
                    app.write_text('export default function App() { return null }\n', encoding='utf-8')
                    if not crashed:
                        crashed.append(True)
                        raise BridgeError('bridge stdout closed unexpectedly')
                return {'stopReason': 'stop', 'summary': ''}

            bridge.prompt.side_effect = prompt
            with patch.object(runner, 'PiBridge', return_value=bridge), \
                    patch.object(runner, 'AgentRuntime'), \
                    patch.object(runner, 'verify_application', side_effect=fake_verify), \
                    patch.object(review, 'verify_application', side_effect=fake_verify):
                result = runner.run_task(make_config(pi_self_review=True), requirements, output, 3000)
            self.assertEqual(bridge.restart.call_count, 1)
            self.assertEqual(result['outcome'], 'partial')  # before the fix: 'failed' (BridgeError ended the run)

    def test_self_review_moves_on_after_two_fix_rounds_without_progress(self):
        with tempfile.TemporaryDirectory(prefix='arc-stale-') as directory:
            root = Path(directory)
            logs, prompts = [], []
            reviewer = review.SelfReview(config=make_config(), output=root / 'app',
                                         evidence_dir=root / 'ev', requirement_dir=root,
                                         file_listing=lambda path: '', log=logs.append,
                                         leaf_ids=['REQ-1'])
            stuck = {'build': 'passed', 'startup': 'passed', 'total': 1,
                     'tests': [{'status': 'failed', 'title': 'REQ-1 export', 'error': 'B4 empty'}]}
            with patch.object(review.SelfReview, '_check', lambda self, *a, **k: dict(stuck)), \
                    patch.object(review.BatchReviewer, '_vision', lambda self, *a: []):
                limits = []
                start = time.monotonic()
                reviewer.run(1, [{'id': 'REQ-1', 'name': 'Export'}], '', start + 3000,
                             lambda text, fixing, until=None: limits.append(until)
                             or prompts.append(fixing) or '')
        # the first reply (write specs) may use at most half the box; fix replies use the box
        self.assertLessEqual(limits[0] - start, 1500 + 5)
        self.assertEqual(limits[1:], [None, None])
        # first prompt, then two fixes that change nothing (checks 2 and 3 equal check 1): stop,
        # instead of the old 6 rounds / 18 minutes
        self.assertEqual(prompts, [False, True, True])
        self.assertTrue(any('no progress in 2 fix rounds' in line for line in logs))

    def test_stopping_a_process_after_the_deadline_never_raises(self):
        import subprocess
        import sys
        from arc_pi import processes
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        real = subprocess.run

        def hung_taskkill(argv, *args, **kwargs):
            if argv and argv[0] == 'taskkill':
                raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'))
            return real(argv, *args, **kwargs)

        with patch.object(processes.subprocess, 'run', hung_taskkill), patch.object(processes.os, 'name', 'nt'):
            processes.stop_process(child, deadline=time.monotonic() - 10)
        self.assertIsNotNone(child.poll())

    def test_spec_lint_flags_shortcuts_that_bypass_the_ui(self):
        with tempfile.TemporaryDirectory(prefix='arc-lint-') as directory:
            tests = Path(directory)
            (tests / 'REQ-1.spec.js').write_text(
                "await page.goto('/');\nawait page.goto('/demo-group');\n"
                "await page.goto('/settings/profile');\nconst r = await fetch('/api/x');\n"
                "await page.getByRole('link', { name: 'Home' }).first().click();\n", encoding='utf-8')
            (tests / 'REQ-2.spec.js').write_text(
                "await page.goto('/');\nawait page.getByRole('link', { name: 'Demo Group' }).click();\n",
                encoding='utf-8')
            notes = review._spec_lint_notes(tests, ['REQ-1', 'REQ-2', 'REQ-3'],
                                            'Open the page at /settings/profile directly.')
        self.assertEqual(len(notes), 2)  # REQ-2 navigates by clicks; REQ-3 has no spec
        self.assertIn('/demo-group', notes[0])
        self.assertNotIn('/settings/profile', notes[0])  # the requirement states that URL
        self.assertIn('fetch(', notes[1])
        self.assertIn('.first()', notes[1])

    def test_empty_vision_answer_is_not_counted_as_a_match(self):
        logs = []
        reviewer = review.SelfReview(config=make_config(visual_model='v'), output=Path('.'),
                                     evidence_dir=Path('.'), requirement_dir=Path('.'),
                                     file_listing=lambda path: '', log=logs.append, leaf_ids=['REQ-1'])
        job = review.ReviewJob([1], [], [], '', Path('.'), {}, time.monotonic() + 60)
        check = {'screenshots': {'/a': {'path': 'a.png', 'url': '/a'}, '/b': {'path': 'b.png', 'url': '/b'}}}
        answers = {'/a': '', '/b': 'NO ISSUES'}
        with patch.object(review.BatchReviewer, '_ask_vision',
                          lambda self, ref, shot, route, context: answers[route]):
            notes = reviewer._vision(job, [('ref-a.png', '/a'), ('ref-b.png', '/b')], check)
        self.assertEqual(notes, [])
        self.assertIn('vision: compared 1 pair(s), 0 with differences', logs)
        self.assertTrue(any('empty answer for ref-a.png' in line for line in logs))

    def test_final_review_that_breaks_the_build_is_reverted(self):
        with tempfile.TemporaryDirectory(prefix='arc-final-') as directory:
            root = Path(directory)
            requirements = root / 'input' / 'requirements.yaml'
            requirements.parent.mkdir()
            requirements.write_text(YAML, encoding='utf-8')
            output = root / 'app'
            app = output / 'frontend' / 'src' / 'App.jsx'
            bridge = MagicMock()
            bridge.totals = {}
            bridge.context_overhead_bytes = None
            bridge.needs_compaction.return_value = False
            bridge.compact.return_value = 'notes'
            finals = []

            def prompt(text, deadline, reasoning=None):
                if 'Batch 1: implement' in text:
                    app.write_text('export default function App() { return null }\n', encoding='utf-8')
                if '## Final review\n' in text:
                    finals.append(text)
                    app.write_text('import Gone from "./pages/Gone.jsx";\nexport default Gone;\n',
                                   encoding='utf-8')
                return {'stopReason': 'stop', 'summary': ''}

            bridge.prompt.side_effect = prompt
            with patch.object(runner, 'PiBridge', return_value=bridge), \
                    patch.object(runner, 'AgentRuntime'), \
                    patch.object(runner, 'verify_application', side_effect=fake_verify), \
                    patch.object(review, 'verify_application', side_effect=fake_verify):
                runner.run_task(make_config(pi_self_review=True), requirements, output, 3000)
            self.assertEqual(len(finals), 1)
            self.assertNotIn('Gone', app.read_text(encoding='utf-8'))

    def test_token_cap_stops_new_model_calls(self):
        bridge = MagicMock()
        bridge.totals = {'attempts': 1, 'inputTokens': 90, 'outputTokens': 10}
        deadline = time.monotonic() + 60
        self.assertFalse(runner._can_dispatch(bridge, make_config(pi_max_run_tokens=100), deadline))
        self.assertTrue(runner._can_dispatch(bridge, make_config(pi_max_run_tokens=101), deadline))
        self.assertTrue(runner._can_dispatch(bridge, make_config(pi_max_run_tokens=0), deadline))


class RenderedPageTests(unittest.TestCase):
    def test_unmapped_reference_is_only_compared_with_home_when_it_looks_like_home(self):
        pick = review.BatchReviewer._screenshot_routes
        self.assertEqual(pick(None, '', ['reference/manage-rows.png']), [])
        self.assertEqual(pick(None, '', ['reference/manage-rows.png', 'reference/board-home.png']),
                         [('reference/board-home.png', '/')])
        summary = 'SCREENSHOT: reference/manage-rows.png /board/1'
        self.assertEqual(pick(None, summary, ['reference/manage-rows.png']),
                         [('reference/manage-rows.png', '/board/1')])

    def test_controls_rendered_twice_block_the_gate_with_a_note(self):
        aria = ('- banner:\n  - link "Sign in"\n  - heading "Repos" [level=1]\n'
                '- navigation:\n  - link "Sign in"\n  - link "Explore"\n- heading "Repos" [level=2]')
        check = {'screenshots': {'/': {'aria': aria}}}
        notes = review._duplicate_notes(check)
        self.assertEqual(len(notes), 1)
        self.assertIn('link "Sign in" x2', notes[0])
        self.assertNotIn('Explore', notes[0])
        self.assertNotIn('heading', notes[0])
        self.assertIn('route /', review._aria_text(check))
        self.assertEqual(review._duplicate_notes({'screenshots': {'/': {'aria': '- link "Explore"'}}}), [])

    def test_placeholder_name_from_any_requirement_is_not_a_name_clash(self):
        from arc_pi.contract import ui_name_templates
        nodes = [{'id': 'REQ-1', 'description': 'Each tab has a button "Options for <tab name>".'},
                 {'id': 'REQ-2', 'description': 'A "Rename" item opens a dialog.'}]
        named = ui_name_templates(nodes)
        self.assertIn('Options for <tab name>', named)
        aria = '- tab "Page 1"\n- button "Options for Page 1"\n- link "Notes"\n- button "Edit Notes"'
        notes = review._shadow_notes({'screenshots': {'/': {'aria': aria}}}, named)
        self.assertEqual(len(notes), 1)
        self.assertNotIn('Options for Page 1', notes[0])
        self.assertIn('button "Edit Notes" contains "Notes"', notes[0])

    def test_button_whose_name_contains_a_link_name_is_flagged_unless_the_requirement_names_it(self):
        aria = ('- link "Monthly report"\n- button "More options for Monthly report"\n- tab "Page 1"\n'
                '- button "Delete octo"\n- link "octo"\n- button "Page 1 options"\n- button "Notes"')
        notes = review._shadow_notes({'screenshots': {'/': {'aria': aria}}}, ['Delete <name>'])
        self.assertEqual(len(notes), 1)
        self.assertIn('button "More options for Monthly report" contains "Monthly report"', notes[0])
        self.assertIn('button "Page 1 options" contains "Page 1"', notes[0])
        self.assertNotIn('Delete octo', notes[0])
        self.assertEqual(review._shadow_notes({'screenshots': {'/': {'aria': '- link "Notes"\n- button "Notes"'}}}, []), [])


class VerifyCopyTests(unittest.TestCase):
    def test_dependency_install_failure_is_a_build_error_not_a_crash(self):
        with tempfile.TemporaryDirectory(prefix='arc-deps-') as directory:
            app = Path(directory) / 'app'
            for part in ('frontend', 'backend'):
                (app / part).mkdir(parents=True)
                (app / part / 'package.json').write_text('{}', encoding='utf-8')
            with patch.object(verify, 'report_verification_errors'), patch.object(verify, '_ensure_installed',
                              side_effect=verify.VerifyError('npm install failed (exit 1): lock file')):
                report = verify.verify_application(output_dir=app, report_dir=Path(directory) / 'rep',
                                                   deadline=time.monotonic() + 30, grading_port=3000,
                                                   build_only=True)
        self.assertEqual(report['build'], 'failed')
        self.assertIn('dependency install failed', report['error'])

    def test_app_copy_runs_outside_dot_folders(self):
        # Express/send 404s files under dot-folders; evidence lives under `.arc`.
        seen = []

        def record(source, destination):
            seen.append(Path(destination))
            raise RuntimeError('stop after copy')

        with tempfile.TemporaryDirectory(prefix='arc-copy-') as directory:
            app = Path(directory) / 'app'
            for part in ('frontend', 'backend'):
                (app / part).mkdir(parents=True)
                (app / part / 'package.json').write_text('{}', encoding='utf-8')
            with patch.object(verify, '_copy_tree', side_effect=record), \
                    self.assertRaises(RuntimeError):
                verify.verify_application(output_dir=app, report_dir=app / '.arc' / 'report',
                                          deadline=time.monotonic() + 30, grading_port=3000)
        self.assertTrue(seen)
        self.assertFalse([part for part in seen[0].parts if part.startswith('.')], seen[0])


if __name__ == '__main__':
    unittest.main()
