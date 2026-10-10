from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from .artifacts import atomic_json, source_hash
from .errorlog import report_verification_errors
from .processes import (free_port, resolve_node_bin, run_command,
                        start_process, stop_process)

PROJECT_DIR = Path(__file__).resolve().parents[1]

EXCLUDED_FROM_COPY = frozenset({".git", ".arc", ".venv", "node_modules", "dist"})
SOURCE_HASH_EXTRA = frozenset({"data", ".cache"})
SPEC_PATTERN = re.compile(r"\.(spec|test)\.(js|ts|jsx|tsx|mjs|cjs)$")
_SOURCE_EXTS = (".mjs", ".js", ".ts", ".jsx", ".tsx", ".json")
_STATIC_IMPORT_RE = re.compile(
    r"(?:^|\n)\s*(?:import|export)\s+(?:[^'\"]*?\sfrom\s+)?(['\"])([^'\"]+)\1",
    re.MULTILINE)
STATUS_MAP = {"passed": "passed", "expected": "passed", "unexpected": "failed",
              "failed": "failed", "flaky": "failed", "skipped": "skipped",
              "timedOut": "timed_out", "interrupted": "interrupted"}
MAX_STARTUP_WAIT_SECONDS = 30.0
MAX_KEEP_BYTES = 64 * 1024
TAIL_CHARS = 4000
_INSTALL_LOCK = threading.RLock()


class VerifyError(Exception):
    """Structural/fatal verification problem (never a model failure)."""


# primitives


def _remove_tree(path: Path) -> None:
    """Remove a path; junctions/symlinks are unlinked, never followed."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    try:
        shutil.rmtree(path)
    except OSError:
        for attempt in (os.rmdir, os.unlink):
            try:
                attempt(path)
                return
            except OSError:
                continue


def _walk_files(root: Path, exclude_names: frozenset, depth: int = 0) -> list[Path]:
    out: list[Path] = []
    if depth > 64:
        return out
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return out
    for entry in entries:
        if entry.name in exclude_names:
            continue
        if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
            out.extend(_walk_files(Path(entry.path), exclude_names, depth + 1))
        elif entry.is_file(follow_symlinks=False):
            out.append(Path(entry.path))
    return out



TITLE_CALL_RE = re.compile(r"""(\btest(?:\.only)?\(\s*)(['"`])(.+?)\2""")


def _dedupe_test_titles(spec: Path) -> None:
    """Playwright refuses a whole suite when one file repeats a test title; requirement scenario
    names can repeat, so number the repeats in the verification copy."""
    text = spec.read_text(encoding="utf-8", errors="replace")
    seen: dict[str, int] = {}

    def renumber(match):
        title = match.group(3)
        seen[title] = seen.get(title, 0) + 1
        if seen[title] == 1:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}{title} ({seen[title]}){match.group(2)}"

    fixed = TITLE_CALL_RE.sub(renumber, text)
    if fixed != text:
        spec.write_text(fixed, encoding="utf-8")


UNIQUE_TITLES_JS = (
    "function __uniqueTitles(pw) {\n"
    "  // verification copy only: number repeated titles of tests declared in a loop (Playwright\n"
    "  // refuses the whole suite on one duplicate); the wrapper lives in this file so each test\n"
    "  // keeps this spec file as its location\n"
    "  const seen = new Map();\n"
    "  const test = new Proxy(pw.test, { apply(target, self, args) {\n"
    "    if (typeof args[0] === 'string') {\n"
    "      const count = (seen.get(args[0]) || 0) + 1;\n"
    "      seen.set(args[0], count);\n"
    "      if (count > 1) args[0] = `${args[0]} (${count})`;\n"
    "    }\n"
    "    return Reflect.apply(target, self, args);\n"
    "  } });\n"
    "  return { ...pw, test };\n"
    "}\n"
)
PLAYWRIGHT_REQUIRE_RE = re.compile(r"""require\(\s*(['"])@playwright/test\1\s*\)""")


def _unique_titles(spec: Path) -> None:
    """Wrap a spec's `require('@playwright/test')` so loop-declared repeated titles are numbered."""
    text = spec.read_text(encoding="utf-8", errors="replace")
    fixed = PLAYWRIGHT_REQUIRE_RE.sub(lambda m: f"__uniqueTitles({m.group(0)})", text)
    if fixed != text:
        spec.write_text(UNIQUE_TITLES_JS + fixed, encoding="utf-8")


LOCAL_URL_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0):\d+")


def _point_at(script: Path, base_url: str) -> None:
    """Specs and helpers often pin the platform port (http://127.0.0.1:3000); the check runs the
    app on another port, so point those literals at it in the verification copy."""
    text = script.read_text(encoding="utf-8", errors="replace")
    fixed = LOCAL_URL_RE.sub(base_url.rstrip("/"), text)
    if fixed != text:
        script.write_text(fixed, encoding="utf-8")

def _copy_ignore(directory, names):
    return sorted(EXCLUDED_FROM_COPY & set(names))


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, ignore=_copy_ignore, symlinks=True)


