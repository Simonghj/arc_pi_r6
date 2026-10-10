from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .errorlog import redact
from .processes import free_port, resolve_node_bin, start_process, stop_process

PROJECT_DIR = Path(__file__).resolve().parents[1]
BUNDLE_PATH = PROJECT_DIR / 'bridge' / 'dist' / 'agent.mjs'
START_TIMEOUT_SECONDS = 30.0
GRACEFUL_CLOSE_SECONDS = 5.0
SETTLE_GRACE_SECONDS = 15.0
COMPACT_RATIO = 0.8
_ADDITIVE_TOTALS = ('attempts', 'inputTokens', 'outputTokens', 'cacheReadTokens',
                    'cacheWriteTokens', 'reasoningTokens', 'estimatedTokens')
COMPACT_SECONDS = 150
COMPACT_PROMPT = (
    "Handoff: this session ends now and a fresh session continues the project with only your "
    "note. Without calling any tools, write the handoff: implemented requirement ids and their "
    "routes, file map (path -> purpose), data model and seed data, API endpoints, conventions "
    "(exact labels, roles, shared helpers in review-tests/helpers.js), what the last batch "
    "finished and what still fails. Plain text, at most 60 lines.")

EMPTY_TOTALS = {
    'attempts': 0,
    'inputTokens': 0,
    'outputTokens': 0,
    'cacheReadTokens': 0,
    'cacheWriteTokens': 0,
    'reasoningTokens': 0,
    'estimatedTokens': 0,
    'usageComplete': True,
    'cost': None,
    'currency': None,
}


class BridgeError(RuntimeError):
    """Fatal bridge failure: the child is dead or the protocol broke."""

    def __init__(self, message: str, code: str = 'protocol'):
        super().__init__(message)
        self.code = code


