"""Requirement-only ("blind") scenario tests: a separate session that sees the requirement files
but never the application source writes one Playwright test per scenario (start at `/`, click
and fill by quoted names, no API, URL or storage shortcuts). It runs in a background thread while
the main agent builds; its suite is the final verification suite, so the main agent's repairs aim
at user-visible behaviour instead of at its own tests."""
from __future__ import annotations

import re
import shutil
import threading
import time
import traceback
from pathlib import Path

from .bridge import BridgeError, PiBridge
from .processes import resolve_node_bin, run_command
from .requirements import describe_node

BLIND_DIR = "blind-tests"
HELPERS = "helpers.js"
HELPERS_SOURCE = ("playwright-e2e", "blind-helpers.js")
GROUP_PROMPT_SECONDS = 900
FIX_PROMPT_SECONDS = 600
MIN_PROMPT_SECONDS = 90
# high (the engine sends medium as high too): naming judgment matters most; replies cut at the
# output limit are recovered by continuations.
BLIND_REASONING = "high"
MAX_GROUP_CONTINUATIONS = 4
CONTINUE_STOPS = ("length", "turn_limit", "prompt_timeout", "error")  # error: e.g. bad tool JSON
SYNTAX_CHECK_SECONDS = 30
TEST_TITLE_RE = re.compile(r"""\btest\(\s*(['"`])(.+?)\1\s*,""")
# Shortcuts that bypass the user interface: a scenario test acts like the user (open `/`, click, fill).
FORBIDDEN = (
    (re.compile(r"\bpage\.goto\s*\("), "page.goto (only h.openHome may navigate)"),
    (re.compile(r"\b(?:page\.)?request\s*\.|/api/|\bfetch\s*\("), "API access"),
    (re.compile(r"\b(?:localStorage|sessionStorage)\b"), "browser storage"),
    (re.compile(r"\.evaluate\s*\(|\.route\s*\("), "page internals"),
    (re.compile(r"toHaveURL\s*\(|\bpage\.url\s*\("), "URL assertion"),
    (re.compile(r"""\.locator\(\s*['"`](?:\.|#|\[data-)"""), "class, id or data-attribute selector"),
)

BLIND_SYSTEM = """\
You write Playwright end-to-end tests for a web application from its requirement files only. You
cannot see the application source and must not guess its internals. Check only what the
requirement text states, as a user sees it.

Test contract (follow exactly):
- One CommonJS file per atomic requirement: `<requirement id>.spec.js` in the workspace root, e.g.
  `REQ-2-1-1.spec.js`. Start with `const { test, expect } = require('@playwright/test');` and
  `const h = require('./helpers');`.
- One `test(...)` per scenario, in scenario order; the title is the scenario name exactly as
  written in the requirement (it already starts with the requirement id).
- Every test starts with `await h.openHome(page);` (the only navigation). Reach every other page by
  clicking links, buttons, tabs and menus named in the requirement text, as a user would. Never
  call page.goto, fetch or any /api/ URL, never read storage, never use page.evaluate, page.route,
  toHaveURL, or CSS/id/data-attribute selectors.
- Locate by role and accessible name: `page.getByRole(role, { name: h.toPattern('Quoted name') })`,
  `page.getByLabel(...)`, `h.clickNamed(page, 'Name')`, `h.fillField(page, 'Label', value)`,
  `h.expectTextsVisible(page, [...])`, `h.expectAbsent(page, 'Name')`. Use `h.exact('Name')` when a
  shorter name would also match longer ones. Use only names and messages the requirement quotes or
  clearly states; never invent a label.
- Sign in through the visible sign-in form with the seed credentials the scenario gives.
- Seed records named in GIVEN steps already exist. Values the scenario creates or enters must be
  unique per run: `h.unique('prefix')`. The server and its data are shared by every test and never
  reset: never delete, rename or edit a seed record unless the scenario itself does so.
- Assert each THEN step through what the page shows. After a change that must persist, reload with
  `await page.reload()` and assert again.
- Each test must finish within 10 seconds: keep to the scenario's steps.

`helpers.js` already exists (read it once). Write files with the write tool only, ONE spec file per tool call
and at most two files per reply (a long reply is cut off and loses its files); keep each file
short and plain. Do not run tests, servers or other commands, and do not read anything except
`helpers.js` and the requirement files. Finish each reply with the list of files you wrote."""


