"""Batch review: a separate reviewer session (BatchReviewer) or the executor itself (SelfReview)."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .prompts import E2E_SKILL, REVIEW_SKILL, skill_text
from .requirements import describe_node
from .contract import ui_names
from .verify import verify_application

SNAPSHOT_EXCLUDED = frozenset({"node_modules", "dist", "dist-ssr", ".cache", "data", ".git", ".arc"})
REVIEW_REASONING = "max"
REVIEW_FALLBACK_REASONING = "high"  # used after a max reply times out with no output
REVIEW_JOB_SECONDS = 900
REVIEW_PROMPT_SECONDS = 480
REVIEW_VERIFY_SECONDS = 150
REVIEW_SUITE_SECONDS = 90
MIN_ROUND_SECONDS = 150
SELF_VERIFY_SECONDS = 300
SELF_SUITE_SECONDS = 240  # the self-review suite holds every batch's specs
MAX_SELF_FIX_ROUNDS = 6
FIRST_REPLY_SHARE = 0.5  # of the self-review box, for the first reply
MAX_STALE_ROUNDS = 2  # fix rounds in a row without more passing specs or fewer page issues
MAX_FINAL_ROUNDS = 2
MAX_FINAL_PAIRS = 12
MAX_REVIEW_CONTINUATIONS = 3
MAX_REVIEW_FIX_ROUNDS = 6
MAX_JOB_NODES = 8
STOP_JOIN_SECONDS = 45
REVIEW_TESTS_DIR = "review-tests"
TEST_HELPERS = "helpers.js"
MAX_VISION_PAIRS = 3
VISION_TIMEOUT_SECONDS = 120
ARIA_LINE_RE = re.compile(r'^\s*-\s*(link|button|heading|tab|menuitem|textbox|checkbox|combobox)\s+"([^"]+)"')
MAX_ARIA_CHARS = 2500
HOME_REF_RE = re.compile(r"home|overview|landing|index|dashboard|list", re.IGNORECASE)
REFERENCE_RE = re.compile(r"reference/[\w.\-]+\.(?:png|jpe?g|webp)", re.IGNORECASE)
SCREENSHOT_LINE_RE = re.compile(r"SCREENSHOT:\s*`?([^\s`]+)`?\s+`?(/[^\s`]*)`?")

REVIEW_SYSTEM = """\
You are the REVIEWER of a generated full-stack web application. Another agent implements \
requirement batches one after another and WAITS for you: the next batch starts only when the \
scenario tests for this batch pass and its pages match the reference screenshots. You work on a \
private copy; make the code for the given requirement nodes actually work, then prove it with \
Playwright tests.

Workspace contract: frontend/ (Vite + React, `npm run build` -> frontend/dist) and backend/ \
(Node.js + Express, `npm run start` honors PORT, serves frontend/dist and JSON APIs under /api, \
persists to a JSON file, hashes passwords with Node crypto scrypt). Only PORT is used.

Check and fix: build/startup errors first; every frontend fetch has a backend route with the \
same method, path and JSON shape; mutations persist across reload; predefined seed data exists; \
exact accessible names and roles quoted by the requirement; pages reachable from the home page.