class PiBridge:
    def __init__(self, evidence_dir: Path | str | None = None):
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self._proc: subprocess.Popen | None = None
        self._last_proc: subprocess.Popen | None = None
        self._deadline = 0.0
        self._secret = ''
        self._events: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._seq = 0
        self._ready_events: dict[str, threading.Event] = {}
        self._close_id: str | None = None
        self._closed_event: threading.Event | None = None
        self._active_id: str | None = None
        self._abort_kind: str | None = None
        self._totals = dict(EMPTY_TOTALS)
        self._carried: dict = {}
        self._start_args: dict | None = None
        self._last_message: dict | None = None
        self._fatal: str | None = None
        self._fatal_code = 'protocol'
        self._closing = False
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()
        self._context_overhead_bytes: int | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

    # life

    def start(self, config, workspace, read_roots, system_prompt, deadline, *,
              node_bin: str | None = None) -> None:
        """Spawn the bundled engine and wait for its init acknowledgement."""
        if self._proc is not None:
            raise RuntimeError('PiBridge already started')
        self._start_args = dict(config=config, workspace=workspace, read_roots=read_roots,
                                system_prompt=system_prompt, deadline=deadline, node_bin=node_bin)
        if not BUNDLE_PATH.is_file():
            raise RuntimeError(f'bridge bundle missing: {BUNDLE_PATH} (run: node bridge/build.mjs)')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Global deadline exhausted before bridge start')
        if node_bin is None:
            node_bin = resolve_node_bin(deadline)

        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            spill_dir = self.evidence_dir / 'tool-spills'
        else:
            spill_dir = Path(tempfile.mkdtemp(prefix='arc-pi-spill-'))
        spill_dir.mkdir(parents=True, exist_ok=True)

        tool_port = free_port(exclude=3000)
        roots = [Path(root).resolve() for root in read_roots]
        roots.append(spill_dir.resolve())  # explicit spill read root
        wire_config = config.bridge_config(tool_port, spill_dir)
        self._secret = str(config.openai_api_key)
        self._deadline = deadline
        self._fatal = None
        self._closing = False

        proc = start_process(
            [str(node_bin), str(BUNDLE_PATH)],
            cwd=PROJECT_DIR,
            env={'OPENAI_API_KEY': config.openai_api_key},
            stdin=subprocess.PIPE,
        )
        self._proc = proc
        self._reader = threading.Thread(target=self._read_stdout, name='bridge-stdout', daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, name='bridge-stderr', daemon=True)
        self._reader.start()
        self._stderr_reader.start()

        init_id = self._new_id()
        ack = threading.Event()
        self._ready_events[init_id] = ack
        remaining_ms = max(1, int(remaining * 1000))
        init = {
            'v': 1, 'id': init_id, 'type': 'init',
            'config': wire_config,
            'workspace': str(Path(workspace).resolve()),
            'readRoots': [str(root) for root in roots],
            'systemPrompt': str(system_prompt),
            'deadlineEpochMs': int(time.time() * 1000) + remaining_ms,
            'remainingMs': remaining_ms,
        }
        self._record('out', init)
        start_deadline = time.monotonic() + min(START_TIMEOUT_SECONDS, max(1.0, remaining))
        try:
            self._send(init)
            while time.monotonic() < start_deadline:
                if self._fatal:
                    raise BridgeError(self._fatal, self._fatal_code)
                if ack.wait(timeout=0.2):
                    return
            raise BridgeError('bridge did not acknowledge init within the deadline')
        except BaseException:
            self._fail_process()
            raise
        finally:
            self._ready_events.pop(init_id, None)

    def prompt(self, text: str, phase_deadline: float, reasoning: str | None = None) -> dict:
        """Send one prompt; ``reasoning`` (off/low/medium/high/max) overrides PI_REASONING for it."""
        if self._fatal:
            raise BridgeError(self._fatal, self._fatal_code)
        if self._proc is None:
            raise RuntimeError('PiBridge is closed or not started')
        if self._active_id is not None:
            raise RuntimeError('a bridge prompt is already active')
        hard = min(phase_deadline, self._deadline)
        if hard - time.monotonic() <= 0:
            return {'summary': '', 'stopReason': 'budget', 'usage': dict(self._totals),
                    'errorCode': None, 'errorMessage': 'phase/global deadline exhausted before prompt dispatch'}
        prompt_id = self._new_id()
        phase_ms = max(1, int((hard - time.monotonic()) * 1000))
        command = {
            'v': 1, 'id': prompt_id, 'type': 'prompt',
            'text': str(text),
            'phaseDeadlineEpochMs': int(time.time() * 1000) + phase_ms,
        }
        if reasoning is not None:
            command['reasoning'] = reasoning
        self._active_id = prompt_id
        self._abort_kind = None
        self._record('out', command)
        try:
            self._send(command)
        except BaseException:
            self._active_id = None
            raise
        return self._wait_prompt(prompt_id, hard)

    def abort(self) -> None:
        """Interrupt an active prompt immediately (ready-ack only)."""
        if self._proc is None or self._fatal:
            return
        abort_id = self._new_id()
        ack = threading.Event()
        self._ready_events[abort_id] = ack
        self._record('out', {'v': 1, 'id': abort_id, 'type': 'abort'})
        try:
            self._send({'v': 1, 'id': abort_id, 'type': 'abort'})
            ack.wait(timeout=GRACEFUL_CLOSE_SECONDS)
        finally:
            self._ready_events.pop(abort_id, None)
        if not ack.is_set():
            raise BridgeError('bridge did not acknowledge abort')

    def close(self) -> None:
        """Abort if necessary, close the protocol, kill the owned process tree."""
        if self._proc is None:
            return
        self._closing = True
        proc = self._proc
        try:
            if self._active_id is not None and not self._fatal:
                try:
                    self.abort()
                except BridgeError:
                    pass
            if not self._fatal:
                close_id = self._new_id()
                closed = threading.Event()
                self._closed_event = closed
                self._close_id = close_id
                self._record('out', {'v': 1, 'id': close_id, 'type': 'close'})
                try:
                    self._send({'v': 1, 'id': close_id, 'type': 'close'})
                    closed.wait(timeout=GRACEFUL_CLOSE_SECONDS)
                except BridgeError:
                    pass
        finally:
            self._proc = None
            self._last_proc = proc
            stop_process(proc, deadline=time.monotonic() + GRACEFUL_CLOSE_SECONDS)
            self._join_threads()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            self._write_stderr_log()
            self._append_session({'type': 'closed', 'exitCode': proc.poll()})

    # queries

    @property
    def totals(self) -> dict:
        """Usage totals, summed across restarts; lastInputTokens is per session."""
        merged = dict(self._totals)
        for name, value in self._carried.items():
            merged[name] = merged.get(name, 0) + value
        return merged

    def needs_compaction(self, context_tokens: int) -> bool:
        return self._totals.get('lastInputTokens', 0) > COMPACT_RATIO * context_tokens

    def restart(self) -> None:
        """Close this session and start a fresh one with the same arguments."""
        args = dict(self._start_args or {})
        for name in _ADDITIVE_TOTALS:
            self._carried[name] = self._carried.get(name, 0) + int(self._totals.get(name) or 0)
        self.close()
        self._totals = dict(EMPTY_TOTALS)
        self._events = queue.Queue()
        self._fatal = None
        self._context_overhead_bytes = None
        self.start(**args)

    def compact(self, extra: str = '') -> str:
        """Summarize the session, then continue in a fresh one; returns the summary."""
        response = self.prompt(COMPACT_PROMPT + extra, time.monotonic() + COMPACT_SECONDS, reasoning='off')
        summary = str(response.get('summary') or '').strip()
        self.restart()
        return summary

    @property
    def context_overhead_bytes(self) -> int | None:
        return self._context_overhead_bytes

    @property
    def process(self):
        """Live child Popen (None after close), or the last known process."""
        return self._proc if self._proc is not None else self._last_proc

    @property
    def pid(self) -> int | None:
        proc = self.process
        return proc.pid if proc is not None else None

    # internals

    def _new_id(self) -> str:
        self._seq += 1
        return str(self._seq)

    def _send(self, command: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise BridgeError('bridge child is not running')
        line = json.dumps(command, ensure_ascii=False, separators=(',', ':')) + '\n'
        with self._lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise BridgeError(f'bridge stdin write failed: {exc}') from exc

    def _wait_prompt(self, prompt_id: str, hard: float) -> dict:
        deadline_aborted = False
        while True:
            if self._fatal:
                fatal = self._fatal
                self._active_id = None
                raise BridgeError(fatal, self._fatal_code)
            now = time.monotonic()
            if hard - now <= 0 and not deadline_aborted:
                deadline_aborted = True
                self._abort_kind = 'deadline'
                try:
                    self.abort()
                except BridgeError:
                    pass
                continue
            if deadline_aborted:
                wait = min(self._deadline + SETTLE_GRACE_SECONDS, hard + SETTLE_GRACE_SECONDS) - now
                if wait <= 0:
                    self._active_id = None
                    raise BridgeError('bridge did not settle after deadline abort')
            else:
                wait = hard - now
            event = self._get_event(min(wait, 2.0))
            if event is None:
                continue
            etype = event.get('type')
            if etype == 'message':
                self._last_message = event.get('message')
                self._append_session({'type': 'message', 'id': prompt_id, 'message': self._last_message})
            elif etype == 'usage':
                totals = event.get('totals')
                if isinstance(totals, dict):
                    self._totals = dict(totals)
            elif etype == 'done':
                stop_reason = event.get('stopReason')
                result = {
                    'summary': str(event.get('summary', '')),
                    'stopReason': (stop_reason
                                   if stop_reason in ('stop', 'length', 'budget', 'turn_limit')
                                   else 'stop'),
                    'usage': dict(self._totals),
                    'errorCode': None,
                    'errorMessage': None,
                }
                self._append_session({'type': 'done', 'id': prompt_id,
                                      'stopReason': result['stopReason'], 'summary': result['summary']})
                self._active_id = None
                return result
            elif etype == 'error':
                return self._prompt_error(prompt_id, event)
            else:
                self._active_id = None
                raise BridgeError(f'unexpected event during prompt: {etype!r}')

    def _prompt_error(self, prompt_id: str, event: dict) -> dict:
        code = event.get('code') or 'internal'
        message = str(event.get('message', ''))
        self._append_session({'type': 'error', 'id': prompt_id, 'code': code, 'message': message})
        self._active_id = None
        if code == 'cancelled':
            stop_reason = 'budget' if self._abort_kind == 'deadline' else 'cancelled'
            return {'summary': '', 'stopReason': stop_reason, 'usage': dict(self._totals),
                    'errorCode': code, 'errorMessage': message}
        if code in ('provider', 'context'):
            return {'summary': '', 'stopReason': 'error', 'usage': dict(self._totals),
                    'errorCode': code, 'errorMessage': message}
        raise BridgeError(f'bridge reported {code}: {message}', code)

    def _get_event(self, timeout: float):
        try:
            return self._events.get(timeout=max(0.0, timeout))
        except queue.Empty:
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                self._fatal_error(
                    f'bridge child exited while waiting for an event (exit {proc.returncode})'
                    + self._stderr_tail())
            return None

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.rstrip('\n')
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                self._fatal_error(f'malformed bridge event: {self._redact(line[:200])}')
                return
            self._record('in', event)
            self._route(event)
        if self._closing:
            return
        code = proc.poll()
        self._fatal_error(
            'bridge stdout closed unexpectedly'
            + (f' (exit {code})' if code is not None else '')
            + self._stderr_tail())

    def _route(self, event: dict) -> None:
        if not isinstance(event, dict) or event.get('v') != 1 or not isinstance(event.get('id'), str):
            self._fatal_error(f'invalid bridge event envelope: {event}')
            return
        etype = event.get('type')
        eid = event['id']
        if etype == 'ready':
            ack = self._ready_events.get(eid)
            if ack is None:
                self._fatal_error(f'unexpected ready event id {eid!r}')
            else:
                overhead = event.get('contextOverheadBytes')
                if isinstance(overhead, int) and overhead > 0:
                    self._context_overhead_bytes = overhead
                ack.set()
            return
        if etype == 'closed':
            if self._closed_event is not None and eid == self._close_id:
                self._closed_event.set()
            else:
                self._fatal_error(f'unexpected closed event id {eid!r}')
            return
        if etype in ('message', 'usage', 'done', 'error'):
            if eid != self._active_id:
                self._fatal_error(
                    f'{etype} for unknown id {eid!r}'
                    + (f': {event.get("message")}' if etype == 'error' else ''),
                    event.get('code') if etype == 'error' else 'protocol')
                return
            self._events.put(event)
            return
        self._fatal_error(f'unknown bridge event type {etype!r}')

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            with self._stderr_lock:
                self._stderr_lines.append(self._redact(line.rstrip('\n')))
                if len(self._stderr_lines) > 1000:
                    del self._stderr_lines[:500]

    def _fatal_error(self, message: str, code: str = 'protocol') -> None:
        if self._fatal is None:
            self._fatal = message
            self._fatal_code = code
            self._record('fatal', message)

    def _fail_process(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                stop_process(proc, deadline=time.monotonic() + GRACEFUL_CLOSE_SECONDS)
            except Exception:
                pass

    def _join_threads(self) -> None:
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)

    def _stderr_tail(self) -> str:
        with self._stderr_lock:
            if not self._stderr_lines:
                return ''
            return '\n[bridge stderr tail]\n' + '\n'.join(self._stderr_lines[-20:])

    # evidence

    def _redact(self, text: str) -> str:
        if self._secret and len(self._secret) >= 4:
            return redact(text, self._secret)
        return text

    def _append_file(self, name: str, content: str) -> None:
        if self.evidence_dir is None:
            return
        path = self.evidence_dir / name
        with self._lock:
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(content)

    def _record(self, direction: str, payload) -> None:
        record = {'v': 1, 'dir': direction, 'at': round(time.time(), 3), 'payload': payload}
        self._append_file(
            'bridge-events.jsonl',
            self._redact(json.dumps(record, ensure_ascii=False, default=str)) + '\n')

    def _append_session(self, record: dict) -> None:
        self._append_file(
            'bridge-session.jsonl',
            self._redact(json.dumps(record, ensure_ascii=False, default=str)) + '\n')

    def _write_stderr_log(self) -> None:
        if self.evidence_dir is None:
            return
        with self._stderr_lock:
            lines = list(self._stderr_lines)
        if not lines:
            return
        with self._lock:
            with open(self.evidence_dir / 'bridge-stderr.log', 'a', encoding='utf-8') as handle:
                handle.write('\n'.join(lines) + '\n')
