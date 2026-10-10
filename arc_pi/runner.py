from __future__ import annotations

import hashlib
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from arcbench_agent_runtime import AgentRuntime
from arc_pi.artifacts import atomic_json, restore_app, snapshot_app, source_hash
from arc_pi.blindtest import BLIND_DIR, FORBIDDEN, HELPERS_SOURCE, TEST_TITLE_RE, BlindTester
from arc_pi.bridge import BridgeError, PiBridge
from arc_pi.config import Config
from arc_pi.contract import contract_notes, data_files, seeds_only_in_data, ui_name_templates
from arc_pi.errorlog import redact_secrets, report_error
from arc_pi.prompts import (
    CHUNK_GROWTH_RESERVE_BYTES,
    CHUNK_MIN_BYTES,
    ALWAYS_SKILL,
    SYSTEM_PREFIX,
    combined_chain_text,
    contract_prompt,
    folder_contracts,
    root_contract,
    batch_prompt,
    chunked_batch_prompts,
    failure_digest,
    fit_batches,
    group_batches,
    nudge_prompt,
    select_skills,
    skill_text,
)
from arc_pi.requirements import (
    RequirementError,
    external_dependencies,
    load_requirement_tree,
    node_index,
    top_folder_of_leaves,
    topo_order,
    validate_requirement_tree,
)
from arc_pi.projectlog import ProjectErrorLog
from arc_pi.review import TEST_HELPERS, BatchReviewer, SelfReview
from arc_pi.verify import verify_application

# Final review of the whole app (vision vs every reference, max effort), then the full suite
# (up to 900 s) and one repair.
FINAL_REVIEW_SECONDS = 900
FINAL_RESERVE_SECONDS = 1500 + FINAL_REVIEW_SECONDS


def _final_phase_seconds(budget: float, fixed: int = 0) -> tuple[float, float]:
    """(final review box, whole final reserve): full size on the 6 h platform budget, scaled
    down for short runs so they still get batch time."""
    if fixed:  # PI_FINAL_PHASE_SECONDS: half whole-app review, half full suite + repair
        fixed = min(float(fixed), budget / 2)  # a short run still gets batch time
        return min(fixed / 2, 2 * FINAL_REVIEW_SECONDS), fixed
    review = min(FINAL_REVIEW_SECONDS, max(180.0, budget / 24))
    return review, review + min(FINAL_RESERVE_SECONDS - FINAL_REVIEW_SECONDS, max(420.0, budget * 0.07))
FINAL_VERIFY_SECONDS = 240
# 100+ specs at up to 10 s each need far more than a few minutes.
FINAL_SUITE_SECONDS = 2700.0  # own + blind suites (~200 tests, failures wait 10 s each)
BATCH_OVERRUN_SECONDS = 60
MAX_REPAIR_FAILURES = 15
MIN_REPAIR_RESERVE_SECONDS = 60
VERIFY_WATCHDOG_SECONDS = 2400
GUARD_VERIFY_SECONDS = 150
# Gated review: after each batch the reviewer works (max effort) until its tests pass and pages
# match the references, or this time box ends; only then does the next batch start.
REVIEW_GATE_SECONDS = 900
REVIEW_GATE_GRACE_SECONDS = 60
# Single agent (PI_REVIEWER=self): after each batch the executor checks and fixes its own work
# for up to PI_SELF_REVIEW_SECONDS (config), stopping early after 2 fix rounds without progress.
SELF_FIX_REASONING = "high"  # fix rounds and final repairs (max doubled reasoning output)
MAX_SELF_CONTINUATIONS = 2
GUARD_SUITE_SECONDS = 75
# A hung request (no reply at all) times out at 300 s in the engine and is retried inside this window.
# A rate limit (HTTP 429) can last minutes: wait longer before each further try.
NETWORK_RETRY_WAITS = (30, 60, 120, 240)
TRANSIENT_ERROR_RE = re.compile(
    r"^network error|proxy_error|connection reset|reset by peer|upstream|ECONNRESET|socket hang up"
    r"|invalid tool_call arguments|HTTP 429|HTTP 5\d\d|rate limit|too many requests", re.I)
NETWORK_RETRY_PROMPT = (
    "The previous request failed before the model answered (network error or malformed reply). "
    "Carry out the most recent instruction above now."
)
MAX_BATCH_FOLLOW_UPS = 8
MAX_BUILD_FIX_GRACE = 2
MAX_REPAIR_CONTINUATIONS = 2
REPAIR_CONTINUE_PROMPT = (
    "You stopped before changing any file. You have enough context now: edit the source to fix "
    "the failing assertions listed above (exact accessible names, roles, text and navigation). "
    "Write the fix first; read another file only if the edit needs it."
)
_SOURCE_EXCLUDED_DIRS = {
    "node_modules",
    "dist",
    "dist-ssr",
    "coverage",
    "test-results",
    ".git",
    ".arc",
}


# helpers


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{secrets.token_hex(2)}"


def _result(run_id: str, output_dir: Path) -> dict:
    return {
        "outcome": "failed",
        "exitCode": 1,
        "reason": "",
        "runId": run_id,
        "outputDir": str(output_dir),
        "evidenceDir": None,
        "metricsPath": None,
        "verificationPath": None,
        "verification": None,
        "progress": None,
        "metrics": None,
    }


def _failed(base: dict, reason: str) -> dict:
    return {**base, "outcome": "failed", "exitCode": 1, "reason": reason}