class _Log:
    """Capped in-memory tail plus full UTF-8 retention on disk."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            self._path.unlink()
        self._chunks: list[str] = []
        self._bytes = 0
        self._lock = threading.Lock()

    def push(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._chunks.append(text)
            self._bytes += len(text)
            while self._bytes > MAX_KEEP_BYTES and len(self._chunks) > 1:
                self._bytes -= len(self._chunks.pop(0))
            with open(self._path, "a", encoding="utf-8", errors="replace") as handle:
                handle.write(text)

    def tail(self) -> str:
        with self._lock:
            return "".join(self._chunks)[-TAIL_CHARS:]

    def text(self) -> str:
        with self._lock:
            return "".join(self._chunks)


_SERVER_ERROR_RE = re.compile(r"^\s*(?:Uncaught\s+)?(?:[A-Z]\w*(?:Error|Exception)|Error)\b[:\s].*")
MAX_SERVER_ERRORS = 5


def server_errors(log_text: str) -> list[str]:
    """Unique backend error lines, each with its first app stack frame."""
    found: list[str] = []
    lines = log_text.splitlines()
    for index, line in enumerate(lines):
        if not _SERVER_ERROR_RE.match(line):
            continue
        frame = next((later.strip() for later in lines[index + 1:index + 12]
                      if later.strip().startswith("at ") and "node_modules" not in later
                      and "node:internal" not in later), "")
        entry = (line.strip() + (f" ({frame})" if frame else ""))[:300]
        if entry not in found:
            found.append(entry)
        if len(found) >= MAX_SERVER_ERRORS:
            break
    return found


class _RunResult:
    def __init__(self, code: int, stdout: str, stderr: str, timed_out: bool):
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out

    def tail(self) -> str:
        return (self.stderr + self.stdout)[-TAIL_CHARS:]


def _run(argv, *, cwd: Path, deadline: float, env=None, log: _Log) -> _RunResult:
    try:
        result = run_command(argv, cwd=cwd, deadline=deadline, env=env)
    except TimeoutError as exc:
        log.push(f"{exc}\n")
        return _RunResult(-1, "", "", True)
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired carries raw bytes even for text-mode processes.
        stdout, stderr = _text(exc.stdout), _text(exc.stderr)
        log.push(stdout + stderr)
        log.push("command timed out\n")
        return _RunResult(-1, stdout, stderr, True)
    log.push(result.stdout)
    log.push(result.stderr)
    return _RunResult(result.returncode, result.stdout, result.stderr, False)


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


class _BackendProcess:
    """Owned backend child with concurrent stdout/stderr draining."""

    def __init__(self, proc, log: _Log):
        self._proc = proc
        self._threads = []
        for stream in (proc.stdout, proc.stderr):
            thread = threading.Thread(target=self._drain, args=(stream, log),
                                      daemon=True)
            thread.start()
            self._threads.append(thread)

    @staticmethod
    def _drain(stream, log: _Log) -> None:
        try:
            for chunk in iter(lambda: stream.read(8192), ""):
                log.push(chunk)
        except Exception:
            pass

    def poll(self):
        return self._proc.poll()

    def stop(self, deadline: float) -> None:
        stop_process(self._proc, deadline)
        for thread in self._threads:
            thread.join(timeout=2)
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _probe(base_url: str) -> bool:
    try:
        with _LOOPBACK_OPENER.open(base_url, timeout=1.5) as response:
            response.status
        return True
    except Exception:
        return False


def _wait_http(base_url: str, timeout_seconds: float,
               proc: _BackendProcess | None) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        if _probe(base_url):
            return True
        time.sleep(0.4)
    return False


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _npm_bin() -> str:
    override = os.environ.get("PI_NPM")
    if override:
        return override
    node = shutil.which("node")
    if node:
        candidate = Path(node).parent / ("npm.cmd" if os.name == "nt" else "npm")
        if candidate.exists():
            return str(candidate)
    found = shutil.which("npm")
    if found:
        return found
    raise VerifyError("npm not found on PATH; install Node >=22.19.0")


def _link_dir(target: Path, link_path: Path) -> None:
    """Link or copy cached node_modules into place (junction on Windows)."""
    _remove_tree(link_path)
    try:
        os.symlink(str(target), str(link_path), target_is_directory=True)
    except OSError:
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/d", "/c", "mklink", "/J", str(link_path), str(target)],
                capture_output=True, timeout=30)
            if result.returncode == 0:
                return
        shutil.copytree(target, link_path, symlinks=True)


def _install_key(package_dir: Path) -> str:
    parts = []
    for name in ("package.json", "package-lock.json"):
        path = package_dir / name
        if path.exists():
            parts.append(path.read_bytes())
    if not parts:
        raise VerifyError(f"{package_dir} lacks a package.json")
    return hashlib.sha256(b"\0".join(parts)).hexdigest()[:24]


def _ensure_installed(package_dir: Path, deadline: float, log: _Log) -> Path:
    """Lock-keyed npm dependency cache; node_modules only, never source/data."""
    cache = PROJECT_DIR / ".cache" / "npm" / _install_key(package_dir)
    node_modules = cache / "node_modules"
    with _INSTALL_LOCK:
        if not node_modules.exists():
            scratch = cache / "scratch"
            _remove_tree(scratch)
            scratch.mkdir(parents=True, exist_ok=True)
            for name in ("package.json", "package-lock.json"):
                source = package_dir / name
                if source.exists():
                    shutil.copy2(source, scratch / name)
            args = (["ci"] if (package_dir / "package-lock.json").exists() else ["install"])
            args.extend(["--include=dev", "--include=optional", "--no-audit", "--no-fund"])
            result = _run([_npm_bin(), *args], cwd=scratch, deadline=deadline, log=log)
            if result.code != 0 and args[0] == "ci" and not result.timed_out:
                # package.json and lock out of sync (the agent added a dependency): a fresh npm
                # install resolves it, so do that instead of failing the check
                args[0] = "install"
                result = _run([_npm_bin(), *args], cwd=scratch, deadline=deadline, log=log)
            if result.code != 0 or not (scratch / "node_modules").exists():
                reason = "timeout" if result.timed_out else f"exit {result.code}"
                raise VerifyError(f"npm install failed ({reason}): {result.tail()}")
            os.replace(scratch / "node_modules", node_modules)
            _remove_tree(scratch)
    return node_modules


def _check_backend_syntax(backend_dir: Path, deadline: float,
                          log: _Log) -> str | None:
    files: list[Path] = []
    src_dir = backend_dir / "src"
    if src_dir.is_dir():
        files.extend(path for path in src_dir.rglob("*.js")
                     if "node_modules" not in path.parts)
    files.extend(path for path in backend_dir.glob("*.js")
                 if "node_modules" not in path.parts)
    node = resolve_node_bin(deadline)
    for path in files:
        result = _run([node, "--check", str(path)],
                      cwd=backend_dir, deadline=deadline, log=log)
        if result.code != 0:
            reason = "timeout" if result.timed_out else f"exit {result.code}"
            return (f"backend syntax check failed ({reason}) on "
                    f"{path.relative_to(backend_dir)}: {result.tail()}")
    return None


def _chromium_executable(node: str, grader_node_modules: Path, deadline: float,
                         log: _Log) -> Path:
    probe = _run([node, "-e",
                  "console.log(require('playwright').chromium.executablePath())"],
                 cwd=grader_node_modules, deadline=deadline, log=log)
    executable = probe.stdout.strip() if probe.code == 0 else ""
    if not executable:
        raise VerifyError("grader cache lacks the playwright package")
    executable_path = Path(executable)
    with _INSTALL_LOCK:
        if executable_path.exists():
            return executable_path
        cli = grader_node_modules / "@playwright" / "test" / "cli.js"
        result = _run([node, str(cli), "install", "chromium"],
                      cwd=grader_node_modules, deadline=deadline, log=log)
        if result.code != 0 or not executable_path.exists():
            raise VerifyError(f"Chromium is unavailable and could not be installed: "
                              f"{result.tail()}")
    return executable_path


_HOME_PROBE_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const pageErrors = [];
  const consoleErrors = [];
  page.on('pageerror', (error) => pageErrors.push(String(error && error.message || error)));
  page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()); });
  try {
    await page.goto(process.argv[2], { waitUntil: 'load', timeout: 15000 });
    await page.waitForTimeout(1500);
  } catch (error) {
    pageErrors.push('navigation failed: ' + error.message);
  }
  const textLength = await page.evaluate(() => (document.body ? document.body.innerText.trim().length : 0)).catch(() => 0);
  console.log(JSON.stringify({ pageErrors: pageErrors.slice(0, 5), consoleErrors: consoleErrors.slice(0, 5), textLength }));
  await browser.close();
})().catch((error) => { console.log(JSON.stringify({ probeError: String(error) })); });
"""