def spec_issues(path: Path, scenario_names: list[str]) -> list[str]:
    """Contract violations of one spec file: forbidden shortcuts and titles that are not the
    requirement's scenario names."""
    text = path.read_text(encoding="utf-8", errors="replace")
    issues = [f"{path.name}: uses {label}" for pattern, label in FORBIDDEN if pattern.search(text)]
    titles = [match.group(2).strip() for match in TEST_TITLE_RE.finditer(text)]
    if not titles:
        issues.append(f"{path.name}: contains no test")
    unknown = [title for title in titles if title not in scenario_names]
    if unknown:
        issues.append(f"{path.name}: titles not equal to a scenario name: {unknown[:3]}")
    return issues


class BlindTester:
    def __init__(self, *, config, requirement_dir: Path, workdir: Path, evidence_dir: Path, log):
        self._config = config
        self._requirement_dir = Path(requirement_dir)
        self.workdir = Path(workdir)
        self._evidence = Path(evidence_dir)
        self._log = log
        self._thread: threading.Thread | None = None
        self.totals: dict = {}

    def start(self, groups: list[list[dict]], until: float) -> None:
        self._thread = threading.Thread(target=self._run_safely, args=(groups, until),
                                        name="blind-tests", daemon=True)
        self._thread.start()

    def join(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(max(0.0, timeout))
            if self._thread.is_alive():
                self._log("blind tests: still writing at final verification; using the finished files")

    def tests_dir(self) -> Path | None:
        """The suite, once at least one spec file was accepted (copied so later writes cannot race
        the verification run)."""
        specs = sorted(self.workdir.glob("*.spec.js"))
        if not specs:
            return None
        ready = self._evidence / "blind-suite"
        shutil.rmtree(ready, ignore_errors=True)
        ready.mkdir(parents=True)
        for path in specs:
            shutil.copy2(path, ready / path.name)
        # always the harness copy: the writer must not change the helpers' navigation
        shutil.copy2(Path(self._config.project_dir) / "skills" / HELPERS_SOURCE[0] / HELPERS_SOURCE[1],
                     ready / HELPERS)
        return ready

    def _run_safely(self, groups, until) -> None:
        try:
            self._run(groups, until)
        except Exception as exc:  # the build must never fail because of the test writer
            self._log(f"blind tests: stopped ({type(exc).__name__}: {str(exc)[:200]}) "
                      f"[{traceback.extract_tb(exc.__traceback__)[-1].lineno}]")

    def _run(self, groups: list[list[dict]], until: float) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(self._config.project_dir) / "skills" / HELPERS_SOURCE[0] / HELPERS_SOURCE[1],
                     self.workdir / HELPERS)
        names = {str(node.get("id")): [str(s.get("name") or "").strip() for s in node.get("scenarios") or []]
                 for group in groups for node in group}
        bridge = PiBridge(evidence_dir=self._evidence / "blind-session")
        bridge.start(config=self._config, workspace=self.workdir, read_roots=[self._requirement_dir],
                     system_prompt=BLIND_SYSTEM, deadline=until)
        try:
            for index, group in enumerate(groups, start=1):
                if until - time.monotonic() < MIN_PROMPT_SECONDS:
                    self._log(f"blind tests: time box ended before group {index}/{len(groups)}")
                    break
                if bridge.needs_compaction(self._config.pi_context_tokens):
                    bridge.restart()  # groups are independent: a fresh session needs no summary
                response = bridge.prompt(self._group_prompt(group),
                                         min(until, time.monotonic() + GROUP_PROMPT_SECONDS),
                                         reasoning=BLIND_REASONING)
                for _ in range(MAX_GROUP_CONTINUATIONS):
                    missing = [str(node.get("id")) for node in group
                               if not (self.workdir / f"{node.get('id')}.spec.js").is_file()]
                    if not missing or until - time.monotonic() < MIN_PROMPT_SECONDS:
                        break
                    if response.get("stopReason") not in CONTINUE_STOPS and response.get("stopReason") != "stop":
                        break
                    response = bridge.prompt(
                        "Continue: write the missing spec files now, one file per tool call: "
                        + ", ".join(f"{node_id}.spec.js" for node_id in missing),
                        min(until, time.monotonic() + GROUP_PROMPT_SECONDS),
                        reasoning=BLIND_REASONING)
                written = sum(1 for node in group if (self.workdir / f"{node.get('id')}.spec.js").is_file())
                self._log(f"blind tests: group {index}/{len(groups)} wrote {written}/{len(group)} "
                          f"file(s) (stop={response.get('stopReason')})")
            issues = self._validate(names, until)
            if issues and until - time.monotonic() >= MIN_PROMPT_SECONDS:
                bridge.prompt("Fix these contract problems in your test files now (edit the files; "
                              "keep scenario titles exactly):\n" + "\n".join(f"- {i}" for i in issues[:40]),
                              min(until, time.monotonic() + FIX_PROMPT_SECONDS),
                              reasoning=BLIND_REASONING)
                issues = self._validate(names, until)
            dropped = self._drop_broken(names, until)
            specs = len(list(self.workdir.glob("*.spec.js")))
            tests = sum(len(TEST_TITLE_RE.findall(p.read_text(encoding="utf-8", errors="replace")))
                        for p in self.workdir.glob("*.spec.js"))
            self._log(f"blind tests: {specs} spec file(s), {tests} test(s) ready; dropped {dropped}; "
                      f"{len(issues)} title issue(s) left")
        except BridgeError as exc:
            self._log(f"blind tests: engine error ({str(exc)[:200]})")
        finally:
            self.totals = bridge.totals
            bridge.close()

    def _group_prompt(self, group: list[dict]) -> str:
        ids = ", ".join(str(node.get("id")) for node in group)
        return ("Write the spec files for these requirements now: " + ids + ".\n\n"
                + "\n\n".join(describe_node(node) for node in group))

    def _validate(self, names: dict[str, list[str]], until: float) -> list[str]:
        issues: list[str] = []
        for path in sorted(self.workdir.glob("*.spec.js")):
            error = self._syntax_error(path, until)
            if error:
                issues.append(f"{path.name}: syntax error: {error}")
            issues += spec_issues(path, names.get(path.name[:-len(".spec.js")], []))
        return issues

    def _drop_broken(self, names: dict[str, list[str]], until: float) -> int:
        """One file with a syntax error stops the whole Playwright run, and a file that takes a
        shortcut passes without exercising the UI: remove both kinds."""
        dropped = 0
        for path in sorted(self.workdir.glob("*.spec.js")):
            shortcut = any("uses " in issue for issue in spec_issues(path, names.get(path.name[:-8], [])))
            if shortcut or self._syntax_error(path, until):
                path.unlink()
                dropped += 1
        return dropped

    @staticmethod
    def _syntax_error(path: Path, until: float) -> str | None:
        deadline = min(until, time.monotonic() + SYNTAX_CHECK_SECONDS)
        if deadline <= time.monotonic():
            deadline = time.monotonic() + SYNTAX_CHECK_SECONDS
        result = run_command([resolve_node_bin(deadline), "--check", str(path)], cwd=path.parent,
                             deadline=deadline)
        if result.returncode == 0:
            return None
        text = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else str(result.stderr)
        return next((line.strip() for line in text.splitlines() if "Error" in line), text.strip()[:200])