def _progress_log(output: Path, message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[arc_pi {stamp}] {redact_secrets(message)}"
    try:
        print(line, flush=True)
    except (OSError, UnicodeError):
        pass
    try:
        log_path = output / ".arc" / "debug.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _first_line(text: str) -> str:
    return next((part.strip() for part in str(text or "").splitlines() if part.strip()), "")


def _log_verification(output: Path, label: str, report: dict) -> None:
    """Log a verification summary followed by each observed failure."""
    _progress_log(
        output,
        f"{label}: build={report.get('build')} startup={report.get('startup')} "
        f"acceptance={report.get('acceptance')} "
        f"passed={report.get('passed', 0)}/{report.get('total', 0)}",
    )
    for line in _failure_lines(report, MAX_REPAIR_FAILURES):
        _progress_log(output, f"  {line}")


def _guard_output(output_dir: Path, requirement_path: Path, project_dir: Path) -> str | None:
    """Reject output paths that could overwrite or sit inside protected trees."""
    output = output_dir.resolve()
    project = project_dir.resolve()
    req_path = requirement_path.resolve()
    req_dir = req_path if req_path.is_dir() else req_path.parent
    if output == project or output == req_dir:
        return f"output directory {output} is a protected harness/input directory"
    if project.is_relative_to(output):
        return f"output directory {output} would contain the harness sources"
    if req_dir.is_relative_to(output):
        return f"output directory {output} would contain the immutable requirement input"
    if output.is_relative_to(req_dir):
        return f"output directory {output} is inside the requirement input directory"
    if output.is_relative_to(project):
        relative = output.relative_to(project)
        if not relative.parts or relative.parts[0] not in ("runs", "output"):
            return (
                f"output directory {output} is inside the harness sources; "
                "only project-local runs/ and output/ are allowed"
            )
    return None


def _scaffold(output_dir: Path, project_dir: Path) -> list[str]:
    """Copy template frontend/backend only when entirely missing; never overlay."""
    copied: list[str] = []
    for name in ("frontend", "backend"):
        source = project_dir / "template" / name
        target = output_dir / name
        if target.exists():
            continue
        shutil.copytree(source, target)
        copied.append(name)
    return copied


MAP_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css", ".html", ".json")
MAP_MAX_CHARS = 20000
MAP_LINE_CHARS = 260
EXPORT_RE = re.compile(r"export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|const|let|class)\s+([A-Za-z_$][\w$]*)"
                       r"|exports\.([A-Za-z_$][\w$]*)\s*=")
MODULE_EXPORTS_RE = re.compile(r"module\.exports\s*=\s*\{([^}]{0,600})\}")
API_ROUTE_RE = re.compile(r"\.(get|post|put|patch|delete)\(\s*['\"`](/[^'\"`]{0,80})")
PAGE_ROUTE_RE = re.compile(r"\bpath\s*[=:]\s*\{?\s*['\"`](/[^'\"`]{0,60})")


def _file_listing(output: Path) -> str:
    """Project map for the prompts: every backend and frontend source file with its length, exported
    names, and the API and page routes it defines; read from the source each time, so it is complete
    and current without a model call."""
    lines: list[str] = []
    for sub in ("backend", "frontend"):
        root = output / sub
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            parts = path.relative_to(output).parts
            if (not path.is_file() or {"node_modules", "dist", ".git", ".arc"} & set(parts)
                    or path.suffix.lower() not in MAP_SUFFIXES or path.name.startswith("package")
                    or (path.suffix.lower() == ".json" and "data" in parts)):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            names = [first or second for first, second in EXPORT_RE.findall(text)]
            for block in MODULE_EXPORTS_RE.findall(text):
                names += [item.split(":")[0].strip() for item in block.split(",") if item.strip()]
            info = []
            if names:
                info.append("exports " + ", ".join(dict.fromkeys(names)))
            api = [f"{method.upper()} {route}" for method, route in API_ROUTE_RE.findall(text)]
            if api:
                info.append("api " + ", ".join(dict.fromkeys(api)))
            pages = PAGE_ROUTE_RE.findall(text) if sub == "frontend" else []
            if pages:
                info.append("routes " + ", ".join(dict.fromkeys(pages)))
            line = f"{'/'.join(parts)} ({text.count(chr(10)) + 1} lines)" + (": " + "; ".join(info) if info else "")
            lines.append(line if len(line) <= MAP_LINE_CHARS else line[:MAP_LINE_CHARS - 3] + "...")
    listing = "\n".join(lines)
    if len(listing) > MAP_MAX_CHARS:
        listing = listing[:MAP_MAX_CHARS].rsplit("\n", 1)[0] + "\n... (map truncated)"
    return listing


def _source_map(root: Path) -> dict[str, str]:
    """Relative path -> content hash for every source file in the app."""
    files: dict[str, str] = {}
    for sub in ("frontend", "backend"):
        base = root / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            parts = path.relative_to(root).parts
            if any(part in _SOURCE_EXCLUDED_DIRS for part in parts):
                continue
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _changed_paths(before: dict[str, str], root: Path) -> list[str]:
    after = _source_map(root)
    changed = sorted(rel for rel in set(before) | set(after) if before.get(rel) != after.get(rel))
    return changed


def _source_state(root: Path) -> tuple:
    parts = []
    for sub in ("frontend", "backend"):
        path = root / sub
        if path.is_dir():
            parts.append(source_hash(path))
    return tuple(parts)


def _prompt_deadline(task_deadline: float, seconds: float) -> float:
    """Per-prompt watchdog: one prompt (all its model steps) may not run longer than this;
    0 = no per-prompt limit (only the run deadline; each model request still times out)."""
    return task_deadline if seconds <= 0 else min(task_deadline, time.monotonic() + seconds)


def _run_tokens(bridge: PiBridge) -> int:
    totals = bridge.totals or {}
    return int(totals.get("inputTokens", 0) or 0) + int(totals.get("outputTokens", 0) or 0)


def _run_cost_cny(bridge: PiBridge, config: Config) -> float:
    """Estimated model cost so far; providers that report no cache hits are priced as uncached."""
    totals = bridge.totals or {}
    cached = int(totals.get("cacheReadTokens", 0) or 0)
    uncached = max(0, int(totals.get("inputTokens", 0) or 0) - cached)
    output = int(totals.get("outputTokens", 0) or 0)
    return (uncached * config.pi_price_input + cached * config.pi_price_cache
            + output * config.pi_price_output) / 1_000_000


def _can_dispatch(bridge: PiBridge, config: Config, deadline: float) -> bool:
    if time.monotonic() >= deadline:
        return False
    if config.pi_max_run_tokens and _run_tokens(bridge) >= config.pi_max_run_tokens:
        return False  # the run's token cap
    if config.pi_max_run_cost_cny and _run_cost_cny(bridge, config) >= config.pi_max_run_cost_cny:
        return False  # the run's cost cap (CNY)
    return bridge.totals.get("attempts", 0) < config.pi_max_model_calls


def _is_network_failure(response: dict) -> bool:
    return (response.get("stopReason") == "error"
            and response.get("errorCode") == "provider"
            and TRANSIENT_ERROR_RE.search(str(response.get("errorMessage") or "")) is not None)


def _prompt(bridge: PiBridge, config: Config, text: str, deadline: float, output: Path,
            reasoning: str | None = None) -> dict:
    extra = {} if reasoning is None else {"reasoning": reasoning}
    try:
        raw = bridge.prompt(text, _prompt_deadline(deadline, config.pi_prompt_seconds), **extra)
    except BridgeError as exc:
        # the engine process died (e.g. a tool command killed it): start a fresh one and report
        # this prompt as an error, so a built app is never lost to an engine crash
        _progress_log(output, f"engine lost ({_first_line(str(exc))}); starting a fresh engine")
        bridge.restart()
        return {"stopReason": "error", "errorCode": "bridge", "summary": "",
                "errorMessage": str(exc)}
    response = _relabel_watchdog_abort(raw, bridge, config, deadline)
    for wait in NETWORK_RETRY_WAITS:
        if not _is_network_failure(response):
            return response
        if deadline - time.monotonic() < wait + config.pi_prompt_seconds or not _can_dispatch(bridge, config, deadline):
            return response
        _progress_log(output, f"model {response['errorMessage']}; retrying in {wait}s")
        time.sleep(wait)
        response = _relabel_watchdog_abort(
            bridge.prompt(NETWORK_RETRY_PROMPT, _prompt_deadline(deadline, config.pi_prompt_seconds), **extra),
            bridge, config, deadline)
    return response



def _stop_reason(response: dict) -> str:
    """A reply cut at the output limit inside a tool call arrives as a provider error about invalid
    tool_call JSON; continue it like a cut reply instead of dropping the whole batch."""
    stop = response.get("stopReason") or "stop"
    message = str(response.get("errorMessage") or "")
    if stop == "error" and ("invalid tool_call arguments" in message
                            or "length-truncated tool call" in message):
        return "length"
    if stop == "error" and "unknown tool" in message:
        return "bad_tool"  # the session holds an unusable call: restart it and resend the batch
    return stop


def _relabel_watchdog_abort(response: dict, bridge: PiBridge, config: Config, deadline: float) -> dict:
    # "budget"/"cancelled" also mean the per-prompt deadline hit; only the run deadline/call cap is final.
    if response.get("stopReason") in ("budget", "cancelled") and _can_dispatch(bridge, config, deadline):
        return {**response, "stopReason": "prompt_timeout"}
    return response


def _batch_build(output: Path, report_dir: Path, web_port: int, run_id: str) -> tuple[str, str]:
    report = verify_application(
        output_dir=output,
        tests_dir=None,
        report_dir=report_dir,
        deadline=time.monotonic() + VERIFY_WATCHDOG_SECONDS,
        grading_port=web_port,
        build_only=True,
        start_backend=True,
        command="generate",
        run_id=run_id,
    )
    if report.get("build") == "passed" and report.get("startup") == "failed":
        # A backend that crashes on start fails every scenario: fix it like a build error.
        return "failed", f"backend failed to start: {report.get('error') or ''}"
    return report.get("build", "not_run"), report.get("error") or ""


FINAL_SPECS_PROMPT = (
    "## Scenario tests for final verification\n"
    "These requirement nodes have no scenario test yet, so the final check cannot see what fails: "
    "{ids}. Write `review-tests/<requirement id>.spec.js` for each of them (start with the ones a "
    "user reaches first), one `test()` per scenario titled exactly with the scenario name; assert "
    "every THEN step, including each quoted message and the state after a reload. Write them "
    "as user-level scenario tests: `const h = require('./helpers');`, start each test with "
    "`await h.openHome(page);` (the only navigation), then click and fill only by the names the "
    "requirement quotes (the helpers in `helpers.js` and `getByRole`). "
    "Follow the scenario's path literally: when it says the user opens a page or record (\"opens "
    "the Reports page\", \"opens that record\", \"opens `Summary`\") without naming the links on "
    "the way, click a control with exactly that name right on the page the scenario starts on "
    "(home, after sign-in if it signs in); never first open a parent page or any other page the "
    "scenario does not name, so the test fails when the app lacks that link; sign "
    "in through the form with the seed credentials; create the data a scenario enters with "
    "`h.unique()`; never call page.goto, fetch or /api/, never use CSS/id selectors or "
    "absolute URLs (the suite runs on another port); you may add functions to `helpers.js` but "
    "keep `openHome` as `page.goto('/')`; never use "
    "`.first()`/`.nth()` to dodge duplicates. Write one `test()` call with a literal title per "
    "scenario (no loops over titles). Assert each THEN step on the page. Write the files "
    "now (several per reply); do not change application code in this step."
)
FINAL_SPECS_MIN_SECONDS = 1500  # spec writing plus the full suite and one repair must still fit


TESTS_DIR_NAME = "review-tests"


def _freeze_specs(tests_dir: Path) -> dict[str, bytes]:
    """Repairs must fix the app, not rewrite the specs that measure it."""
    if not tests_dir.is_dir():
        return {}
    return {path.relative_to(tests_dir).as_posix(): path.read_bytes()
            for path in tests_dir.rglob("*") if path.is_file() and "node_modules" not in path.parts}


def _restore_specs(tests_dir: Path, frozen: dict[str, bytes]) -> None:
    if not tests_dir.is_dir():
        return
    for path in list(tests_dir.rglob("*")):
        if (path.is_file() and "node_modules" not in path.parts
                and path.relative_to(tests_dir).as_posix() not in frozen):
            path.unlink()
    for name, data in frozen.items():
        target = tests_dir / name
        if not target.is_file() or target.read_bytes() != data:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def _missing_scenarios(tests_dir: Path, nodes: list[dict]) -> list[str]:
    """Scenario names of these nodes that no test in the node's spec file is titled with."""
    missing: list[str] = []
    for node in nodes:
        spec = tests_dir / f"{node.get('id')}.spec.js"
        text = spec.read_text(encoding="utf-8", errors="replace") if spec.is_file() else ""
        titles = {match.group(2).strip() for match in TEST_TITLE_RE.finditer(text)}
        missing += [name for name in (str(scenario.get("name") or "").strip()
                                      for scenario in node.get("scenarios") or []
                                      if isinstance(scenario, dict))
                    if name and name not in titles]
    return missing


def _stale_spec_ids(tests_dir: Path, nodes: list[dict]) -> list[str]:
    """Nodes whose own spec was written for an earlier version of the requirement: a current
    scenario name is no longer one of the spec's test titles."""
    return [str(node.get("id")) for node in nodes
            if node.get("id") and (tests_dir / f"{node.get('id')}.spec.js").is_file()
            and _missing_scenarios(tests_dir, [node])]


def _set_aside_stale_specs(tests_dir: Path, nodes: list[dict], target: Path) -> list[str]:
    """Specs of changed requirements check the old behaviour: move them out of the suite so
    batches, final verification and repairs measure the current text (they are rewritten)."""
    stale = _stale_spec_ids(tests_dir, nodes)
    if stale:
        target.mkdir(parents=True, exist_ok=True)
        for node_id in stale:
            shutil.move(str(tests_dir / f"{node_id}.spec.js"), str(target / f"{node_id}.spec.js"))
    return stale


def _ship_clean_data(output: Path, nodes: list[dict], report_dir: Path, web_port: int,
                     run_id: str, set_aside: Path | None = None) -> str:
    """Smoke runs pollute the shipped data file (test records, changed seed passwords): remove it
    (or move it to ``set_aside``) when the app builds, boots and seeds itself without it."""
    files = data_files(output)
    if not files:
        return "no runtime data files"
    only_in_data = seeds_only_in_data(output, nodes)
    if only_in_data:
        return f"kept: seed records exist only in the data ({', '.join(only_in_data[:5])})"
    with tempfile.TemporaryDirectory(prefix="arc-clean-data-", ignore_cleanup_errors=True) as scratch:
        trial = Path(scratch)
        for name in ("frontend", "backend"):
            shutil.copytree(output / name, trial / name, symlinks=True,
                            ignore=shutil.ignore_patterns("node_modules", "dist"))
        for path in files:
            (trial / path.relative_to(output)).unlink(missing_ok=True)
        status, error = _batch_build(trial, report_dir, web_port, run_id)
    if status != "passed":
        return f"kept: the app does not start without its data ({_first_line(error)[:200]})"
    for path in files:
        if set_aside is None:
            path.unlink(missing_ok=True)
            continue
        target = set_aside / path.relative_to(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
    verb = "removed" if set_aside is None else "set aside"
    return f"{verb} {len(files)} runtime data file(s); the app seeds itself at startup"


def _regression_check(output: Path, tests_dir: Path, guard: set, report_dir: Path,
                      web_port: int, run_id: str, leaf_ids: list[str]) -> list[dict]:
    """Re-run only the reviewer specs that passed before; return guarded tests that now fail."""
    with tempfile.TemporaryDirectory(prefix="arc-guard-") as scratch:
        suite = Path(scratch)
        for name in sorted({file for file, _ in guard} | {TEST_HELPERS}):
            if (tests_dir / name).is_file():
                shutil.copy2(tests_dir / name, suite / name)
        report = verify_application(
            output_dir=output, tests_dir=suite, report_dir=report_dir,
            deadline=time.monotonic() + GUARD_VERIFY_SECONDS, grading_port=web_port,
            known_requirement_ids=set(leaf_ids), suite_timeout_seconds=GUARD_SUITE_SECONDS,
            command="generate", run_id=run_id)
    if report.get("startup") != "passed":
        return []  # build/startup failures already reach the agent through _batch_build
    return [row for row in report.get("tests") or []
            if (row.get("file"), row.get("title")) in guard and row.get("status") != "passed"]


def _regression_prompt(batch_index: int, broken: list[dict]) -> str:
    lines = [f"Batch {batch_index} broke scenario(s) that passed before this batch. Restore them now "
             "without undoing this batch's new work (exact names, roles, routes and stored data):"]
    for row in broken[:MAX_REPAIR_FAILURES]:
        entry = f"- {row.get('file', '')} :: {row.get('title', '')}"
        observed = _informative_error_lines(str(row.get("error") or ""))
        if observed:
            entry += "\n  observed: " + "\n  ".join(observed)
        snapshot = str(row.get("pageSnapshot") or "").strip()
        if snapshot:
            entry += "\n  page at failure: " + " | ".join(snapshot.splitlines())[:400]
        lines.append(entry)
    return "\n".join(lines)


BAD_TOOL_HINT = (
    "Your previous reply called a tool that does not exist, so it was rejected and nothing was "
    "saved. Use only the tools you are given, one call at a time. Start this batch again:\n\n"
)
LENGTH_HINT = (
    "It hit the output limit, most likely in the middle of one very large file, so that file "
    "was not saved. Write one file per tool call and keep each file short (about 200 lines at "
    "most): split a large page or module into several smaller components and helpers. "
)


def _continuation_prompt(batch_index: int, stop_reason: str, output: Path) -> str:
    """Short follow-up: the batch's requirements are already in the transcript."""
    return (
        f"Batch {batch_index}: your previous response was cut off ({stop_reason}). "
        + (LENGTH_HINT if stop_reason == "length" else "")
        + "Continue the unfinished work for this batch's requirements now. Make sure every route, "
        "import and component you referenced exists. Do not redo finished work.\n\n"
        "## Current application source files\n" + _file_listing(output)
    )


def _build_fix_prompt(batch_index: int, build_error: str, output: Path, stop_reason: str = "stop") -> str:
    cut_off = (
        f"Your previous response was cut off ({stop_reason}); finish this batch's unfinished work too.\n\n"
        if stop_reason != "stop" else ""
    )
    return (
        f"Batch {batch_index}: the application build now fails:\n\n{build_error.strip()[:1500]}\n\n"
        f"{cut_off}"
        "Fix every listed cause first. For a missing module, write that file now with its real "
        "implementation (never an empty placeholder); for a wrong path, correct the import "
        "specifier. Always write an imported file in the same reply as, or before, the file that "
        "imports it. Do not remove required functionality.\n\n"
        "## Current application source files\n" + _file_listing(output)
    )


def _suite_timeout(deadline: float) -> float:
    """Playwright global timeout that keeps an acceptance run inside the run deadline."""
    return max(30.0, min(FINAL_SUITE_SECONDS, deadline - time.monotonic() - 60.0))


NOTE_HEADING = "## Project state from before your context reset\n"
MAX_NOTE_CHARS = 12000


def _next_batch_scan(group: list[dict]) -> str:
    """Handoff addition: the outgoing session, which knows the code, maps the next batch's
    requirements onto it so the fresh session starts in the right files."""
    lines = [f"- {node.get('id')}: {node.get('name')}: "
             + " ".join(str(node.get("description") or "").split())[:300] for node in group]
    return ("\n\nThe next batch implements these requirements:\n" + "\n".join(lines)
            + "\nEnd the handoff with a section NEXT BATCH (at most 20 more lines): for each id above, the "
              "existing files, routes, API endpoints and seed records it builds on or must change, and "
              "the exact quoted names, roles and messages from its text that are easy to miss.")


def _batch_preamble(build_error: str, review_notes: str, compact_notes: str = "",
                    contracts: str = "", error_log: str = "", changed: list[str] = ()) -> str:
    """Context reset notes, section contracts, carried build error and reviewer notes."""
    parts: list[str] = []
    if compact_notes:
        parts.append(NOTE_HEADING + compact_notes.strip()[:MAX_NOTE_CHARS])
    if changed:
        parts.append(
            "## Changed requirements in this batch\n"
            f"The existing app implements an earlier version of {', '.join(changed)}: their current "
            "scenarios differ from the ones its old tests checked (those tests were removed). Read the "
            "current text of these nodes and change the app wherever it differs (names, messages, "
            "validation, seeded values, flows); keep the rest of the app working.")
    if contracts:
        parts.append(contracts)
    if build_error:
        parts.append(
            "## The application build currently FAILS (left over from the previous batch)\n"
            f"{build_error.strip()[:1500]}\n\n"
            "Fix this first, in your first reply, then implement this batch.")
    if review_notes:
        parts.append(
            "## Notes from the reviewer (its fixes are already in the files)\n"
            f"{review_notes.strip()[:3000]}")
    if error_log:
        parts.append("## Project error log (`.arc/error-log.md`): errors you or the reviewer hit "
                     "earlier; keep them fixed and do not repeat them\n" + error_log)
    return "\n\n".join(parts)


def _can_repair_dispatch(bridge: PiBridge, config: Config, deadline: float) -> bool:
    return time.monotonic() < deadline


def _needs_repair(report: dict) -> bool:
    return (report.get("build") == "failed" or report.get("startup") == "failed"
            or report.get("acceptance") == "failed" or bool(report.get("serverErrors")))


def _repair_accepted(best: dict, candidate: dict) -> bool:
    """Keep a candidate that passes more tests (or equal tests with fewer backend errors)."""
    if best.get("build") == "failed" and candidate.get("build") == "passed":
        return True
    if best.get("startup") == "failed" and candidate.get("startup") == "passed":
        return True
    best_passed = {t.get("id") for t in best.get("tests") or [] if t.get("status") == "passed"}
    candidate_passed = {t.get("id") for t in candidate.get("tests") or [] if t.get("status") == "passed"}
    if len(candidate_passed) <= len(best_passed):
        # Same tests still pass and the backend logs fewer errors: a real fix.
        return (best_passed <= candidate_passed
                and len(candidate.get("serverErrors") or []) < len(best.get("serverErrors") or []))
    return True  # net gain counts: e.g. 2 -> 5 passing was reverted when one old pass flipped


def _repair_can_continue(best: dict, candidate: dict) -> bool:
    if any(report.get(stage) != "passed"
           for report in (best, candidate) for stage in ("build", "startup")):
        return False
    best_tests = {test.get("id"): test.get("status") for test in best.get("tests") or []}
    candidate_tests = {test.get("id"): test.get("status") for test in candidate.get("tests") or []}
    return (
        bool(best_tests)
        and None not in best_tests
        and best_tests.keys() == candidate_tests.keys()
        and candidate.get("acceptance") == "failed"
        and all(candidate_tests[test_id] == "passed"
                for test_id, status in best_tests.items() if status == "passed")
        and not any(status in ("skipped", "not_run", "interrupted")
                    for status in candidate_tests.values())
    )


def _failure_lines(report: dict, limit: int) -> list[str]:
    lines: list[str] = []
    if report.get("build") == "failed":
        lines.append(f"BUILD failed: {report.get('error') or 'frontend build failed'}")
    if report.get("startup") == "failed":
        lines.append(f"STARTUP failed: {report.get('error') or 'backend not HTTP-ready'}")
    home = report.get("homePage") or {}
    if home.get("blank") or home.get("pageErrors"):
        detail = "; ".join(home.get("pageErrors") or []) or "no uncaught error"
        if home.get("blank") and home.get("consoleErrors"):
            detail += "; console: " + "; ".join(home["consoleErrors"])
        state = "renders nothing" if home.get("blank") else "throws in the browser"
        lines.append(f"HOME PAGE {state} (fix this first; every test starts there): {detail[:600]}")
    errors = report.get("serverErrors") or []
    if errors:
        lines.append("SERVER ERRORS logged by the backend during the checks (fix these first; "
                     "each breaks every request that reaches it): " + "; ".join(errors)[:900])
    if report.get("acceptance") == "failed":
        for test in report.get("tests") or []:
            status = test.get("status")
            if status in ("passed", "skipped"):
                continue
            error = str(test.get("error") or "").strip()
            entry = f"TEST {test.get('file', '')} :: {test.get('title', '')} ({status})"
            observed = _informative_error_lines(error)
            if observed:
                entry += "\n  observed: " + "\n  ".join(observed)
            snapshot = str(test.get("pageSnapshot") or "").strip()
            if snapshot:
                entry += "\n  page at failure: " + " | ".join(snapshot.splitlines())[:600]
            lines.append(entry)
        if report.get("error"):
            lines.append(f"ACCEPTANCE: {report['error']}")
    seen: set[str] = set()
    deduped: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        deduped.append(line)
        if len(deduped) >= limit:
            break
    return deduped


_ERROR_NOISE = re.compile(
    r"^(Call log:|- Expect \"|- waiting for |at |attachment #|Error Context:|Usage:|npx playwright|─|\d+ \||\|)"
)


def _informative_error_lines(error: str) -> list[str]:
    """Drop Playwright boilerplate so the assertion details fit the digest."""
    parts = [line.strip() for line in error.splitlines() if line.strip()]
    if len(parts) > 1 and parts[0].startswith("Test timeout of"):
        parts = parts[1:]
    kept: list[str] = []
    total = 0
    for part in parts:
        if _ERROR_NOISE.match(part):
            continue
        part = part[:200]
        if len(kept) >= 8 or total + len(part) > 1000:
            break
        kept.append(part)
        total += len(part)
    return kept


def _record_test_events(runtime: AgentRuntime, report: dict, known_ids: list[str]) -> None:
    """Mirror mapped results into events and traceability; unmapped rows stay private."""
    known = set(known_ids)
    for row in report.get("tests") or []:
        test_id = row.get("id")
        req_id = row.get("requirementId")
        file_path = row.get("file") or ""
        title = row.get("title") or ""
        status = row.get("status")
        if not test_id or not req_id or req_id not in known:
            continue
        runtime.traceability.upsert_test(
            test_id=str(test_id),
            req_id=str(req_id),
            type="playwright",
            file_path=file_path,
            first_line="1",
            passed=status == "passed",
        )
        message = f"{file_path} :: {title}" if file_path else str(test_id)
        if status == "passed":
            runtime.events.mark_test_passed(req_id, message)
        else:
            runtime.events.mark_test_failed(req_id, f"{message} [{status}]")


def _build_metrics(run_id: str, config: Config, started: float, totals: dict, report: dict | None) -> dict:
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "model": config.model,
        "elapsedMs": int((time.monotonic() - started) * 1000),
        "attempts": totals.get("attempts", 0),
        "tokens": {
            "input": totals.get("inputTokens", 0),
            "output": totals.get("outputTokens", 0),
            "cacheRead": totals.get("cacheReadTokens", 0),
            "cacheWrite": totals.get("cacheWriteTokens", 0),
            "reasoning": totals.get("reasoningTokens", 0),
            "estimated": totals.get("estimatedTokens", 0),
        },
        "usageComplete": totals.get("usageComplete", False),
        "cost": totals.get("cost"),
        "currency": totals.get("currency"),
        "config": {
            "timeBudgetSeconds": config.pi_time_budget_seconds,
            "contextBytes": config.pi_context_bytes,
            "repairRounds": config.pi_repair_rounds,
            "reasoning": config.pi_reasoning,
        },
        "verification": {
            "status": (report or {}).get("acceptance"),
            "passed": (report or {}).get("passed", 0),
            "total": (report or {}).get("total", 0),
        },
    }


# run_task


def run_task(config: Config, requirement_path, output_dir, web_port: int) -> dict:
    started = time.monotonic()
    deadline = started + config.pi_time_budget_seconds
    final_review_seconds, final_reserve_seconds = _final_phase_seconds(config.pi_time_budget_seconds,
                                                                    config.pi_final_phase_seconds)
    reserve_start = max(started, deadline - final_reserve_seconds)
    # Batch prompts stop shortly after the reserve starts so final verification keeps its time.
    batch_until = min(deadline, reserve_start + BATCH_OVERRUN_SECONDS)
    run_id = _new_run_id()
    output = Path(output_dir)
    base = _result(run_id, output)

    # 1. Requirement tree: validated and ordered before anything else.
    requirement_path = Path(requirement_path)
    try:
        tree = load_requirement_tree(requirement_path)
        validate_requirement_tree(tree)
        ordered = topo_order(tree)
    except RequirementError as exc:
        return _failed(base, f"invalid requirement tree: {exc}")
    except OSError as exc:
        return _failed(base, f"cannot read requirements at {requirement_path}: {exc}")
    leaf_ids = [str(node.get("id") or "") for node in ordered]
    earlier_stage_ids = external_dependencies(tree)
    requirement_dir = requirement_path if requirement_path.is_dir() else requirement_path.parent

    # 2. Output guards: never build inside or over protected trees.
    guard_error = _guard_output(output, requirement_path, config.project_dir)
    if guard_error:
        return _failed(base, guard_error)

    if not config.openai_api_key.strip():
        return {
            **base, "outcome": "failed", "exitCode": 0,
            "reason": ("OPENAI_API_KEY is empty; fill "
                       f"{config.project_dir / '.env'} with your model gateway "
                       "gateway key and confirm MODEL is permitted"),
        }

    # 4. Harness assets: the always-loaded skill and both template halves.
    web_skill = config.project_dir / "skills" / "web-implementation" / "SKILL.md"
    if not web_skill.is_file():
        return _failed(base, f"harness asset missing: {web_skill}")
    for name in ("frontend", "backend"):
        if not (config.project_dir / "template" / name).is_dir():
            return _failed(base, f"harness asset missing: template/{name}")

    # 5. Scaffold (copy only entirely missing frontend/backend directories).
    try:
        output.mkdir(parents=True, exist_ok=True)
        _scaffold(output, config.project_dir)
    except OSError as exc:
        return _failed(base, f"cannot prepare output directory {output}: {exc}")

    # 6. Evidence plumbing and platform SDK.
    evidence_dir = output / ".arc" / "arc_pi" / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    runtime = AgentRuntime.from_env(project_dir=str(output))
    error_log = ProjectErrorLog(output / ".arc" / "error-log.md")
    runtime.events.mark_run_started(f"model={config.model}")
    if earlier_stage_ids:
        _progress_log(output, f"{len(earlier_stage_ids)} dependency id(s) are not in this requirement "
                              f"file (earlier stage, already in the starting app): "
                              f"{', '.join(earlier_stage_ids[:12])}")
    runtime.traceability.store_requirement_tree(tree)
    _progress_log(output, f"run {run_id} started: model={config.model}, "
                          f"{len(leaf_ids)} requirement(s): {', '.join(leaf_ids)}")
    changed_ids = set(_set_aside_stale_specs(output / TESTS_DIR_NAME, ordered,
                                             evidence_dir / "changed-requirement-specs"))
    if changed_ids:
        _progress_log(output, f"{len(changed_ids)} requirement(s) changed since the starting app's specs "
                              f"were written; old specs set aside: {', '.join(sorted(changed_ids))}")
    if data_files(output):
        # data the starting app wrote while it ran earlier (other users' records, changed seeds):
        # start from the app's own seeding, like the shipped app
        _progress_log(output, "starting app runtime data: " + _ship_clean_data(
            output, ordered, evidence_dir / "verify-start-data", web_port, run_id,
            set_aside=evidence_dir / "starting-app-runtime-data"))
    try:
        runtime.git.ensure_repo(create_initial_commit=False)
    except Exception as exc:  # git unavailable is fatal before any paid work
        runtime.events.mark_run_failed(f"git unavailable: {exc}")
        _progress_log(output, f"FAILED: git unavailable: {exc}")
        return {**_failed(base, f"git unavailable: {exc}"), "evidenceDir": str(evidence_dir)}

    progress: dict = {
        "schemaVersion": 1,
        "runId": run_id,
        "outcome": "failed",
        "attemptedLeafIds": [],
        "incompleteLeafIds": [],
        "skills": {"selected": [], "absent": [], "paths": {}},
        "batches": [],
        "checks": [],
        "unresolved": [],
    }
    attempted: set[str] = set()
    incomplete: set[str] = set()
    absent_skills: set[str] = set()
    selected_skills: set[str] = set()
    skills_paths: dict[str, str] = {}
    outcome = "failed"
    exit_code = 1
    reason = "no verification performed"
    best_report: dict | None = None
    best_report_dir: Path | None = None
    fatal_error: Exception | None = None
    bridge: PiBridge | None = None
    any_source_changes = False
    nudge_used = False
    budget_blocked = False
    provisional_repair = False
    carried_build_error = ""
    review_notes = ""
    reviewer: BatchReviewer | None = None
    blind: BlindTester | None = None

    try:
        folder_map = top_folder_of_leaves(tree)
        batches = fit_batches(group_batches(ordered, folder_map),
                              int((reserve_start - started) // config.pi_seconds_per_batch))
        summary = (
            f"Requirement tree: {len(ordered)} atomic nodes in {len(batches)} batch(es); "
            f"ordered execution ids: {', '.join(leaf_ids)}."
        )
        always_skill, _ = skill_text(config.project_dir / "skills", [ALWAYS_SKILL])
        system_prompt = "\n\n".join(part for part in (
            SYSTEM_PREFIX, summary, always_skill, root_contract(tree)) if part)
        tests_dir: Path | None = None
        read_roots = [requirement_dir]

        bridge = PiBridge(evidence_dir=evidence_dir)
        bridge.start(
            config=config,
            workspace=output,
            read_roots=read_roots,
            system_prompt=system_prompt,
            deadline=deadline,
        )
        if config.pi_reviewer:
            reviewer = BatchReviewer(
                config=config, output=output, evidence_dir=evidence_dir,
                requirement_dir=requirement_dir, deadline=deadline - FINAL_VERIFY_SECONDS,
                bridge_factory=PiBridge, file_listing=_file_listing, root_text=root_contract(tree),
                log=lambda message: _progress_log(output, message), error_log=error_log)
        self_review = (SelfReview(config=config, output=output, evidence_dir=evidence_dir,
                                  requirement_dir=requirement_dir, file_listing=_file_listing,
                                  log=lambda message: _progress_log(output, message),
                                  leaf_ids=leaf_ids, error_log=error_log,
                                  named_ui=ui_name_templates(list(node_index(tree).values())),
                                  clash_check=not earlier_stage_ids)
                       if config.pi_self_review and not config.pi_reviewer else None)
        fix_level = [SELF_FIX_REASONING]
        if config.pi_blind_test:
            # runs in parallel with the batches; must be done when final verification starts
            blind = BlindTester(config=config, requirement_dir=requirement_dir,
                                workdir=evidence_dir / BLIND_DIR, evidence_dir=evidence_dir,
                                log=lambda message: _progress_log(output, message))
            blind.start(batches, deadline - (final_reserve_seconds - final_review_seconds))
            _progress_log(output, "blind tests: writer started in parallel")

        def self_converse(text: str, fixing: bool, until: float) -> str:
            """One self-review prompt (+continuations): configured level to write, max to fix."""
            level = fix_level[0] if fixing else config.pi_reasoning
            summaries: list[str] = []
            prompt = text
            for _ in range(1 + MAX_SELF_CONTINUATIONS):
                if time.monotonic() >= until or not _can_dispatch(bridge, config, deadline):
                    break
                before = _source_state(output)
                response = _prompt(bridge, config, prompt, until, output, reasoning=level)
                summaries.append(str(response.get("summary") or ""))
                stop = _stop_reason(response)
                if (level == "max" and not summaries[-1].strip() and _source_state(output) == before
                        and stop in ("prompt_timeout", "budget", "cancelled")):
                    fix_level[0] = level = "high"
                    _progress_log(output, "self-review: max reply timed out empty; fixing at high")
                if stop not in ("turn_limit", "length", "prompt_timeout"):
                    break
                prompt = ("Continue and finish this now: the fixes, and any Playwright spec files for "
                          "these nodes still missing under review-tests/.")
            return "\n".join(part for part in summaries if part.strip())

        guard: set[tuple[str, str]] = set()  # (spec file, test title) that passed in a reviewer check

        def merge_review() -> None:
            nonlocal carried_build_error, review_notes
            merged = reviewer.merge_ready() if reviewer is not None else None
            if merged is None:
                return
            progress["checks"].append({"type": "review", "applied": merged.applied,
                                       "conflicts": merged.conflicts})
            sections = [review_notes, merged.notes]
            if merged.conflicts:
                sections.append("Reviewer fixes NOT applied because you changed these files "
                                "meanwhile; re-check them yourself: " + ", ".join(merged.conflicts))
            review_notes = "\n\n".join(part for part in sections if part)
            _progress_log(output, f"reviewer merged: applied {len(merged.applied)} file(s), "
                                  f"conflicts {len(merged.conflicts)}")
            if merged.test_rows:
                _record_test_events(runtime, {"tests": merged.test_rows}, leaf_ids)
                guard.update((row.get("file"), row.get("title")) for row in merged.test_rows
                             if row.get("status") == "passed" and row.get("file"))
            if not merged.applied:
                return
            status, error = _batch_build(
                output, evidence_dir / f"verify-review-{len(progress['checks']):03d}", web_port, run_id)
            carried_build_error = error if status == "failed" else ""
            if status == "passed":
                for node_id in merged.fixed_build_ids:
                    runtime.events.mark_implementation_done(
                        node_id, "reviewer fixed the build; acceptance pending")
            runtime.git.commit("arc_pi reviewer fixes")

        # 7. Ordered batches: one persistent agent, folder-grouped, bounded.
        for batch_index, group in enumerate(batches, start=1):
            group_ids = [str(node.get("id") or "") for node in group]
            merge_review()
            if budget_blocked or time.monotonic() >= reserve_start:
                message = (
                    "model budget exhausted; batch not started"
                    if budget_blocked and time.monotonic() < reserve_start
                    else f"verification reserve reached; batch {batch_index} not started"
                )
                incomplete.update(group_ids)
                progress["checks"].append({"type": "skip", "batch": batch_index, "detail": message})
                progress["unresolved"].append(f"batch {batch_index} ({', '.join(group_ids)}): {message}")
                _progress_log(output, f"batch {batch_index} skipped: {message}")
                continue
            if not _can_dispatch(bridge, config, deadline):
                incomplete.update(group_ids)
                progress["checks"].append(
                    {"type": "skip", "batch": batch_index,
                     "detail": "model call/time budget exhausted"}
                )
                progress["unresolved"].append(f"batch {batch_index} ({', '.join(group_ids)}): budget exhausted")
                _progress_log(output, f"batch {batch_index} skipped: model call/time budget exhausted")
                continue

            for node_id in group_ids:
                runtime.events.mark_implementation_started(node_id, f"batch {batch_index}/{len(batches)}")
            attempted.update(group_ids)
            _progress_log(output, f"batch {batch_index}/{len(batches)} implementing: {', '.join(group_ids)}")

            skill_names = select_skills(tree, group_ids)
            skills_text, missing = skill_text(config.project_dir / "skills", skill_names)
            selected_skills.update(skill_names)
            absent_skills.update(missing)
            for name in skill_names:
                path = config.project_dir / "skills" / name / "SKILL.md"
                if path.is_file():
                    skills_paths[name] = str(path)

            before_files = _source_map(output)
            before_state = _source_state(output)
            follow_ups = 0
            chunk_count = 1
            file_listing = _file_listing(output)
            (evidence_dir / "project-map.md").write_text(file_listing, encoding="utf-8")
            prompt_text = batch_prompt(
                group, skills_text, batch_index, requirement_dir=requirement_dir,
                file_listing=file_listing,
            )
            compact_notes = ""
            # Batches continue in one session; it is handed off (note + fresh session) only when a request
            # passed 80% of PI_CONTEXT_TOKENS.
            if bridge.needs_compaction(config.pi_context_tokens):
                compact_notes = str(bridge.compact(_next_batch_scan(group)) or "")
                _progress_log(output, "handoff -> fresh session")
            preamble = _batch_preamble(carried_build_error, review_notes, compact_notes,
                                       folder_contracts(tree, group_ids), error_log.recent(),
                                       [node_id for node_id in group_ids if node_id in changed_ids])
            review_notes = ""
            if preamble:
                prompt_text = preamble + "\n\n" + prompt_text
            chunked = False
            chunks: list[str] = []
            overhead = bridge.context_overhead_bytes
            if (overhead is not None
                    and len(prompt_text.encode("utf-8")) + overhead > config.pi_context_bytes):
                chunk_target = config.pi_context_bytes - overhead - CHUNK_GROWTH_RESERVE_BYTES
                if chunk_target >= CHUNK_MIN_BYTES:
                    chunks = chunked_batch_prompts(
                        group, skills_text, batch_index,
                        requirement_dir=requirement_dir,
                        file_listing=file_listing,
                        chunk_target_bytes=chunk_target,
                    )
                    chunked = True
                    chunk_count = len(chunks)
            if chunked:
                if preamble and chunks:
                    chunks[0] = preamble + "\n\n" + chunks[0]
                response = {}
                for chunk_text in chunks:
                    if budget_blocked or not _can_dispatch(bridge, config, deadline):
                        break
                    response = _prompt(bridge, config, chunk_text, batch_until, output)
                    if response.get("stopReason") == "budget":
                        budget_blocked = True
                    if (response.get("stopReason") == "error"
                            and response.get("errorCode") == "context"):
                        break
            else:
                response = _prompt(bridge, config, prompt_text, batch_until, output)
                if response.get("stopReason") == "budget":
                    budget_blocked = True

            build_status, build_error = "not_run", ""
            built_state = None
            idle_follow_ups = 0
            build_fix_grace = 0
            contract_checked = False
            while True:
                stop_reason = _stop_reason(response)
                if stop_reason not in ("stop", "turn_limit", "length", "prompt_timeout", "bad_tool"):
                    break
                current_state = _source_state(output)
                if current_state != before_state and current_state != built_state:
                    build_status, build_error = _batch_build(
                        output, evidence_dir / f"verify-batch-{batch_index:03d}-{follow_ups}",
                        web_port, run_id)
                    built_state = current_state
                    progress["checks"].append({"type": "build", "batch": batch_index,
                                               "status": build_status, "detail": build_error})
                fixing_build = build_status == "failed" or (
                    build_status == "not_run" and bool(carried_build_error))
                if stop_reason == "bad_tool":
                    bridge.restart()
                    follow_up_text = BAD_TOOL_HINT + prompt_text
                elif build_status == "failed":
                    follow_up_text = _build_fix_prompt(batch_index, build_error, output, stop_reason)
                elif stop_reason != "stop":
                    follow_up_text = _continuation_prompt(batch_index, stop_reason, output)
                elif current_state == before_state and not nudge_used:
                    follow_up_text = nudge_prompt()
                elif build_status == "passed" and not contract_checked:
                    # one cheap, model-free check of names and seed records instead of a review round
                    contract_checked = True
                    notes = contract_notes(output, group)
                    _progress_log(output, f"batch {batch_index}/{len(batches)} contract check: "
                                          f"{len(notes)} gap(s)")
                    if not notes:
                        break
                    follow_up_text = contract_prompt(batch_index, notes)
                else:
                    break
                over_limit = follow_ups >= MAX_BATCH_FOLLOW_UPS or time.monotonic() >= batch_until
                if over_limit and fixing_build and build_fix_grace < MAX_BUILD_FIX_GRACE:
                    build_fix_grace += 1
                    over_limit = False
                idle_limit = 3 if stop_reason in ("length", "bad_tool") else 2  # nothing was saved: retry
                if over_limit or idle_follow_ups >= idle_limit or not _can_dispatch(bridge, config, deadline):
                    break
                if follow_up_text == nudge_prompt():
                    nudge_used = True
                follow_ups += 1
                reason = "build failed" if build_status == "failed" else stop_reason
                _progress_log(output, f"batch {batch_index}/{len(batches)} follow-up {follow_ups} ({reason})")
                response = _prompt(bridge, config, follow_up_text, batch_until, output)
                if response.get("stopReason") == "budget":
                    budget_blocked = True
                idle_follow_ups = idle_follow_ups + 1 if _source_state(output) == current_state else 0

            changed = _changed_paths(before_files, output)
            state_changed = _source_state(output) != before_state
            if (changed or state_changed) and _source_state(output) != built_state:
                build_status, build_error = _batch_build(
                    output, evidence_dir / f"verify-batch-{batch_index:03d}-final", web_port, run_id)
                progress["checks"].append({"type": "build", "batch": batch_index,
                                           "status": build_status, "detail": build_error})
            if changed or state_changed:
                any_source_changes = True
            if build_status in ("passed", "failed"):
                carried_build_error = build_error if build_status == "failed" else ""
            if (changed or state_changed) and build_status == "passed" and reviewer is not None:
                merge_review()  # pick up reviews that finished during this batch before guarding
            if ((changed or state_changed) and build_status == "passed" and guard
                    and reviewer is not None and not carried_build_error
                    and time.monotonic() < reserve_start):
                broken = _regression_check(output, reviewer.tests_dir, guard,
                                           evidence_dir / f"guard-{batch_index:03d}",
                                           web_port, run_id, leaf_ids)
                _progress_log(output, f"batch {batch_index}/{len(batches)} regression guard: "
                                      f"{len(broken)} of {len(guard)} earlier passing scenario(s) broke")
                error_log.add(f"executor batch {batch_index} regression",
                              [f"BROKE {row.get('title')}: "
                               + " | ".join(_informative_error_lines(str(row.get('error') or ''))[:3])
                               for row in broken])
                if broken and _can_dispatch(bridge, config, deadline):
                    _prompt(bridge, config, _regression_prompt(batch_index, broken), batch_until, output)
                    build_status, build_error = _batch_build(
                        output, evidence_dir / f"verify-batch-{batch_index:03d}-guard", web_port, run_id)
                    progress["checks"].append({"type": "build", "batch": batch_index,
                                               "status": build_status, "detail": build_error})
                    carried_build_error = build_error if build_status == "failed" else ""

            produced = bool(changed) or state_changed
            final_stop = _stop_reason(response)
            interrupted = final_stop in ("turn_limit", "length", "prompt_timeout", "budget", "error", "cancelled",
                                         "bad_tool")
            if interrupted:
                incomplete.update(group_ids)
            if produced and build_status == "passed" and not interrupted:
                status = "implemented"
                for node_id in group_ids:
                    runtime.events.mark_implementation_done(
                        node_id, "source changed and build passed; acceptance pending"
                    )
                runtime.git.commit(f"arc_pi batch {batch_index}: {', '.join(group_ids)}")
            elif produced and build_status == "failed":
                status = "failed"
                for node_id in group_ids:
                    runtime.events.mark_implementation_failed(
                        node_id, f"build failed: {build_error or 'see report'}"
                    )
            else:
                status = "incomplete"
                incomplete.update(group_ids)
                for node_id in group_ids:
                    runtime.events.mark_implementation_failed(
                        node_id,
                        (f"interrupted by {final_stop}; implementation not proven"
                         if interrupted
                         else "no source changes and no acceptance proof"),
                    )
            progress["batches"].append(
                {
                    "index": batch_index,
                    "nodeIds": group_ids,
                    "status": status,
                    "changedPaths": changed,
                    "buildCheck": build_status,
                    "message": build_error,
                    "stopReason": final_stop,
                    "followUps": follow_ups,
                    "chunks": chunk_count,
                }
            )
            if status != "implemented":
                progress["unresolved"].append(f"batch {batch_index} ({', '.join(group_ids)}): {status}")
            batch_line = (f"batch {batch_index}/{len(batches)} {status}: build={build_status}, "
                          f"stop={final_stop}, changed files={len(changed)}")
            if build_error:
                batch_line += f", error: {_first_line(build_error)}"
            if response.get("errorMessage"):
                batch_line += (f", model {response.get('errorCode') or 'stop'}: "
                               f"{_first_line(response['errorMessage'])}")
            _progress_log(output, batch_line)
            if build_status == "failed" and build_error:
                error_log.add(f"executor batch {batch_index} build",
                              _informative_error_lines(build_error)[:4])
            if reviewer is not None and produced:
                gate_until = min(reserve_start, time.monotonic() + REVIEW_GATE_SECONDS)
                reviewer.submit(
                    batch_index, group,
                    group_ids if status == "failed" and not interrupted else [],
                    "\n".join(combined_chain_text(tree, node_id) for node_id in group_ids),
                    until=gate_until)
                reviewer.drain(until=gate_until + REVIEW_GATE_GRACE_SECONDS)
                merge_review()
            if (self_review is not None and config.pi_batch_review and produced
                    and time.monotonic() < reserve_start):
                self_until = (min(batch_until, time.monotonic() + config.pi_self_review_seconds)
                              if config.pi_self_review_seconds else batch_until)
                checked = self_review.run(
                    batch_index, group,
                    "\n".join(combined_chain_text(tree, node_id) for node_id in group_ids),
                    self_until, lambda text, fixing, until=None: self_converse(
                        text, fixing, min(self_until, until or self_until)))
                _record_test_events(runtime, checked, leaf_ids)
                build_status, build_error = _batch_build(
                    output, evidence_dir / f"verify-batch-{batch_index:03d}-self", web_port, run_id)
                carried_build_error = build_error if build_status == "failed" else ""
                if build_status == "passed":
                    runtime.git.commit(f"arc_pi batch {batch_index} self-review")

        missing_specs = ([node_id for node_id in leaf_ids
                          if not (self_review.tests_dir / f"{node_id}.spec.js").is_file()]
                         if self_review is not None else [])
        if missing_specs and deadline - time.monotonic() > FINAL_SPECS_MIN_SECONDS:
            # before the final review (which may use its whole box): requirements without specs are
            # invisible to final verification and repair (also an app carried over from a stage)
            specs_dir = self_review.tests_dir
            specs_dir.mkdir(parents=True, exist_ok=True)
            if not (specs_dir / TEST_HELPERS).is_file():
                shutil.copy2(config.project_dir / "skills" / HELPERS_SOURCE[0] / HELPERS_SOURCE[1],
                             specs_dir / TEST_HELPERS)
            specs_until = min(deadline - FINAL_SPECS_MIN_SECONDS, time.monotonic() + 900)
            # fresh session: the last batch's long context would be resent on every spec-writing call
            specs_notes = str(bridge.compact() or "")
            _progress_log(output, "handoff -> fresh session for the scenario tests")
            self_converse((NOTE_HEADING + specs_notes[:MAX_NOTE_CHARS] + "\n\n" if specs_notes else "")
                          + FINAL_SPECS_PROMPT.format(ids=", ".join(missing_specs)), False, specs_until)
            written = [specs_dir / f"{node_id}.spec.js" for node_id in missing_specs]
            dropped = [spec.name for spec in written
                       if spec.is_file() and any(pattern.search(spec.read_text(encoding="utf-8", errors="replace"))
                                                 for pattern, _ in FORBIDDEN)]
            for name in dropped:
                (specs_dir / name).unlink()
            missing_nodes = [node for node in ordered if str(node.get("id")) in missing_specs]
            untested = _missing_scenarios(specs_dir, missing_nodes)
            if untested and time.monotonic() < specs_until:
                self_converse("These scenarios still have no `test()` titled exactly with their name: "
                              + "; ".join(untested[:30]) + ". Add them now to the spec file of their "
                              "requirement id, following the same rules; do not change application code.",
                              False, specs_until)
                untested = _missing_scenarios(specs_dir, missing_nodes)
            _progress_log(output, f"final specs: {len(list(specs_dir.glob('*.spec.js')))} file(s) "
                                  f"written; dropped {len(dropped)} with shortcuts; "
                                  f"{len(untested)} scenario(s) without a test")
        if (config.pi_final_review and self_review is not None
                and time.monotonic() < deadline - final_reserve_seconds + final_review_seconds):
            final_until = min(deadline - (final_reserve_seconds - final_review_seconds),
                              time.monotonic() + final_review_seconds)
            if bridge.needs_compaction(config.pi_context_tokens):
                bridge.compact()
            before_review = evidence_dir / "before-final-review"
            snapshot_app(output, before_review)
            before_status, _ = _batch_build(output, evidence_dir / "verify-before-final-review",
                                            web_port, run_id)
            self_review.final_review(ordered, final_until,
                                     lambda text, fixing: self_converse(text, fixing, final_until),
                                     contract_notes(output, ordered))
            build_status, build_error = _batch_build(
                output, evidence_dir / "verify-final-review", web_port, run_id)
            if build_status == "passed":
                runtime.git.commit("arc_pi final review")
            elif before_status == "passed":
                # never hand a broken build/backend to grading: undo the final review's edits
                restore_app(before_review, output)
                error_log.add("final review", [f"reverted: {_first_line(build_error)}"])
                _progress_log(output, "final review broke build or startup; its changes were reverted")
        if reviewer is not None:
            reviewer.drain(until=deadline - FINAL_VERIFY_SECONDS)
            reviewer.stop()
            merge_review()
            if reviewer.has_tests():
                tests_dir = reviewer.tests_dir
                _progress_log(output, f"final verification uses the reviewer's scenario tests in {tests_dir}")
        if self_review is not None and self_review.has_tests():
            tests_dir = self_review.tests_dir
            _progress_log(output, f"final verification uses the agent's own scenario tests in {tests_dir}")
        blind_suite = None
        if blind is not None:
            blind.join(120)
            blind_suite = blind.tests_dir()
            if blind_suite is not None and tests_dir is not None:
                # both suites: the own specs carried the repairs that lifted results, the blind ones
                # add what a tester without the source expects
                combined = evidence_dir / "combined-suite"
                shutil.rmtree(combined, ignore_errors=True)
                shutil.copytree(tests_dir, combined)
                shutil.copytree(blind_suite, combined / "blind")
                tests_dir = combined
                _progress_log(output, f"final verification uses the own and the blind tests in {tests_dir}")
            elif blind_suite is not None:
                tests_dir = blind_suite
                _progress_log(output, f"final verification uses the blind requirement tests in {tests_dir}")

        def acceptance(name: str, tests: Path | None) -> dict:
            return verify_application(
                output_dir=output,
                tests_dir=tests,
                report_dir=evidence_dir / name,
                deadline=time.monotonic() + VERIFY_WATCHDOG_SECONDS,
                grading_port=web_port,
                known_requirement_ids=set(leaf_ids),
                suite_timeout_seconds=_suite_timeout(deadline),
                command="generate",
                run_id=run_id,
            )

        final_name = "verify-final"
        try:
            report = acceptance(final_name, tests_dir)
        except Exception as exc:
            if tests_dir is None:
                raise
            _progress_log(output, f"scenario tests could not run ({_first_line(str(exc))}); verifying without them")
            tests_dir = None
            final_name = "verify-final-no-tests"
            report = acceptance(final_name, None)
        if tests_dir is not None and report.get("total", 0) == 0:
            _progress_log(output, "scenario tests did not load; verifying without them")
            tests_dir = None
            final_name = "verify-final-no-tests"
            report = acceptance(final_name, None)
        _record_test_events(runtime, report, leaf_ids)
        _log_verification(output, "final verification", report)
        error_log.add("final verification", _failure_lines(report, MAX_REPAIR_FAILURES))
        best_report = report
        best_report_dir = evidence_dir / final_name
        best_dir = evidence_dir / "best"
        snapshot_app(output, best_dir)

        # 9. Bounded evidence-driven repair with keep-best preservation.
        repair_rounds = config.pi_repair_rounds
        used_rounds = 0
        previous_repair_status = None
        repair_report = best_report
        while used_rounds < repair_rounds and _needs_repair(repair_report):
            if not _can_repair_dispatch(bridge, config, deadline):
                progress["unresolved"].append("repair stopped: deadline or shutdown reserve reached")
                break
            failures = _failure_lines(repair_report, MAX_REPAIR_FAILURES)
            if not failures:
                break
            if deadline - time.monotonic() < MIN_REPAIR_RESERVE_SECONDS:
                progress["unresolved"].append(
                    "repair stopped: insufficient time for another full verification plus shutdown"
                )
                progress["checks"].append({"type": "repair-skip", "detail": "time reserve reached"})
                break
            used_rounds += 1
            repair_skill_names = select_skills(tree, leaf_ids, repair=True)
            repair_skills_text, missing = skill_text(config.project_dir / "skills", repair_skill_names)
            selected_skills.update(repair_skill_names)
            absent_skills.update(missing)
            for name in repair_skill_names:
                path = config.project_dir / "skills" / name / "SKILL.md"
                if path.is_file():
                    skills_paths[name] = str(path)
            remaining = max(0, int(deadline - time.monotonic()))
            repair_prompt = f"{failure_digest(failures)}\n\nRemaining time budget: {remaining}s."
            repair_prompt += ("\n\nA failing test whose requirement id is not in the current requirement "
                              "files checks behaviour from an earlier version; when it contradicts the "
                              "current requirement text, follow the current text.")
            if blind_suite is not None:
                repair_prompt += ("\n\nTests under `blind/` were written from the requirement text by a "
                                  "separate tester that cannot see your code: each starts at `/` and "
                                  "clicks the quoted names. "
                                  "Fix the APPLICATION (names, roles, reachability from `/`, seed "
                                  "data); skip only a failure whose test step the requirement "
                                  "text does not support.")
            if previous_repair_status == "reverted":
                repair_prompt += (
                    "\n\nThe previous repair attempt did not improve the retained "
                    "result and was reverted. Make a different, minimal change this time."
                )
            elif previous_repair_status == "unchanged":
                repair_prompt += (
                    "\n\nThe previous repair attempt made no source changes. "
                    "Make a concrete minimal edit for the observed failure."
                )
            elif previous_repair_status in ("accepted", "provisional"):
                repair_prompt += (
                    "\n\nThe previous repair is present in the working source. "
                    "Its latest verification is shown above. Preserve the fixes and "
                    "resolve the remaining assertions, including UI roles and exact text."
                )
            if repair_skills_text:
                repair_prompt = f"[Skills]\n{repair_skills_text}\n\n{repair_prompt}"
            repair_prompt += ("\n\nThe test files are fixed in this step: change only application "
                              "code (frontend/ and backend/); edits under review-tests/ are undone.")
            frozen_specs = _freeze_specs(output / TESTS_DIR_NAME)
            before_repair_state = _source_state(output)
            final_level = fix_level[0] if self_review is not None else None
            repair_response = _prompt(bridge, config, repair_prompt, deadline, output, reasoning=final_level)
            for _ in range(MAX_REPAIR_CONTINUATIONS):
                if (_source_state(output) != before_repair_state
                        or _stop_reason(repair_response) not in ("turn_limit", "length", "prompt_timeout")
                        or not _can_dispatch(bridge, config, deadline)):
                    break
                repair_response = _prompt(bridge, config, REPAIR_CONTINUE_PROMPT, deadline, output,
                                          reasoning=final_level)
            _restore_specs(output / TESTS_DIR_NAME, frozen_specs)
            if _source_state(output) == before_repair_state:
                previous_repair_status = "unchanged"
                progress["checks"].append(
                    {
                        "type": "repair",
                        "round": used_rounds,
                        "status": "unchanged",
                        "detail": "no source change from the repair prompt; previous report retained",
                    }
                )
                _progress_log(output, f"repair {used_rounds} unchanged: the model made no source change "
                                      f"(stop={repair_response.get('stopReason')})")
                if repair_response.get("stopReason") == "budget":
                    progress["unresolved"].append(
                        "repair stopped: model call/time budget exhausted"
                    )
                    break
                continue
            candidate_dir = evidence_dir / f"verify-repair-{used_rounds}"
            candidate = verify_application(
                output_dir=output,
                tests_dir=tests_dir,
                report_dir=candidate_dir,
                deadline=time.monotonic() + VERIFY_WATCHDOG_SECONDS,
                grading_port=web_port,
                known_requirement_ids=set(leaf_ids),
                suite_timeout_seconds=_suite_timeout(deadline),
                command="generate",
                run_id=run_id,
            )
            _record_test_events(runtime, candidate, leaf_ids)
            _log_verification(output, f"repair {used_rounds} verification", candidate)
            if _repair_accepted(best_report, candidate):
                snapshot_app(output, best_dir)
                best_report = candidate
                best_report_dir = candidate_dir
                previous_repair_status = "accepted"
                repair_report = candidate
                provisional_repair = False
                progress["checks"].append(
                    {
                        "type": "repair",
                        "round": used_rounds,
                        "status": "accepted",
                        "detail": f"{candidate.get('passed', 0)}/{candidate.get('total', 0)} passed",
                    }
                )
                _progress_log(output, f"repair {used_rounds} accepted: best result is now "
                                      f"{candidate.get('passed', 0)}/{candidate.get('total', 0)}")
            elif used_rounds < repair_rounds and _repair_can_continue(best_report, candidate):
                repair_report = candidate
                provisional_repair = True
                previous_repair_status = "provisional"
                progress["checks"].append(
                    {
                        "type": "repair",
                        "round": used_rounds,
                        "status": "provisional",
                        "detail": "no passing tests lost; continue from latest failures without replacing best",
                    }
                )
                _progress_log(output, f"repair {used_rounds} provisional: "
                                      "pass count unchanged; next repair uses the current source and failures")
            else:
                restore_app(best_dir, output)
                previous_repair_status = "reverted"
                repair_report = best_report
                provisional_repair = False
                progress["checks"].append(
                    {
                        "type": "repair",
                        "round": used_rounds,
                        "status": "reverted",
                        "detail": "candidate regressed or did not improve; best source restored",
                    }
                )
                _progress_log(output, f"repair {used_rounds} reverted: no improvement; best source restored")
        if provisional_repair:
            restore_app(best_dir, output)
            provisional_repair = False
            _progress_log(output, "provisional repair restored: no verified improvement before repair stopped")
        report = best_report
        _record_test_events(runtime, report, leaf_ids)
        if report.get("build") == "passed":
            _progress_log(output, "runtime data: " + _ship_clean_data(
                output, ordered, evidence_dir / "verify-clean-data", web_port, run_id))

        # 10. Outcome and preview proof.
        build_ok = report.get("build") == "passed"
        bootable = report.get("startup") in ("passed", "not_run")
        acceptance = report.get("acceptance")
        all_implemented = not incomplete
        if any_source_changes and build_ok and report.get("startup") == "passed":
            runtime.events.notify_commit_history_changed(
                f"run {run_id} app built and HTTP-ready; preview available", preview=True
            )
        if all_implemented and acceptance == "passed":
            outcome, exit_code = "completed", 0
            reason = f"acceptance passed {report.get('passed', 0)}/{report.get('total', 0)}"
            runtime.events.mark_run_completed(reason)
        elif build_ok and bootable:
            outcome, exit_code = "partial", 0
            parts: list[str] = []
            if not all_implemented:
                parts.append(f"{len(incomplete)} requirement node(s) not implemented")
            if acceptance != "passed":
                if acceptance in ("unavailable", "not_run"):
                    parts.append("acceptance unavailable")
                else:
                    parts.append(
                        f"acceptance {acceptance} ({report.get('passed', 0)}/{report.get('total', 0)} passed)"
                    )
            reason = f"INCOMPLETE: {'; '.join(parts)}" if parts else "INCOMPLETE"
            runtime.events.mark_run_completed(reason)
        else:
            outcome, exit_code = "failed", 0
            reason = (
                report.get("error")
                or f"build={report.get('build')} startup={report.get('startup')} acceptance={acceptance}"
            )
            runtime.events.mark_run_failed(reason)
    except Exception as exc:
        fatal_error = exc
    finally:
        if provisional_repair:
            try:
                restore_app(best_dir, output)
            except Exception as exc:
                fatal_error = fatal_error or exc
        if reviewer is not None:
            reviewer.close()
            progress["reviewerUsage"] = reviewer.totals
        if blind is not None:
            progress["blindTestUsage"] = blind.totals
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass
        totals = bridge.totals if bridge is not None else {}
        metrics = _build_metrics(run_id, config, started, totals, best_report)
        if fatal_error is not None:
            outcome, exit_code, reason = "failed", 1, f"{type(fatal_error).__name__}: {fatal_error}"
        progress["outcome"] = outcome
        progress["attemptedLeafIds"] = sorted(attempted)
        progress["incompleteLeafIds"] = sorted(incomplete)
        progress["skills"]["selected"] = sorted(selected_skills)
        progress["skills"]["absent"] = sorted(absent_skills)
        progress["skills"]["paths"] = dict(sorted(skills_paths.items()))
        for path, payload in (
            (evidence_dir / "metrics.json", metrics),
            (evidence_dir / "progress.json", progress),
            (evidence_dir / "verification.json", best_report),
        ):
            if payload is None:
                continue
            try:
                atomic_json(path, payload)
            except OSError as exc:
                detail = f"evidence write failed for {path}: {exc}"
                progress["unresolved"].append(detail)
                report_error(detail, command="generate", phase="evidence",
                             run_id=run_id, report_path=str(path))

    if fatal_error is not None:
        runtime.events.mark_run_failed(reason)
    _progress_log(output, f"run {run_id} finished: outcome={outcome}, reason: {reason}; "
                          f"evidence: {evidence_dir}")

    verification_path = None
    if best_report_dir is not None:
        verification_path = best_report_dir / "verification.json"
    return {
        "outcome": outcome,
        "exitCode": exit_code,
        "reason": reason,
        "runId": run_id,
        "outputDir": str(output),
        "evidenceDir": str(evidence_dir),
        "metricsPath": str(evidence_dir / "metrics.json"),
        "verificationPath": str(verification_path) if verification_path is not None else None,
        "verification": best_report,
        "progress": progress,
        "metrics": metrics,
    }