def _probe_home_page(node: str, copy_dir: Path, base_url: str, deadline: float, log: _Log) -> dict | None:
    try:
        script = copy_dir / "home-probe.cjs"
        script.write_text(_HOME_PROBE_JS, encoding="utf-8")
        result = _run([node, str(script), base_url + "/"], cwd=copy_dir,
                      deadline=min(deadline, time.monotonic() + 60), log=log)
        line = next((part for part in reversed(result.stdout.splitlines()) if part.startswith("{")), "")
        data = json.loads(line) if line else None
        if not data or "probeError" in data:
            return None
        return {"blank": data.get("textLength", 0) == 0, "pageErrors": data.get("pageErrors") or [],
                "consoleErrors": data.get("consoleErrors") or []}
    except Exception:
        return None


_CAPTURE_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const [base, outDir, ...routes] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const shots = {};
  for (const [index, route] of routes.entries()) {
    try {
      await page.goto(base + route, { waitUntil: 'load', timeout: 15000 });
      await page.waitForTimeout(1200);
      const file = outDir + '/shot-' + index + '.png';
      await page.screenshot({ path: file });
      let aria = '';
      try { aria = await page.locator('body').ariaSnapshot({ timeout: 5000 }); } catch (e) {}
      shots[route] = { path: file, url: page.url(), aria };
    } catch (error) {
      shots[route] = { error: String(error && error.message || error) };
    }
  }
  console.log(JSON.stringify(shots));
  await browser.close();
})().catch((error) => { console.log(JSON.stringify({ captureError: String(error) })); });
"""


def _capture_screenshots(copy_dir: Path, base_url: str, routes: list[str],
                         out_dir: Path, deadline: float, log: _Log) -> dict:
    """Screenshot each app route in Chromium; {route: {path,url}|{error}}."""
    grader_dir = PROJECT_DIR / "grader"
    if not (grader_dir / "package.json").exists():
        raise VerifyError(f"bundled grader/ package is missing at {grader_dir}")
    node = resolve_node_bin(deadline)
    grader_node_modules = _ensure_installed(grader_dir, deadline, log)
    _chromium_executable(node, grader_node_modules, deadline, log)
    _link_dir(grader_node_modules, copy_dir / "node_modules")
    out_dir.mkdir(parents=True, exist_ok=True)
    script = copy_dir / "capture.cjs"
    script.write_text(_CAPTURE_JS, encoding="utf-8")
    result = _run([node, str(script), base_url, out_dir.resolve().as_posix(), *routes],
                  cwd=copy_dir, deadline=min(deadline, time.monotonic() + 30 + 20 * len(routes)),
                  log=log)
    line = next((part for part in reversed(result.stdout.splitlines()) if part.startswith("{")), "")
    data = json.loads(line) if line else {}
    if not data or "captureError" in data:
        raise VerifyError(f"screenshot capture failed: {data.get('captureError') or result.tail()}")
    return data


# report mapping


def map_playwright_report(report: dict, known_requirement_ids,
                          tests_root) -> list[dict]:
    known = set(known_requirement_ids) if known_requirement_ids else None
    root = str(tests_root).replace(os.sep, "/").rstrip("/")
    out: list[dict] = []

    def visit(suite: dict) -> None:
        for spec in suite.get("specs") or []:
            file = str(spec.get("file") or "").replace("\\", "/")
            relative_file = (file[len(root) + 1:]
                             if file.startswith(root + "/") else file)
            title = str(spec.get("title") or "")
            for test in spec.get("tests") or []:
                requirement_id = None
                for annotation in test.get("annotations") or []:
                    description = annotation.get("description") or ""
                    if annotation.get("type") == "requirement" and known \
                            and description in known:
                        requirement_id = description
                        break
                if requirement_id is None:
                    match = re.match(r"^(REQ-[\w.-]+)", title)
                    if match and known and match.group(1) in known:
                        requirement_id = match.group(1)
                project = str(test.get("projectName") or "")
                messages: list[str] = []
                snapshot = None
                for result in test.get("results") or []:
                    if not isinstance(result, dict):
                        continue
                    for item in (result.get("errors") or []) + [result.get("error")]:
                        text = _error_text(item.get("message")) if isinstance(item, dict) else None
                        if text and text not in messages:
                            messages.append(text)
                    snapshot = snapshot or _page_snapshot(result.get("attachments"))
                    if messages:
                        break
                if not messages:
                    for item in test.get("errors") or []:
                        text = _error_text(item.get("message")) if isinstance(item, dict) else None
                        if text and text not in messages:
                            messages.append(text)
                error = "\n".join(messages)[:3000] if messages else None
                status = test.get("status")
                results = test.get("results") or []
                if results and isinstance(results[0], dict) \
                        and results[0].get("status"):
                    status = results[0]["status"]
                out.append({
                    "id": hashlib.sha256(
                        f"{relative_file}|{title}|{project}".encode("utf-8")
                    ).hexdigest()[:32],
                    "requirementId": requirement_id,
                    "file": relative_file,
                    "title": title,
                    "project": project,
                    "status": STATUS_MAP.get(str(status or ""), "failed"),
                    "error": error,
                    "pageSnapshot": snapshot,
                })
        for child in suite.get("suites") or []:
            visit(child)

    for suite in report.get("suites") or []:
        visit(suite)
    return out


SNAPSHOT_ROLES = ("heading", "alert", "status", "dialog", "alertdialog", "link", "button")


def _page_snapshot(attachments) -> str | None:
    for item in attachments or []:
        if not isinstance(item, dict) or item.get("name") != "error-context" or not item.get("path"):
            continue
        try:
            text = Path(item["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        lines = []
        for raw in text.splitlines():
            line = re.sub(r"\s*\[(?:ref|cursor|level)=[^\]]*\]", "", raw.strip())
            if line.startswith("- ") and line[2:].split(" ", 1)[0].rstrip(":") in SNAPSHOT_ROLES:
                lines.append(line[2:][:160])
            if len(lines) >= 15:
                break
        return "\n".join(lines) or None
    return None


def _enrich_errors_from_log(tests: list[dict], log_text: str) -> None:
    if not log_text or not tests:
        return
    log_text = re.sub(r"\x1b\[[0-9;]*m", "", log_text)
    entry_re = re.compile(
        r"^\s*(?:(?:x|✘|✖|ok|✓)\s+\d+\s+\[|\d+\)\s+\[)"
    )
    blocks: list[tuple[str, str]] = []
    current_title: str | None = None
    current_lines: list[str] = []
    for line in log_text.splitlines():
        if entry_re.match(line):
            if current_title is not None:
                blocks.append((current_title, "\n".join(current_lines)))
            title_part = line.split("›")[-1].strip()
            current_title = title_part or line.strip()
            current_lines = [line]
        elif current_title is not None:
            current_lines.append(line)
    if current_title is not None:
        blocks.append((current_title, "\n".join(current_lines)))
    if not blocks:
        return

    def match_block(title: str) -> tuple[str, str] | None:
        detail_re = re.compile(r"^\s*\d+\)\s*\[")
        details = [b for b in blocks if detail_re.match(b[1].splitlines()[0])]
        inline_failed = [
            b for b in blocks
            if not detail_re.match(b[1].splitlines()[0])
            and not re.match(r"^\s*ok\s", b[1].splitlines()[0])
        ]
        for block_title, block_text in details + inline_failed:
            overlap = min(len(block_title), len(title))
            if overlap >= 20 and title[:overlap] == block_title[:overlap]:
                return block_title, block_text
        return None

    hint_re = re.compile(
        r"(waiting for .*|Locator: .*|Call log:|Error: .*(?:not found|resolved to|strict mode violation).*)"
    )
    for test in tests:
        if test.get("status") not in ("timed_out", "interrupted"):
            continue
        if "Locator:" in str(test.get("error") or ""):
            continue  # the JSON report already carried the assertion detail
        matched = match_block((test.get("title") or "").strip())
        if matched is None:
            continue
        hints: list[str] = []
        for line in matched[1].splitlines():
            if hint_re.search(line.strip()):
                hints.append(line.strip()[:240])
            if len(hints) >= 3:
                break
        if hints:
            existing = str(test.get("error") or "").strip()
            test["error"] = (existing + "\n" if existing else "") + " | ".join(hints)


def _error_text(message) -> str | None:
    if not message:
        return None
    if isinstance(message, dict):
        message = message.get("value") or str(message)
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(message))
    return text[:2000] or None


# main flow


MAX_REPORTED_IMPORT_FAILURES = 15


def _check_frontend_imports(frontend_dir: Path, warn=None) -> str | None:
    src = frontend_dir / "src"
    if not src.is_dir():
        return None
    try:
        files = sorted(path for path in src.rglob("*")
                       if path.is_file()
                       and path.suffix.lower() in _SOURCE_EXTS)
    except OSError:
        return None
    failures: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _STATIC_IMPORT_RE.finditer(text):
            specifier = match.group(2)
            if not specifier or not specifier.startswith("."):
                continue
            if "?" in specifier or "#" in specifier:
                continue
            target = (path.parent / specifier).resolve()
            suffix = Path(specifier).suffix.lower()
            if suffix and suffix not in _SOURCE_EXTS:
                continue  # CSS/asset-like: not the static check's job
            rel = path.relative_to(frontend_dir).as_posix()
            if suffix:
                if not target.is_file():
                    failures.append(f'unresolved import "{specifier}" from "{rel}"')
                continue
            candidates = [target]
            candidates.extend(target.with_name(target.name + ext)
                              for ext in _SOURCE_EXTS)
            candidates.extend(target / f"index{ext}" for ext in _SOURCE_EXTS)
            existing = [candidate for candidate in candidates
                        if candidate.is_file()]
            if len(existing) > 1 and warn is not None:
                warn(f'warning: ambiguous import "{specifier}" from "{rel}" '
                     f'matches {len(existing)} files ('
                     f'{", ".join(c.name for c in existing[:3])}'
                     f'{", …" if len(existing) > 3 else ""}); build continues\n')
            if not existing:
                failures.append(f'unresolved import "{specifier}" from "{rel}"')
    if not failures:
        return None
    shown = failures[:MAX_REPORTED_IMPORT_FAILURES]
    if len(failures) > len(shown):
        shown.append(f"... and {len(failures) - len(shown)} more unresolved import(s)")
    return "\n".join(shown)


def _resolve_tests_dir(tests_dir) -> Path | None:
    if tests_dir is None:
        return None
    resolved = Path(tests_dir).resolve()
    return resolved if resolved.is_dir() else None


def verify_application(*, output_dir, tests_dir=None, report_dir, deadline,
                       grading_port, port=None, build_only=False, start_backend=False,
                       known_requirement_ids=None, command: str = "verify",
                       run_id: str | None = None, screenshot_routes=None,
                       suite_timeout_seconds: float | None = None) -> dict:
    started = time.monotonic()
    output_dir = Path(output_dir).resolve()
    report_dir = Path(report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    frontend_dir = output_dir / "frontend"
    backend_dir = output_dir / "backend"
    if not (frontend_dir / "package.json").exists() \
            or not (backend_dir / "package.json").exists():
        raise VerifyError(f"output dir {output_dir} lacks frontend/ or backend/ "
                          f"with a package.json")

    if port is not None and not _port_free(port):
        raise VerifyError(f"requested verification port {port} is busy; "
                          f"refusing to kill its owner")
    if port is None:
        port = free_port(exclude=grading_port)

    base_url = f"http://127.0.0.1:{port}"
    # Outside `.arc`: Express/send refuses files under dot-folders, which made every SPA
    # deep link 404 in our checks although the same app served it from a normal folder.
    copy_dir = Path(tempfile.mkdtemp(prefix="arc-verify-"))

    report: dict = {"schemaVersion": 1, "sourceHash": "", "testsHash": None,
                    "build": "not_run", "startup": "not_run",
                    "acceptance": "not_run", "passed": 0, "failed": 0,
                    "skipped": 0, "total": 0, "tests": [], "error": None,
                    "elapsedMs": 0}
    build_log = _Log(report_dir / "logs" / "build.log")
    startup_log = _Log(report_dir / "logs" / "startup.log")
    acceptance_log = _Log(report_dir / "logs" / "acceptance.log")
    backend_proc: _BackendProcess | None = None
    propagating = False

    def write_report() -> None:
        report["elapsedMs"] = int((time.monotonic() - started) * 1000)
        atomic_json(report_dir / "verification.json", report)

    try:
        report["sourceHash"] = source_hash(output_dir,
                                           extra_exclude=SOURCE_HASH_EXTRA)
        _copy_tree(frontend_dir, copy_dir / "frontend")
        _copy_tree(backend_dir, copy_dir / "backend")

        import_failure = _check_frontend_imports(copy_dir / "frontend",
                                                 warn=build_log.push)
        if import_failure is not None:
            report["build"] = "failed"
            report["error"] = import_failure
            write_report()
            return report

        try:
            frontend_install = _ensure_installed(copy_dir / "frontend", deadline,
                                                 build_log)
            backend_install = _ensure_installed(copy_dir / "backend", deadline,
                                                build_log)
        except VerifyError as exc:
            # a broken dependency is a build error for the agent to fix, never a run failure
            report["build"] = "failed"
            report["error"] = f"dependency install failed: {str(exc)[-1500:]}"
            write_report()
            return report
        _link_dir(frontend_install, copy_dir / "frontend" / "node_modules")
        _link_dir(backend_install, copy_dir / "backend" / "node_modules")

        build_result = _run([_npm_bin(), "run", "build"],
                            cwd=copy_dir / "frontend", deadline=deadline,
                            log=build_log)
        if build_result.code != 0:
            report["build"] = "failed"
            reason = "timeout" if build_result.timed_out \
                else f"exit {build_result.code}"
            report["error"] = f"frontend build failed ({reason}): " \
                              f"{build_result.tail()}"
            write_report()
            return report

        syntax_failure = _check_backend_syntax(copy_dir / "backend", deadline,
                                               build_log)
        if syntax_failure is not None:
            report["build"] = "failed"
            report["error"] = syntax_failure
            write_report()
            return report
        report["build"] = "passed"

        if build_only and not start_backend:
            report["startup"] = "not_run"
            report["acceptance"] = "not_run"
            write_report()
            return report

        backend_proc = _BackendProcess(
            start_process([_npm_bin(), "run", "start"],
                          cwd=copy_dir / "backend", env={"PORT": str(port)}),
            startup_log)
        remaining = max(0.1, deadline - time.monotonic())
        ready = _wait_http(base_url, min(MAX_STARTUP_WAIT_SECONDS, remaining),
                           backend_proc)
        if not ready:
            report["startup"] = "failed"
            report["error"] = "backend did not become HTTP-ready: " \
                              + startup_log.tail()
            write_report()
            return report
        report["startup"] = "passed"
        if build_only:
            report["acceptance"] = "not_run"
            write_report()
            return report

        if screenshot_routes:
            try:
                report["screenshots"] = _capture_screenshots(
                    copy_dir, base_url, list(screenshot_routes),
                    report_dir / "screenshots", deadline, acceptance_log)
            except (VerifyError, OSError, ValueError) as exc:
                report["screenshotError"] = str(exc)[:500]

        resolved_tests = _resolve_tests_dir(tests_dir)
        if resolved_tests is None:
            report["acceptance"] = "not_run"
            report["serverErrors"] = server_errors(startup_log.text())
            write_report()
            return report

        _copy_tree(resolved_tests, copy_dir / "tests")
        spec_files = [file for file in _walk_files(copy_dir / "tests",
                                                   frozenset())
                      if SPEC_PATTERN.search(file.name)]
        for spec in spec_files:
            _dedupe_test_titles(spec)
            _unique_titles(spec)
        for script in _walk_files(copy_dir / "tests", frozenset()):
            if script.suffix in (".js", ".cjs", ".mjs", ".ts"):
                _point_at(script, base_url)
        if not spec_files:
            report["acceptance"] = "unavailable"
            report["error"] = "no acceptance specs found in the tests directory"
            write_report()
            return report
        report["testsHash"] = source_hash(resolved_tests)

        grader_dir = PROJECT_DIR / "grader"
        if not (grader_dir / "package.json").exists():
            report["acceptance"] = "unavailable"
            report["error"] = f"bundled grader/ package is missing at {grader_dir}"
            write_report()
            return report
        node = resolve_node_bin(deadline)
        grader_node_modules = _ensure_installed(grader_dir, deadline,
                                                acceptance_log)
        _chromium_executable(node, grader_node_modules, deadline, acceptance_log)
        _link_dir(grader_node_modules, copy_dir / "node_modules")
        report["homePage"] = _probe_home_page(node, copy_dir, base_url, deadline, acceptance_log)

        results_path = copy_dir / "results.json"
        config = copy_dir / "playwright.config.cjs"
        config.write_text(
            "'use strict';\n"
            "const { defineConfig } = require('@playwright/test');\n"
            "module.exports = defineConfig({\n"
            "  testDir: './tests',\n"
            "  timeout: 10000,\n"
            "  expect: { timeout: 10000 },\n"
            "  workers: 1,\n"
            + (f"  globalTimeout: {int(max(10.0, suite_timeout_seconds) * 1000)},\n"
               if suite_timeout_seconds else "")
            +
            "  retries: 0,\n"
            "  fullyParallel: false,\n"
            "  reporter: [['list'], ['json', { outputFile: "
            + json.dumps(results_path.as_posix()) + " }]],\n"
            "  use: { baseURL: " + json.dumps(base_url)
            + ", trace: 'retain-on-failure', screenshot: 'only-on-failure',"
              " video: 'off' },\n"
            "  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],\n"
            "  outputDir: './test-results',\n"
            "});\n", encoding="utf-8")

        test_result = _run(
            [node, str(grader_node_modules / "@playwright" / "test" / "cli.js"),
             "test", "--config", str(config)],
            cwd=copy_dir, deadline=deadline,
            env={"E2E_BASE_URL": base_url, "PLAYWRIGHT_BASE_URL": base_url,
                 "ARC_WEB_BASE_URL": base_url},
            log=acceptance_log)
        if test_result.timed_out or not results_path.exists():
            report["acceptance"] = "failed"
            reason = "timeout" if test_result.timed_out \
                else f"exit {test_result.code}"
            report["error"] = f"acceptance run did not produce a report " \
                              f"({reason}): {test_result.tail()}"
            write_report()
            return report

        if source_hash(resolved_tests) != report["testsHash"]:
            report["acceptance"] = "failed"
            report["error"] = "test files were modified during the acceptance run"
            write_report()
            return report

        try:
            playwright_report = json.loads(results_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report["acceptance"] = "failed"
            report["error"] = "acceptance report is not valid JSON"
            write_report()
            return report

        report["tests"] = map_playwright_report(
            playwright_report, known_requirement_ids, copy_dir / "tests")
        acceptance_log_text = ""
        try:
            acceptance_log_text = (report_dir / "logs" / "acceptance.log").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            acceptance_log_text = ""
        _enrich_errors_from_log(report["tests"], acceptance_log_text)
        report["total"] = len(report["tests"])
        report["passed"] = sum(1 for row in report["tests"]
                               if row["status"] == "passed")
        report["skipped"] = sum(1 for row in report["tests"]
                                if row["status"] == "skipped")
        report["failed"] = report["total"] - report["passed"] - report["skipped"]
        if report["total"] == 0:
            report["acceptance"] = "failed"
            report["error"] = "acceptance collected zero tests (collection error)"
        elif report["failed"] > 0:
            report["acceptance"] = "failed"
        elif report["skipped"] > 0:
            report["acceptance"] = "failed"
            report["error"] = "acceptance contains skipped tests"
        else:
            report["acceptance"] = "passed"
        report["serverErrors"] = server_errors(startup_log.text())

        if (copy_dir / "test-results").is_dir():
            _remove_tree(report_dir / "test-results")
            shutil.copytree(copy_dir / "test-results",
                            report_dir / "test-results")
        write_report()
        return report
    except Exception as exc:
        if backend_proc is not None:
            backend_proc.stop(deadline)
        propagating = True
        report["error"] = str(exc)
        write_report()
        raise
    finally:
        if backend_proc is not None:
            backend_proc.stop(deadline)
        for link in (copy_dir / "node_modules", copy_dir / "frontend" / "node_modules",
                     copy_dir / "backend" / "node_modules"):
            _remove_tree(link)
        _remove_tree(copy_dir)
        if report.get("sourceHash") and source_hash(
                output_dir, extra_exclude=SOURCE_HASH_EXTRA) != report["sourceHash"]:
            report["error"] = "delivered output_dir changed during verification"
            if report["acceptance"] == "passed":
                report["acceptance"] = "failed"
            write_report()
        if not propagating:
            try:
                report_verification_errors(
                    report, command=command, run_id=run_id,
                    report_path=str(report_dir / "verification.json"))
            except Exception:
                pass
