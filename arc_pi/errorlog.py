from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_LOG_PATH = PROJECT_DIR / "logs" / "errors.jsonl"

_REDACTION_MARK = "<redacted>"

_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_append_lock = threading.Lock()
_vt_enable_attempted = False

_ERROR_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Limitless, arc_pi_python: errors</title>
<style>
body {{ background: #ffffff; color: #1a1a1a; font-family: sans-serif; margin: 2em; }}
h1 {{ font-size: 1.4em; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #999999; padding: 0.4em 0.6em; text-align: left; vertical-align: top; }}
th {{ background: #eeeeee; }}
td.error {{ color: #b00020; }}
td.malformed, td.empty {{ color: #555555; font-style: italic; }}
</style>
</head>
<body>
<h1>Limitless, arc_pi_python: errors</h1>
<table>
<thead><tr><th>Timestamp</th><th>Command</th><th>Phase</th><th>Run</th><th>Report</th><th>Test</th><th>Message</th></tr></thead>
<tbody>
{body}
</tbody>
</table>
</body>
</html>
"""


# helpers


def redact(text: str, secret: str) -> str:
    if secret:
        return text.replace(secret, _REDACTION_MARK)
    return text


def redact_secrets(text: str) -> str:
    """Redact both model credentials without leaking their values."""
    value = text
    for name in ("OPENAI_API_KEY", "VISUAL_API_KEY"):
        value = redact(value, os.environ.get(name, ""))
    return value


def _sanitize(text: str) -> str:
    """Strip ANSI escape sequences and control characters from ``text``."""
    return _CONTROL_RE.sub("", _ANSI_RE.sub("", text))


def _error_log_path() -> Path:
    raw = os.environ.get("ARCBENCH_ERROR_LOG")
    if raw:
        return Path(raw)
    return DEFAULT_LOG_PATH


def _enable_vt_windows() -> bool:
    global _vt_enable_attempted
    if _vt_enable_attempted:
        return True
    _vt_enable_attempted = True
    if os.name != "nt":
        return False
    try:
        import ctypes
        import msvcrt
    except (ImportError, OSError):
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = msvcrt.get_osfhandle(sys.stderr.fileno())
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _stderr_color_enabled() -> bool:
    """ANSI red on stderr only for a real TTY without color opt-outs."""
    isatty = getattr(sys.stderr, "isatty", None)
    if not callable(isatty):
        return False
    try:
        tty = isatty()
    except Exception:
        tty = False
    if not tty:
        return False
    if os.environ.get("NO_COLOR") is not None:  # presence incl. empty disables
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if os.name == "nt":
        return _enable_vt_windows()
    return True


def _diagnostic_row(line_number: int) -> str:
    return ('<tr><td class="malformed" colspan="7">'
            f"Unreadable error record at line {line_number}"
            "</td></tr>")


def _cell(value) -> str:
    return "" if value is None else str(value)


# public API


def report_error(message: str, *, command: str, phase: str,
                 run_id: str | None = None, report_path: str | None = None,
                 test_id: str | None = None) -> None:
    text = _sanitize(redact_secrets(str(message)))

    try:
        if _stderr_color_enabled():
            print(f"\x1b[31m{text}\x1b[0m", file=sys.stderr)
        else:
            print(text, file=sys.stderr)
    except Exception:
        pass

    record = {
        "schemaVersion": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": str(command),
        "phase": str(phase),
        "runId": run_id,
        "reportPath": report_path,
        "testId": test_id,
        "message": text,
    }
    path = _error_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _append_lock:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        try:
            print(f"warning: could not append to error log {path}",
                  file=sys.stderr)
        except Exception:
            pass


def report_verification_errors(report: dict, *, command: str,
                               report_path: str,
                               run_id: str | None = None) -> None:
    if not isinstance(report, dict):
        return

    def emit(message: str, test_id: str | None = None) -> None:
        report_error(message, command=command, phase="verify",
                     run_id=run_id, report_path=report_path, test_id=test_id)

    error = str(report.get("error") or "").strip()
    build = report.get("build")
    startup = report.get("startup")
    acceptance = report.get("acceptance")

    if build == "failed":
        emit(f"build failed: {error or 'frontend build failed'}")
    if startup == "failed":
        emit(f"startup failed: {error or 'backend did not become HTTP-ready'}")

    if acceptance == "unavailable":
        emit(f"acceptance unverified: {error or 'no acceptance suite ran'}")
        return
    if acceptance == "not_run":
        return  # build_only or aborted before acceptance: not an error

    emitted_rows = 0
    for row in report.get("tests") or []:
        if not isinstance(row, dict):
            continue
        status = row.get("status")
        if status in ("passed", "not_run"):
            continue
        test_id = row.get("id")
        label = str(row.get("file") or "").strip()
        title = str(row.get("title") or "").strip()
        if title:
            label = f"{label} :: {title}" if label else title
        if not label:
            label = str(test_id)
        message = f"acceptance test {label} [{status}]"
        row_error = str(row.get("error") or "").strip()
        if row_error:
            message += f": {row_error}"
        emit(message, test_id=str(test_id) if test_id is not None else None)
        emitted_rows += 1

    if acceptance == "failed" and emitted_rows == 0:
        emit(f"acceptance failed: {error or 'no failing test details available'}")


def render_error_log(jsonl_path: Path, html_path: Path) -> None:
    from html import escape

    jsonl_path = Path(jsonl_path)
    html_path = Path(html_path)

    rows: list[str] = []
    if jsonl_path.is_file():
        try:
            lines = jsonl_path.read_text(encoding="utf-8",
                                         errors="replace").splitlines()
        except OSError:
            lines = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                rows.append(_diagnostic_row(index))
                continue
            if not isinstance(record, dict):
                rows.append(_diagnostic_row(index))
                continue
            message = _sanitize(str(record.get("message") or ""))
            cells = "".join(
                f"<td>{escape(_cell(record.get(key)))}</td>"
                for key in ("timestamp", "command", "phase", "runId",
                            "reportPath", "testId"))
            rows.append(
                f"<tr>{cells}<td class=\"error\">{escape(message)}</td></tr>")

    body = ("\n".join(rows) if rows
            else '<tr><td class="empty" colspan="7">No errors recorded.</td></tr>')
    document = _ERROR_PAGE_TEMPLATE.format(body=body)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = html_path.with_name(html_path.name + ".tmp")
    tmp_path.write_text(document, encoding="utf-8")
    os.replace(tmp_path, html_path)