Never: rewrite working features, implement requirements you were not given, add placeholders, \
stubs, fake data or test-specific shortcuts, edit package-lock.json, node_modules, .env, .git or \
.arc. Tool paths are relative to the workspace root. Never run npm or start servers: the harness \
builds, starts and tests the app after each of your replies. Keep replies short and end with a \
summary of the files you changed."""


def _hash_bytes(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _app_contents(root: Path) -> dict[str, bytes]:
    """Relative path -> bytes for reviewable app files (no deps/build/data)."""
    files: dict[str, bytes] = {}
    for sub in ("frontend", "backend"):
        base = root / sub
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            parts = path.relative_to(root).parts
            if any(part in SNAPSHOT_EXCLUDED for part in parts) or not path.is_file():
                continue
            data = _read(path)
            if data is not None:
                files["/".join(parts)] = data
    return files


def _app_state(root: Path) -> dict[str, str]:
    return {rel: _hash_bytes(data) for rel, data in _app_contents(root).items()}


def _copy_app(source: Path, destination: Path) -> None:
    ignore = lambda _directory, names: sorted(SNAPSHOT_EXCLUDED & set(names))  # noqa: E731
    for sub in ("frontend", "backend"):
        if (source / sub).is_dir():
            shutil.copytree(source / sub, destination / sub, ignore=ignore, symlinks=True)


def merge_three_way(base: bytes, ours: bytes, theirs: bytes) -> bytes | None:
    """`git merge-file` of reviewer (theirs) into live (ours); None on conflict."""
    with tempfile.TemporaryDirectory(prefix="arc-merge-") as directory:
        paths = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = Path(directory) / name
            path.write_bytes(data)
            paths.append(str(path))
        try:
            done = subprocess.run(["git", "merge-file", "-p", *paths], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
    return done.stdout if done.returncode == 0 else None


@dataclass
class ReviewJob:
    batches: list[int]
    nodes: list[dict]
    failed_build_ids: list[str]
    context_text: str
    work_dir: Path
    base: dict[str, bytes]
    until: float | None = None


@dataclass
class ReviewResult:
    job: ReviewJob
    changed: dict[str, bytes | None] = field(default_factory=dict)
    summary: str = ""
    build: str = "not_run"
    startup: str = "not_run"
    error: str = ""
    vision_notes: list[str] = field(default_factory=list)
    test_rows: list[dict] = field(default_factory=list)


@dataclass
class MergeOutcome:
    applied: list[str]
    conflicts: list[str]
    notes: str
    fixed_build_ids: list[str]
    test_rows: list[dict] = field(default_factory=list)


class BatchReviewer:
    def __init__(self, *, config, output: Path, evidence_dir: Path, requirement_dir: Path,
                 deadline: float, bridge_factory, file_listing, log, root_text: str = "",
                 error_log=None):
        self._config = config
        self._output = Path(output)
        self._evidence = Path(evidence_dir) / "review"
        self._requirement_dir = Path(requirement_dir)
        self._deadline = deadline
        self._bridge_factory = bridge_factory
        self._file_listing = file_listing
        self._log = log
        self._root_text = root_text
        self._error_log = error_log
        self._level = REVIEW_REASONING
        skills_dir = Path(config.project_dir) / "skills"
        review_skill, _ = skill_text(skills_dir, [REVIEW_SKILL])
        self._e2e_skill, _ = skill_text(skills_dir, [E2E_SKILL])
        self._test_helpers = skills_dir / E2E_SKILL / TEST_HELPERS
        self._system = "\n\n".join(part for part in (REVIEW_SYSTEM, review_skill) if part)
        self._scratch = Path(tempfile.mkdtemp(prefix="arc-review-"))
        self.tests_dir = self._evidence / "tests"
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._pending: ReviewJob | None = None
        self._running: ReviewJob | None = None
        self._carry: list[dict] = []
        self._results: list[ReviewResult] = []
        self._bridge = None
        self._stopped = False
        self._jobs = 0
        self.totals: list[dict] = []
        self._thread = threading.Thread(target=self._worker, name="batch-reviewer", daemon=True)
        self._thread.start()

    # main thread

    def submit(self, batch_index: int, nodes: list[dict], failed_build_ids: list[str],
               context_text: str, until: float | None = None) -> None:
        """Snapshot the app now; a newer job absorbs an unstarted older one and leftovers."""
        with self._lock:
            if self._stopped:
                return
            self._jobs += 1
            work = self._scratch / f"job-{self._jobs:03d}"
            _copy_app(self._output, work)
            job = ReviewJob([batch_index], list(nodes), list(failed_build_ids), context_text,
                            work, _app_contents(work), until)
            earlier = list(self._carry)
            self._carry = []
            older = self._pending
            if older is not None:
                job.batches = older.batches + job.batches
                earlier = earlier + older.nodes
                job.failed_build_ids = sorted(set(older.failed_build_ids) | set(failed_build_ids))
                job.context_text = older.context_text + "\n" + context_text
                shutil.rmtree(older.work_dir, ignore_errors=True)
            seen = {str(node.get("id")) for node in job.nodes}
            job.nodes = [n for n in earlier if str(n.get("id")) not in seen] + job.nodes
            self._pending = job
            self._wake.notify_all()

    def merge_ready(self) -> MergeOutcome | None:
        """Apply finished reviews to the live app (main thread, between prompts)."""
        with self._lock:
            results, self._results = self._results, []
        if not results:
            return None
        applied: list[str] = []
        conflicts: list[str] = []
        notes: list[str] = []
        fixed: list[str] = []
        test_rows: list[dict] = []
        for result in results:
            job = result.job
            clean = True
            for rel, theirs in sorted(result.changed.items()):
                if self._apply(rel, job.base.get(rel), theirs):
                    applied.append(rel)
                else:
                    conflicts.append(rel)
                    clean = False
            label = ", ".join(str(index) for index in job.batches)
            if result.summary:
                notes.append(f"Reviewer (batch {label}): {result.summary.strip()[:1200]}")
            if result.vision_notes:
                notes.append("Visual differences still to check:\n" + "\n".join(result.vision_notes)[:1500])
            if result.build != "passed" and result.error:
                notes.append(f"Reviewer check after its fixes: {result.error.strip()[:600]}")
            if clean and result.build == "passed" and result.startup == "passed":
                fixed.extend(job.failed_build_ids)
            if clean:
                test_rows.extend(result.test_rows)
            failing = [row for row in result.test_rows if row.get("status") != "passed"]
            if failing:
                notes.append("Scenario tests still failing:\n" + "\n".join(
                    f"- {row.get('title')}: {_first_line(row.get('error'))}" for row in failing[:8]))
            shutil.rmtree(job.work_dir, ignore_errors=True)
        return MergeOutcome(applied, conflicts, "\n\n".join(notes), fixed, test_rows)

    def _apply(self, rel: str, base: bytes | None, theirs: bytes | None) -> bool:
        target = self._output / rel
        ours = _read(target)
        if _hash_bytes(ours) == _hash_bytes(base):
            content = theirs
        elif base is not None and ours is not None and theirs is not None:
            content = merge_three_way(base, ours, theirs)
            if content is None:
                return False
        else:
            return False
        if content is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return True

    def has_tests(self) -> bool:
        return self.tests_dir.is_dir() and any(self.tests_dir.glob("*.spec.js"))

    def drain(self, until: float) -> None:
        """Wait for queued/running reviews to finish, at most until ``until``."""
        with self._lock:
            while (self._pending is not None or self._running is not None) and not self._stopped:
                remaining = until - time.monotonic()
                if remaining <= 0:
                    break
                self._wake.wait(timeout=min(remaining, 1.0))

    def stop(self) -> None:
        """Interrupt the running job (it may still salvage a verified state) and stop."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._pending = None
            bridge = self._bridge
            self._wake.notify_all()
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass
        self._thread.join(timeout=STOP_JOIN_SECONDS)

    def close(self) -> None:
        self.stop()
        shutil.rmtree(self._scratch, ignore_errors=True)

    # worker thread

    def _worker(self) -> None:
        while True:
            with self._lock:
                while self._pending is None and not self._stopped:
                    self._wake.wait()
                if self._stopped:
                    return
                job, self._pending = self._pending, None
                if len(job.nodes) > MAX_JOB_NODES:
                    self._carry = job.nodes[:-MAX_JOB_NODES] + self._carry
                    job.nodes = job.nodes[-MAX_JOB_NODES:]
                self._running = job
            try:
                result = self._review(job)
            except Exception as exc:  # a failed review must never break the run
                result = ReviewResult(job, error=f"{type(exc).__name__}: {exc}")
                self._log(f"reviewer error on batch {job.batches}: {_trace(exc)}")
            with self._lock:
                self._running = None
                self._results.append(result)
                self._wake.notify_all()

    def _check(self, job: ReviewJob, name: str, until: float, routes=None, with_tests=False) -> dict:
        tests = job.work_dir / REVIEW_TESTS_DIR
        use_tests = with_tests and tests.is_dir() and any(tests.glob("*.spec.js"))
        return verify_application(
            output_dir=job.work_dir, tests_dir=tests if use_tests else None,
            report_dir=self._evidence / job.work_dir.name / name,
            deadline=min(until, time.monotonic() + REVIEW_VERIFY_SECONDS),
            grading_port=0, command="review", screenshot_routes=routes,
            known_requirement_ids={str(node.get("id")) for node in job.nodes},
            suite_timeout_seconds=REVIEW_SUITE_SECONDS)

    def _converse(self, bridge, text: str, until: float, work_dir: Path) -> str:
        """Max-reasoning prompt plus bounded continuations; compacts when context is full."""
        if bridge.needs_compaction(self._config.pi_context_tokens):
            summary = bridge.compact()
            self._log("reviewer context compacted -> fresh session")
            text = f"## Your notes before the context reset\n{summary}\n\n{text}"
        summaries: list[str] = []
        prompt = text
        for _ in range(1 + MAX_REVIEW_CONTINUATIONS):
            if until - time.monotonic() < 30:
                break
            before = _app_state(work_dir)
            response = bridge.prompt(prompt, min(until, time.monotonic() + REVIEW_PROMPT_SECONDS),
                                     reasoning=self._level)
            summaries.append(str(response.get("summary") or ""))
            if (self._level != REVIEW_FALLBACK_REASONING and not summaries[-1].strip()
                    and response.get("stopReason") in ("budget", "cancelled", "prompt_timeout")
                    and _app_state(work_dir) == before):
                self._level = REVIEW_FALLBACK_REASONING
                self._log(f"reviewer: {REVIEW_REASONING} reply timed out with no output; "
                          f"continuing at {self._level}")
            # budget/cancelled = this prompt's deadline cut the reply; continue unless stopped
            if (self._stopped or response.get("stopReason") not in
                    ("turn_limit", "length", "prompt_timeout", "budget", "cancelled")):
                break
            prompt = ("Continue the review work you started and finish it now. If the Playwright "
                      f"spec files for these nodes are not in {REVIEW_TESTS_DIR}/ yet, write them now.")
        return "\n".join(part for part in summaries if part.strip())

    def _review(self, job: ReviewJob) -> ReviewResult:
        result = ReviewResult(job)
        started = time.monotonic()
        until = min(self._deadline, job.until or started + REVIEW_JOB_SECONDS)
        if until - started < MIN_ROUND_SECONDS:
            return result
        label = ", ".join(str(index) for index in job.batches)
        ids = ", ".join(str(node.get("id")) for node in job.nodes)
        self._log(f"reviewer started for batch {label} ({ids})")
        self._level = REVIEW_REASONING
        if self._test_helpers.is_file():
            (job.work_dir / REVIEW_TESTS_DIR).mkdir(parents=True, exist_ok=True)
            shutil.copy2(self._test_helpers, job.work_dir / REVIEW_TESTS_DIR / TEST_HELPERS)
        references = self._references(job)
        routes = self._screenshot_routes("", references)
        check = self._check(job, "check-0", until, routes=[r for _, r in routes] or None)
        vision = self._vision(job, routes, check) + _duplicate_notes(check)  # look first
        self._record(label, "check-0", check, vision)
        checked_state = _app_state(job.work_dir)
        tested = check
        interrupted = False
        bridge = self._bridge_factory(evidence_dir=self._evidence / job.work_dir.name / "bridge")
        with self._lock:
            if self._stopped:
                return result
            self._bridge = bridge
        try:
            bridge.start(config=self._config, workspace=job.work_dir,
                         read_roots=[self._requirement_dir], system_prompt=self._system,
                         deadline=self._deadline)
            talk_until = until - REVIEW_VERIFY_SECONDS  # leave the job's test check its time
            summary = self._converse(bridge, self._first_prompt(job, check, references, vision),
                                     talk_until, job.work_dir)
            if (self._level != REVIEW_FALLBACK_REASONING
                    and not any((job.work_dir / REVIEW_TESTS_DIR).glob("*.spec.js"))):
                # local runs: a max pass could think for minutes and write nothing
                self._level = REVIEW_FALLBACK_REASONING
                self._log(f"reviewer: {REVIEW_REASONING} pass wrote no scenario tests; "
                          f"continuing at {self._level}")
            routes = self._screenshot_routes(summary, references)
            check = self._check(job, "check-1", until, routes=[r for _, r in routes] or None,
                                with_tests=True)
            checked_state = _app_state(job.work_dir)
            tested = check
            vision = self._vision(job, routes, check) + _duplicate_notes(check)
            self._record(label, "check-1", check, vision)
            for round_index in range(MAX_REVIEW_FIX_ROUNDS):
                if _passed(check) and not vision:
                    break
                if talk_until - time.monotonic() < MIN_ROUND_SECONDS:
                    break
                self._log(f"reviewer fix round {round_index + 1} for batch {label}")
                summary += "\n" + self._converse(bridge, self._fix_prompt(check, vision),
                                                 talk_until, job.work_dir)
                routes = self._screenshot_routes(summary, references) or routes
                name = f"check-{round_index + 2}"
                check = self._check(job, name, until, routes=[r for _, r in routes] or None,
                                    with_tests=True)
                checked_state = _app_state(job.work_dir)
                vision = self._vision(job, routes, check) + _duplicate_notes(check)
                self._record(label, name, check, vision)
                if check.get("total"):
                    tested = check
            result.vision_notes = vision
            result.summary = summary
            self._log(f"reviewer gate for batch {label}: "
                      + ("passed (tests pass, pages match)" if _passed(check) and not vision
                         else "not passed within the time box; the next batch starts anyway"))
        except Exception as exc:
            interrupted = True
            self._log(f"reviewer interrupted on batch {label}: {_trace(exc)}")
        finally:
            with self._lock:
                self._bridge = None
            try:
                bridge.close()
            except Exception:
                pass
            self.totals.append(dict(getattr(bridge, "totals", {}) or {}))
        result.build = check.get("build", "not_run")
        result.startup = check.get("startup", "not_run")
        if result.build != "passed" or result.startup != "passed":
            result.error = str(check.get("error") or "")
        verified = result.build == "passed" and result.startup == "passed"
        if interrupted and (not verified or _app_state(job.work_dir) != checked_state):
            self._log(f"reviewer discarded unverified changes for batch {label}")
            return result
        after = _app_contents(job.work_dir)
        for rel in sorted(set(after) | set(job.base)):
            if after.get(rel) != job.base.get(rel):
                result.changed[rel] = after.get(rel)
        if not tested.get("total"):
            tested = check
        result.test_rows = [row for row in tested.get("tests") or [] if row.get("requirementId")]
        self._publish_tests(job, tested)
        passed = sum(1 for row in result.test_rows if row.get("status") == "passed")
        self._log(f"reviewer finished batch {label} in {int(time.monotonic() - started)}s: "
                  f"build={result.build} startup={result.startup} changed files={len(result.changed)} "
                  f"scenario tests passed={passed}/{len(result.test_rows)}")
        return result

    def _record(self, label: str, name: str, check: dict, vision: list[str]) -> None:
        """Write every failure of this check into the project error log."""
        if self._error_log is None:
            return
        lines = []
        if check.get("build") != "passed" or check.get("startup") != "passed":
            lines.append(f"build={check.get('build')} startup={check.get('startup')}: "
                         f"{_error_digest(check.get('error'))}")
        lines += [f"backend error: {error}" for error in check.get("serverErrors") or []]
        lines += [f"TEST {row.get('title')}: {_error_digest(row.get('error'))}"
                  for row in check.get("tests") or [] if row.get("status") != "passed"]
        lines += [f"VISION {note}" for note in vision]
        self._error_log.add(f"reviewer batch {label} {name}", lines)

    def _publish_tests(self, job: ReviewJob, check: dict) -> None:
        """Keep this job's spec files when they loaded and ran (total > 0)."""
        source = job.work_dir / REVIEW_TESTS_DIR
        if not check.get("total") or not source.is_dir():
            return
        with self._lock:
            self.tests_dir.mkdir(parents=True, exist_ok=True)
            for spec in [*source.glob("*.spec.js"), source / TEST_HELPERS]:
                if spec.is_file():
                    shutil.copy2(spec, self.tests_dir / spec.name)

    # prompts

    def _references(self, job: ReviewJob) -> list[str]:
        text = "\n".join(describe_node(node) for node in job.nodes) + "\n" + job.context_text
        refs: list[str] = []
        for match in REFERENCE_RE.findall(text):
            if match not in refs and (self._requirement_dir / match).is_file():
                refs.append(match)
        return refs

    def _first_prompt(self, job: ReviewJob, check: dict, references: list[str],
                      vision: list[str]) -> str:
        ids = [str(node.get("id")) for node in job.nodes]
        parts = [f"Review the application for requirement nodes {', '.join(ids)}."]
        if self._root_text:
            parts.append(self._root_text)
        if job.context_text.strip():
            parts.append("## Section context\n" + job.context_text.strip()[:4000])
        parts += [
            "## Requirement nodes to review\n" + "\n\n".join(describe_node(node) for node in job.nodes),
            "## Automatic check of this snapshot\n" + _check_text(check),
        ]
        aria = _aria_text(check)
        if aria:
            parts.append("## Rendered accessibility tree (what Playwright getByRole/getByLabel see; "
                         "write test locators from these real names)\n" + aria)
        if vision:
            parts.append("## The current frontend compared with the reference screenshots\n"
                         + "\n".join(vision))
        if self._error_log is not None and self._error_log.recent():
            parts.append("## Project error log (errors seen earlier; keep them fixed)\n"
                         + self._error_log.recent())
        parts += [
            "## Step 1: fix\nRead the relevant frontend and backend files and fix every defect "
            "for these nodes (build/startup errors first). Make each page match its reference "
            "screenshot: same regions, headings, labels, buttons, tabs, tables and their order.",
            "## Step 2: write scenario tests\n"
            f"Write `{REVIEW_TESTS_DIR}/<node id>.spec.js` for each node (e.g. "
            f"`{REVIEW_TESTS_DIR}/{ids[0]}.spec.js`), one test per GIVEN/WHEN/THEN scenario, "
            "titles starting with `<node id>: `. Derive every step and name from the requirement "
            "text only: start with `await h.openHome(page);` (the only navigation; never call "
            "page.goto) and reach pages through visible links and buttons; never assume a "
            "route, heading or label the requirement does not state. Each test creates the data "
            "it checks through the UI (fill the forms the requirement describes); assert seed data "
            "only when the requirement names it exactly.",
        ]
        if self._e2e_skill:
            parts.append(self._e2e_skill)
        if references:
            parts.append(
                "## Reference screenshots (read-only, under the requirement input root)\n"
                + "\n".join(f"- {ref}" for ref in references)
                + "\n\nAt the end of your final reply add one line per reference: "
                "`SCREENSHOT: <reference path> <app route starting with />` for the page of this "
                "app that corresponds to it.")
        parts.append("## Current application source files\n" + self._file_listing(job.work_dir))
        return "\n\n".join(parts)

    def _fix_prompt(self, check: dict, vision: list[str]) -> str:
        parts = ["## Automatic check after your changes\n" + _check_text(check)]
        aria = _aria_text(check)
        if aria:
            parts.append("## Rendered accessibility tree now\n" + aria)
        failing = [row for row in check.get("tests") or [] if row.get("status") != "passed"]
        if failing:
            parts.append(
                f"## Scenario tests: {check.get('passed', 0)}/{check.get('total', 0)} passed; failing:\n"
                + "\n".join(f"- {row.get('title')}\n  {str(row.get('error') or '')[:400]}"
                            for row in failing[:10])
                + "\n\nFor each failure decide from the requirement text: if the test follows the "
                "requirement, fix the APPLICATION; if the test assumed something the requirement "
                "does not state (a route, name or page structure), fix the TEST to follow the "
                "requirement. Never weaken an assertion the requirement states.")
        elif not check.get("total") and check.get("build") == "passed":
            parts.append(f"## No scenario tests ran\nWrite `{REVIEW_TESTS_DIR}/<node id>.spec.js` for "
                         "every node now (one test per scenario); the next batch waits for them.")
        elif check.get("acceptance") == "failed" and check.get("error"):
            parts.append(f"## Scenario test run failed\n{str(check.get('error'))[:1200]}\n\n"
                         f"If the test files under `{REVIEW_TESTS_DIR}/` do not load, fix them.")
        if vision:
            parts.append(
                "## Visual comparison with the reference screenshots\n" + "\n".join(vision)
                + "\n\nMake the page match the reference: labels, controls, sections, layout and "
                "order, as far as the requirement text supports. Ignore sample data values.")
        if failing and self._e2e_skill:
            parts.append(self._e2e_skill)
        parts.append("Fix these now with concrete file edits.")
        return "\n\n".join(parts)

    def _screenshot_routes(self, summary: str, references: list[str],
                           limit: int = MAX_VISION_PAIRS) -> list[tuple[str, str]]:
        routes: list[tuple[str, str]] = []
        for ref, route in SCREENSHOT_LINE_RE.findall(summary or ""):
            if ref in references and (ref, route) not in routes:
                routes.append((ref, route))
        if not routes:
            # Without the agent's route mapping only a home-like reference may be compared with "/";
            # pairing a grid picture with the home page made the agent break a working page.
            home = next((ref for ref in references if HOME_REF_RE.search(ref)), None)
            if home:
                routes.append((home, "/"))
        return routes[:limit]

    # vision

    def _vision(self, job: ReviewJob, routes: list[tuple[str, str]], check: dict) -> list[str]:
        if not routes:
            return []
        if not self._config.visual_model:
            self._log("vision: skipped (VISUAL_MODEL not set)")
            return []
        shots = check.get("screenshots") or {}
        if not shots:
            self._log(f"vision: skipped ({str(check.get('screenshotError') or 'no screenshots')[:200]})")
            return []
        notes: list[str] = []
        compared = 0
        for ref, route in routes:
            shot = shots.get(route) or {}
            if not shot.get("path"):
                continue
            try:
                answer = self._ask_vision(self._requirement_dir / ref, Path(shot["path"]), route,
                                          job.context_text)
            except Exception as exc:
                self._log(f"vision: unavailable ({type(exc).__name__}: {str(exc)[:200]})")
                break
            if not answer.strip():
                # an empty reply (the model spent its tokens thinking) is no verdict, not a match
                self._log(f"vision: empty answer for {ref}; not counted as a match")
                continue
            compared += 1
            if "NO ISSUES" not in answer.upper():
                notes.append(f"- {ref} vs route {route} (landed on {shot.get('url')}):\n{answer.strip()[:1200]}")
        self._log(f"vision: compared {compared} pair(s), {len(notes)} with differences")
        return notes

    def _ask_vision(self, reference: Path, screenshot: Path, route: str, context: str) -> str:
        def image(path: Path) -> dict:
            mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}

        instruction = (
            "Image 1 is the reference design for a requirement; image 2 is the current app at "
            f"route {route}. The app should closely resemble the reference in structure and layout. "
            "List concrete, fixable differences: missing buttons, links, headings, fields, tabs, menus, "
            "tables or sections; regions in a different place or order; a completely different page. "
            "Only report elements the requirement context below describes; the app must use the exact "
            "English names from that text, so never ask to translate or copy the reference's language. "
            "Ignore sample data values and small color, font or spacing changes. "
            "Reply with a short bullet list, or exactly NO ISSUES.\n\n"
            "Requirement context:\n" + context[:2000])
        payload = {
            "model": self._config.visual_model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": instruction}, image(reference), image(screenshot)]}],
            "max_tokens": 3000,  # reasoning models spent 800 on thinking and returned nothing
        }
        request = urllib.request.Request(
            self._config.visual_base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {self._config.visual_api_key}"},
            method="POST")
        with urllib.request.urlopen(request, timeout=VISION_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
        return str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")


class SelfReview(BatchReviewer):
    """Single-agent mode: the executor reviews its own batch in its own session.

    Reuses the reviewer's checks, vision, prompts and error log on the live app; there is
    no reviewer thread, snapshot or merge, so BatchReviewer.__init__ is not called.
    """

    def __init__(self, *, config, output: Path, evidence_dir: Path, requirement_dir: Path,
                 file_listing, log, leaf_ids, error_log=None, named_ui=(), clash_check=True):
        self._config = config
        self._named_ui = list(named_ui)  # whole requirement file: names a NAME CLASH must keep
        # an app from an earlier stage may carry names that earlier requirement text demands
        self._clash_check = clash_check
        self._output = Path(output)
        self._evidence = Path(evidence_dir) / "self-review"
        self._requirement_dir = Path(requirement_dir)
        self._file_listing = file_listing
        self._log = log
        self._root_text = ""  # already in the executor's system prompt
        self._error_log = error_log
        self._level = REVIEW_REASONING
        self._stopped = False
        self._lock = threading.Lock()
        skills_dir = Path(config.project_dir) / "skills"
        self._e2e_skill, _ = skill_text(skills_dir, [E2E_SKILL])
        self._test_helpers = skills_dir / E2E_SKILL / TEST_HELPERS
        self._leaf_ids = set(leaf_ids)
        self.tests_dir = self._output / REVIEW_TESTS_DIR

    def _clash_notes(self, check: dict, nodes: list[dict]) -> list[str]:
        return _shadow_notes(check, self._named_ui or ui_names(nodes)) if self._clash_check else []

    def _batch_suite(self, job, scratch: Path) -> Path | None:
        """This batch's spec files (+ shared helpers) so the gate suite finishes in time;
        the whole suite is re-run once in final verification."""
        specs = sorted(self.tests_dir.glob("*.spec.js")) if self.tests_dir.is_dir() else []
        if not specs:
            return None
        ids = [str(node.get("id")) for node in job.nodes]
        mine = [spec for spec in specs if any(spec.name.startswith(node_id + ".")
                                              or spec.name.startswith(node_id + "-") for node_id in ids)]
        for path in self.tests_dir.iterdir():
            if path.is_file() and (path in (mine or specs) or not path.name.endswith(".spec.js")):
                shutil.copy2(path, scratch / path.name)
        return scratch

    def _check(self, job, name, until, routes=None, with_tests=False) -> dict:
        with tempfile.TemporaryDirectory(prefix="arc-batch-suite-") as scratch:
            suite = self._batch_suite(job, Path(scratch)) if with_tests else None
            return self._verify(name, until, routes, suite)

    def _verify(self, name, until, routes, suite) -> dict:
        return verify_application(
            output_dir=self._output, tests_dir=suite,
            report_dir=self._evidence / name,
            deadline=min(until, time.monotonic() + SELF_VERIFY_SECONDS),
            grading_port=0, command="review", screenshot_routes=routes,
            known_requirement_ids=self._leaf_ids, suite_timeout_seconds=SELF_SUITE_SECONDS)

    def run(self, batch_index: int, nodes: list[dict], context_text: str, until: float,
            converse) -> dict:
        """Check, look (vision + accessibility tree), write/run specs, fix until everything
        passes or ``until``. ``converse(text, fixing)`` prompts the executor."""
        job = ReviewJob([batch_index], list(nodes), [], context_text, self._output, {}, until)
        label = str(batch_index)
        self.tests_dir.mkdir(parents=True, exist_ok=True)
        if self._test_helpers.is_file() and not (self.tests_dir / TEST_HELPERS).exists():
            # only seed it: the agent extends helpers.js (e.g. signIn) and its specs depend on that
            shutil.copy2(self._test_helpers, self.tests_dir / TEST_HELPERS)
        references = self._references(job)
        routes = self._screenshot_routes("", references)
        name = f"batch-{batch_index:03d}-0"
        check = self._check(job, name, until, routes=[r for _, r in routes] or None,
                            with_tests=True)
        issues = (self._vision(job, routes, check) + _duplicate_notes(check)
                  + self._clash_notes(check, job.nodes))
        self._record(label, name, check, issues)
        # the first reply (fixes + new specs) gets at most half the box, so fix rounds always run
        first_until = time.monotonic() + max(MIN_ROUND_SECONDS, (until - time.monotonic()) * FIRST_REPLY_SHARE)
        summary = converse(self._first_prompt(job, check, references, issues), False, first_until)
        vision_rounds = 0  # vision is advisory once the agent has had one round to act on it
        best = (-1, 1 << 30)  # (specs passed, page issues) of the best round so far
        stale = 0
        for round_index in range(1, MAX_SELF_FIX_ROUNDS + 1):
            if until - time.monotonic() < 60:
                break  # no time left for an honest check (it would report a false build timeout)
            routes = self._screenshot_routes(summary, references) or routes
            name = f"batch-{batch_index:03d}-{round_index}"
            check = self._check(job, name, until, routes=[r for _, r in routes] or None,
                                with_tests=True)
            vision = self._vision(job, routes, check)
            duplicates = _duplicate_notes(check) + _spec_lint_notes(
                self.tests_dir, [str(node.get("id")) for node in nodes], context_text)
            issues = (vision if vision_rounds < 1 else []) + duplicates
            self._record(label, name, check, vision + duplicates)
            if vision and not duplicates and _passed(check):
                vision_rounds += 1
            passed = sum(1 for row in check.get("tests") or [] if row.get("status") == "passed")
            if _passed(check) and not issues:
                self._log(f"self-review batch {label}: passed ({passed}/{check.get('total', 0)} "
                          "specs, pages match)")
                return check
            if until - time.monotonic() < MIN_ROUND_SECONDS:
                break
            if passed > best[0] or (passed == best[0] and len(issues) < best[1]):
                best, stale = (passed, len(issues)), 0
            else:
                stale += 1
            if stale >= MAX_STALE_ROUNDS:
                # the same failures after repeated fixes: the time is worth more on the next batch
                self._log(f"self-review batch {label}: no progress in {stale} fix rounds "
                          f"({passed}/{check.get('total', 0)} specs); moving on")
                return check
            self._log(f"self-review batch {label}: fix round {round_index} "
                      f"({passed}/{check.get('total', 0)} specs, {len(issues)} page issue(s))")
            summary = converse(self._fix_prompt(check, issues), True)
        self._log(f"self-review batch {label}: not passed in its time box; moving on")
        return check

    def final_review(self, nodes: list[dict], until: float, converse, notes=()) -> None:
        """Whole app at the end: every reference picture vs its page (vision), accessibility
        tree and duplicate controls, then fix rounds. Specs run later in final verification."""
        job = ReviewJob([0], list(nodes), [], "", self._output, {}, until)
        references = self._references(job)
        self._log(f"final review: {len(nodes)} requirement node(s), {len(references)} reference picture(s)")
        check = self._check(job, "final-0", until, routes=["/"])
        issues = _duplicate_notes(check) + self._clash_notes(check, nodes)
        summary = converse(self._final_prompt(job, check, references, issues, list(notes)), True)
        for round_index in range(1, MAX_FINAL_ROUNDS + 1):
            if until - time.monotonic() < 90:
                break
            routes = self._screenshot_routes(summary, references, limit=MAX_FINAL_PAIRS)
            name = f"final-{round_index}"
            check = self._check(job, name, until, routes=[r for _, r in routes] or ["/"])
            issues = (self._vision(job, routes, check) + _duplicate_notes(check)
                      + self._clash_notes(check, nodes))
            self._record("final", name, check, issues)
            if not issues and check.get("build") == "passed" and check.get("startup") == "passed":
                self._log(f"final review: {len(routes)} page(s) match their references")
                return
            if until - time.monotonic() < MIN_ROUND_SECONDS:
                break
            self._log(f"final review round {round_index}: {len(issues)} issue(s) on {len(routes)} page(s)")
            summary = converse(self._fix_prompt(check, issues)
                               + "\n\nKeep the SCREENSHOT lines at the end of your reply.", True)
        self._log("final review: time box ended")

    def _final_prompt(self, job, check, references, issues, notes=()) -> str:
        ids = ", ".join(f"{node.get('id')} {node.get('name') or ''}".strip() for node in job.nodes)
        parts = [
            "## Final review\nAll batches are done. Review the WHOLE application "
            "against the requirement files (read them again under the requirement input root) and "
            "the reference pictures, then fix what differs: exact accessible names and roles, pages "
            "reachable from the home page through the flows the requirements describe, seed data, "
            "persistence after reload, and layout versus each picture. Change only what is wrong; "
            "do not rewrite working features.",
            "## Requirement nodes\n" + ids,
            "## Automatic check now\n" + _check_text(check),
        ]
        if issues:
            parts.append("## Problems found on the home page\n" + "\n".join(issues))
        if notes:
            parts.append("## Requirement names and seed records missing from the source\n"
                         + "\n".join(notes) + "\nRender each UI name exactly (skip example inputs); "
                         "create each seed record in backend startup code.")
        if self._error_log is not None and self._error_log.recent():
            parts.append("## Project error log\n" + self._error_log.recent())
        if references:
            parts.append("## Reference pictures\n" + "\n".join(f"- {ref}" for ref in references)
                         + "\n\nAt the end of your reply add one line per reference: "
                         "`SCREENSHOT: <reference path> <app route starting with />` (a route that "
                         "shows that page when opened directly; the harness screenshots it and "
                         "compares it with the picture).")
        parts.append("## Current application source files\n" + self._file_listing(self._output))
        return "\n\n".join(parts)


def _aria_text(check: dict) -> str:
    parts = [f"### route {route}\n{shot['aria'][:MAX_ARIA_CHARS]}"
             for route, shot in (check.get("screenshots") or {}).items() if shot.get("aria")]
    return "\n".join(parts)


SPEC_GOTO_RE = re.compile(r"""page\.goto\(\s*['"`]([^'"`]*)['"`]""")
SPEC_SHORTCUT_RE = re.compile(r"""\bfetch\(|\brequest\.(get|post|put|patch|delete)\(|\.(first|last)\(\)|\.nth\(""")


def _spec_lint_notes(tests_dir: Path, node_ids: list[str], requirement_text: str) -> list[str]:
    """Specs that skip the user's path (deep `page.goto`, API calls, index-picked duplicates)
    pass without checking that pages are reachable from `/`: flag them in this batch's specs."""
    notes = []
    for node_id in node_ids:
        spec = tests_dir / f"{node_id}.spec.js"
        if not spec.is_file():
            continue
        text = spec.read_text(encoding="utf-8", errors="replace")
        deep = sorted({path for path in SPEC_GOTO_RE.findall(text)
                       if path not in ("", "/") and path not in requirement_text})
        shortcuts = sorted({match.group(0) for match in SPEC_SHORTCUT_RE.finditer(text)})
        if deep:
            notes.append(f"- SPEC {spec.name} opens {', '.join(deep[:5])} with page.goto: scenarios "
                         "start at `/` and the user reaches every page by clicking visible links and buttons. "
                         "Make the APP reachable that way (home, header, menus, lists) and change the "
                         "test to navigate by clicks.")
        if shortcuts:
            notes.append(f"- SPEC {spec.name} uses {', '.join(shortcuts[:5])}: a scenario test acts only "
                         "through the UI and the quoted names (getByRole with the quoted name). Remove API calls; if a "
                         "name matches two elements, fix the APP so it renders once, not the test.")
    return notes


SHADOW_TARGET_ROLES = ("link", "tab")


def _template_re(name: str) -> re.Pattern:
    parts = re.split(r"<[^>]+>", name)
    return re.compile("^" + ".+".join(re.escape(part) for part in parts) + "$", re.I)


def _shadow_notes(check: dict, quoted: list[str]) -> list[str]:
    """A button whose name contains a link's or tab's whole name ("More options for Monthly report"
    next to the "Monthly report" link) makes that name ambiguous for the user and for assistive
    technology. Buttons the requirement names itself are left alone."""
    allowed = [_template_re(name) for name in quoted]
    notes = []
    for route, shot in (check.get("screenshots") or {}).items():
        targets, buttons = set(), set()
        for line in str(shot.get("aria") or "").splitlines():
            match = ARIA_LINE_RE.match(line)
            if not match:
                continue
            role, name = match.group(1), match.group(2).strip()
            if role in SHADOW_TARGET_ROLES and len(name) >= 3:
                targets.add(name)
            elif role == "button":
                buttons.add(name)
        clashes = sorted({(button, target) for button in buttons for target in targets
                          if button.lower() != target.lower() and target.lower() in button.lower()
                          and not any(rule.match(button) for rule in allowed)})
        if clashes:
            notes.append(f"- NAME CLASH on route {route}: " + ", ".join(
                f'button "{button}" contains "{target}"' for button, target in clashes[:6])
                + ". The shorter name then no longer identifies one control. "
                "Give such buttons a name without the item's name (e.g. \"More options\") unless "
                "the requirement names them.")
    return notes


def _duplicate_notes(check: dict) -> list[str]:
    """Controls rendered twice with one name break every `getByRole(role, {name})` test (strict mode)."""
    notes = []
    for route, shot in (check.get("screenshots") or {}).items():
        counts: dict[tuple[str, str], int] = {}
        for line in str(shot.get("aria") or "").splitlines():
            match = ARIA_LINE_RE.match(line)
            if match:
                key = (match.group(1), match.group(2).strip())
                counts[key] = counts.get(key, 0) + 1
        dupes = [f'{role} "{name}" x{n}' for (role, name), n in counts.items()
                 if n > 1 and role != "heading"]
        if dupes:
            notes.append(f"- DUPLICATE controls on route {route}: " + ", ".join(dupes[:8])
                         + ". Render each named control once per page (a test clicking it fails "
                         "in Playwright strict mode); keep the one the requirement places.")
    return notes


def _passed(check: dict) -> bool:
    """Gate: frontend and backend work and every scenario test that ran passed."""
    return (check.get("build") == "passed" and check.get("startup") == "passed"
            and bool(check.get("total")) and check.get("acceptance") == "passed"
            and not check.get("serverErrors"))


def _error_digest(error) -> str:
    """The informative lines of a Playwright/build error (skips the timeout banner)."""
    lines = [line.strip() for line in str(error or "").splitlines() if line.strip()]
    useful = [line for line in lines if not line.startswith("Test timeout of")
              and not line.startswith("Call log") and not line.startswith("at ")]
    return " | ".join(useful[:3])[:300]


def _trace(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)[-3:]
    where = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames))
    return f"{type(exc).__name__}: {exc} [{where}]"


def _first_line(text) -> str:
    return next((part.strip() for part in str(text or "").splitlines() if part.strip()), "")[:300]


def _check_text(report: dict) -> str:
    lines = [f"build: {report.get('build')}", f"startup: {report.get('startup')}"]
    if report.get("build") != "passed" or report.get("startup") != "passed":
        lines.append(f"error: {str(report.get('error') or '')[:1500]}")
    home = report.get("homePage") or {}
    if home.get("pageErrors"):
        lines.append("home page errors: " + "; ".join(home["pageErrors"])[:600])
    if report.get("serverErrors"):
        lines.append("backend errors logged during the check (fix first): "
                     + "; ".join(report["serverErrors"])[:900])
    return "\n".join(lines)
