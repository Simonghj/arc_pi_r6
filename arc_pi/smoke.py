from __future__ import annotations

import dataclasses
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
ARTIFACTS_DIR = PROJECT_DIR / '.cache'


class SmokeError(Exception):
    """Raised when a smoke proof fails."""



def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}


class ScriptedModelServer:
    def __init__(self, turn_fn=None, *, status: int = 200, delay: float = 0.0,
                 retry_after: str | None = None, usage_fn=None):
        self.turn_fn = turn_fn
        self.status = status
        self.delay = delay
        self.retry_after = retry_after
        self.usage_fn = usage_fn
        self.requests: list[dict] = []
        self.failures: list[str] = []
        self.expect_system: list[str] = []
        self.expect_last_user: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self):
                try:
                    length = int(self.headers.get('content-length') or 0)
                    body = json.loads(self.rfile.read(length).decode('utf-8'))
                except Exception:
                    self.send_response(400)
                    self.end_headers()
                    return
                if self.path != '/v1/chat/completions':
                    self.send_response(404)
                    self.end_headers()
                    return
                with outer.lock:
                    outer.requests.append(body)
                    outer._validate(body)
                if outer.delay > 0:
                    time.sleep(outer.delay)
                if outer.status != 200:
                    self.send_response(outer.status)
                    if outer.retry_after:
                        self.send_header('retry-after', outer.retry_after)
                    self.send_header('content-type', 'application/json')
                    payload = {'error': {'message': 'scripted failure',
                                         'type': 'scripted_error',
                                         'code': 'scripted'}}
                    data = json.dumps(payload).encode('utf-8')
                    self.send_header('content-length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                try:
                    message = outer.turn_fn(body.get('messages') or [], body) \
                        if outer.turn_fn else None
                except Exception as exc:  # recipe bug must fail the proof loudly
                    with outer.lock:
                        outer.failures.append(f'turn_fn raised: {exc}')
                    self.send_response(500)
                    self.send_header('content-type', 'application/json')
                    payload = {'error': {'message': str(exc),
                                         'type': 'scripted_error',
                                         'code': 'scripted'}}
                    data = json.dumps(payload).encode('utf-8')
                    self.send_header('content-length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                payload = outer._completion(body, message or {})
                data = json.dumps(payload).encode('utf-8')
                self.send_response(200)
                self.send_header('content-type', 'application/json')
                self.send_header('content-length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        class _QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                error = sys.exc_info()[1]
                if isinstance(error, (ConnectionResetError, BrokenPipeError,
                                      ConnectionAbortedError, TimeoutError)):
                    return
                super().handle_error(request, client_address)

        self.lock = threading.Lock()
        self._server = _QuietServer(('127.0.0.1', 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        self.port: int = self._server.server_address[1]
        self.base_url: str = f'http://127.0.0.1:{self.port}/v1'

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # request validation

    def _validate(self, body: dict) -> None:
        if body.get('stream') is True:
            self.failures.append('request sent stream:true; provider must '
                                 'send stream:false')
        messages = body.get('messages')
        if not isinstance(messages, list) or not messages:
            self.failures.append('request has no messages')
            return
        system = messages[0]
        if system.get('role') != 'system':
            self.failures.append(f'first message role is '
                                 f'{system.get("role")!r}, expected system')
        else:
            text = _text_of(system.get('content'))
            for needle in self.expect_system:
                if needle not in text:
                    self.failures.append(f'system message missing {needle!r}')
        user_messages = [m for m in messages if m.get('role') == 'user']
        last_user_text = _text_of(user_messages[-1].get('content')) \
            if user_messages else ''
        for needle in self.expect_last_user:
            if needle not in last_user_text:
                self.failures.append(f'newest user message missing {needle!r}')
        self._validate_tool_ids(messages)

    def _validate_tool_ids(self, messages: list) -> None:
        seen: set[str] = set()
        for message in messages:
            role = message.get('role')
            if role == 'assistant':
                for call in message.get('tool_calls') or []:
                    if isinstance(call, dict) and isinstance(call.get('id'), str):
                        seen.add(call['id'])
            elif role == 'tool':
                call_id = message.get('tool_call_id')
                if not call_id:
                    self.failures.append('tool result without tool_call_id')
                elif call_id not in seen:
                    self.failures.append(
                        f'tool result references unseen id {call_id!r}')

    def _completion(self, body: dict, message: dict) -> dict:
        tool_calls = message.get('tool_calls') or []
        return {
            'id': f'chatcmpl-smoke-{len(self.requests)}',
            'object': 'chat.completions',
            'created': int(time.time()),
            'model': body.get('model') or 'deepseek-v4-flash',
            'choices': [{
                'index': 0,
                'message': {
                    'role': 'assistant',
                    'content': message.get('content') or '',
                    'tool_calls': tool_calls,
                },
                'finish_reason': 'tool_calls' if tool_calls else 'stop',
            }],
            'usage': self.usage_fn(body) if self.usage_fn else {
                'prompt_tokens': 10,
                'completion_tokens': 5,
                'total_tokens': 15,
                'prompt_cache_hit_tokens': 2,
                'prompt_cache_miss_tokens': 1,
            },
        }


def _text_of(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ''
    return json.dumps(value, default=str)



def smoke_config(server: ScriptedModelServer, **overrides):
    """A Config for a loopback smoke child; never touches real credentials."""
    from arc_pi import config
    base = config.load_config(allow_missing_key=True)
    git_bash = Path('C:/Program Files/Git/bin/bash.exe')
    fields = {
        'openai_api_key': 'smoke-key',
        'openai_base_url': server.base_url,
        'pi_reasoning': 'off',
        'pi_time_budget_seconds': 300,
        'pi_max_model_calls': 40,
        'pi_max_output_tokens': 4096,
        'pi_context_bytes': 131072,
        'pi_repair_rounds': 0,
        'pi_bash_path': str(git_bash) if git_bash.exists() else None,
        'pi_smoke_port': None,
    }
    fields.update(overrides)
    return dataclasses.replace(base, **fields)


def _node_bin(deadline: float) -> str:
    from arc_pi import processes
    return processes.resolve_node_bin(deadline)


def _prompt_outcome(bridge, text: str, phase_deadline: float):
    """(ok, error_code, summary, elapsed): prompt() raises only on fatal."""
    started = time.monotonic()
    try:
        result = bridge.prompt(text, phase_deadline)
    except Exception as exc:
        return (False, getattr(exc, 'code', None), str(exc),
                time.monotonic() - started)
    error_code = result.get('errorCode')
    stop_reason = result.get('stopReason')
    ok = error_code is None and stop_reason in ('stop', 'length')
    return (ok, error_code, result.get('summary'), time.monotonic() - started)


def _prompt_into(outcome: dict, bridge, text: str, phase_deadline: float) -> None:
    """Record a prompt outcome into a dict from a worker thread."""
    outcome['result'] = _prompt_outcome(bridge, text, phase_deadline)


def _safe_close(bridge) -> None:
    """Close a bridge that may already be dead/aborted without raising."""
    try:
        bridge.close()
    except Exception:
        pass


def _npm(argv: list[str], cwd: Path, deadline: float):
    from arc_pi import processes
    command = ['cmd', '/c', 'npm', *argv] if os.name == 'nt' else ['npm', *argv]
    result = processes.run_command(command, cwd=cwd, deadline=deadline)
    if result.returncode != 0:
        raise SmokeError(f'npm {" ".join(argv)} failed in {cwd} '
                         f'rc={result.returncode}\n'
                         f'--- stdout ---\n{result.stdout[-2000:]}\n'
                         f'--- stderr ---\n{result.stderr[-2000:]}')
    return result


def _wait_http_ready(url: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise SmokeError(f'{url} never became HTTP-ready')


def _stop_backend(proc) -> None:
    """Stop an owned backend and close its pipes (no ResourceWarnings)."""
    from arc_pi import processes
    processes.stop_process(proc, deadline=time.monotonic() + 10)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def _wait_backend_ready(proc, url: str, deadline: float) -> None:
    """HTTP-ready, failing fast when the owned backend child exits early."""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SmokeError(f'backend exited early (rc={proc.returncode}) '
                             f'before {url} became ready')
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise SmokeError(f'{url} never became HTTP-ready')



servers: list[ScriptedModelServer] = []


def _protocol_smoke(tmp: Path, deadline: float) -> dict:
    from arc_pi import bridge as bridge_module
    from arc_pi import processes

    dist = PROJECT_DIR / 'bridge' / 'dist' / 'agent.mjs'
    if not dist.exists():
        raise SmokeError(f'missing {dist}; restore the shipped bundle '
                         '(this checkout has no bridge/build.mjs)')
    example_dir = PROJECT_DIR / 'example'
    checks: list[dict] = []

    def check(name: str, condition: bool, detail: str = ''):
        checks.append({'name': name, 'passed': bool(condition),
                       'detail': detail})

    def start_bridge(server, workspace: Path, system_prompt: str,
                     evidence_dir: Path, **overrides):
        evidence_dir.mkdir(parents=True, exist_ok=True)
        bridge = bridge_module.PiBridge(evidence_dir=evidence_dir)
        bridge.start(smoke_config(server, **overrides), workspace,
                     [example_dir], system_prompt, deadline)
        return bridge

    def settle_failure(name: str, bridge, server, expect_code=None):
        """One prompt must terminate with a model-level error, bounded time."""
        ok, code, summary, elapsed = _prompt_outcome(bridge, f'fail {name}',
                                                     time.monotonic() + 120)
        if ok:
            raise SmokeError(f'{name}: prompt succeeded where it must fail: '
                             f'{summary!r}')
        check(f'{name}: prompt terminated with error', True,
              f'code={code} in {elapsed:.1f}s')
        if expect_code:
            check(f'{name}: error code is {expect_code}', code == expect_code,
                  f'code={code!r}')
        check(f'{name}: settled promptly (<45s)', elapsed < 45,
              f'{elapsed:.1f}s')

    # A. happy path: two batches on one child
    server = ScriptedModelServer()
    servers.append(server)
    token = secrets.token_hex(4)
    sys_marker = f'SMOKE_SYS_{token}'
    skill_marker = f'SMOKE_SKILL_{token}'
    b1_marker = f'SMOKE_BATCH1_{token}'
    b2_marker = f'SMOKE_BATCH2_{token}'
    server.expect_system = [sys_marker, skill_marker]
    server.expect_last_user = [b1_marker]
    ws = tmp / 'happy' / 'ws'
    ws.mkdir(parents=True)
    system_prompt = (
        f'{sys_marker}\n'
        f'Loaded skill web-implementation ({skill_marker}): write small '
        f'readable functions, no fake backend data, exact routes and labels. '
        f'Each user prompt carries a batch marker; execute tool calls as the '
        f'smoke driver instructs.')
    happy_bridge = start_bridge(server, ws, system_prompt,
                                tmp / 'happy' / 'evidence')
    try:
        state = {'turns': 0}

        def happy_turn(messages, body):
            state['turns'] += 1
            n = state['turns']
            if n == 1:
                return {'tool_calls': [tool_call(
                    'smoke_call_1', 'read', {'path': 'missing.txt'})]}
            if n == 2:
                tool_results = [m for m in messages if m.get('role') == 'tool']
                text = _text_of(tool_results[-1].get('content'))
                low = text.lower()
                if 'missing.txt' not in text and 'no such file' not in low \
                        and 'enoent' not in low and 'error' not in low:
                    server.failures.append(
                        f'expected read-error tool result, got: {text[:200]!r}')
                return {'tool_calls': [tool_call(
                    'smoke_call_2', 'write',
                    {'path': 'notes/smoke.txt', 'content': 'hello smoke'})]}
            if n == 3:
                return {'tool_calls': [tool_call(
                    'smoke_call_3', 'read', {'path': 'notes/smoke.txt'})]}
            if n == 4:
                return {'tool_calls': [tool_call(
                    'smoke_call_4', 'edit',
                    {'path': 'notes/smoke.txt',
                     'edits': [{'oldText': 'hello smoke',
                                'newText': 'hello smoke edited'}]})]}
            if n == 5:
                return {'tool_calls': [tool_call(
                    'smoke_call_5', 'bash',
                    {'command': 'node -e "console.log('
                                "JSON.stringify({smoke:true}))\" "
                                '> bash-out.json'})]}
            if n == 6:  # batch 1 done
                return {'content': 'Batch 1 complete: wrote, edited and '
                                   'exercised the workspace.'}
            if n == 7:  # first request of batch 2
                body_text = json.dumps(messages, default=str)
                if 'hello smoke edited' not in body_text:
                    server.failures.append(
                        'batch 2 request lost batch 1 tool results '
                        '(child not reused?)')
                return {'tool_calls': [tool_call(
                    'smoke_call_6', 'read', {'path': 'notes/smoke.txt'})]}
            return {'content': 'Batch 2 complete.'}

        server.turn_fn = happy_turn

        ok, code, summary, elapsed = _prompt_outcome(
            happy_bridge, f'{b1_marker}: write, read, edit, bash and report '
                          f'a tool error', time.monotonic() + 60)
        check('batch 1 completed', ok, f'code={code} summary={summary!r}')
        check('batch 1 real write effect',
              (ws / 'notes' / 'smoke.txt').read_text(encoding='utf-8')
              == 'hello smoke edited')
        check('batch 1 real bash effect',
              (ws / 'bash-out.json').exists())

        server.expect_last_user = [b2_marker]
        ok, code, summary, elapsed = _prompt_outcome(
            happy_bridge, f'{b2_marker}: re-read the file batch 1 wrote',
            time.monotonic() + 60)
        check('batch 2 completed on the same child', ok,
              f'code={code} summary={summary!r}')
        check('no repeated scaffold request', state['turns'] == 8,
              f'turns={state["turns"]}')
        check('every request carried system + skill instructions',
              not server.failures, '; '.join(server.failures))
        check('tool-result ids matched assistant tool calls',
              not server.failures, '; '.join(server.failures))

        requests = len(server.requests)
        totals = happy_bridge.totals
        check('cumulative input tokens', totals['inputTokens'] == 10 * requests,
              f'{totals["inputTokens"]} vs {10 * requests}')
        check('cumulative output tokens', totals['outputTokens'] == 5 * requests,
              f'{totals["outputTokens"]} vs {5 * requests}')
        check('cache tokens are subsets, not additions',
              totals['inputTokens'] >= totals['cacheReadTokens']
              + totals['cacheWriteTokens'] and
              totals['cacheReadTokens'] == 2 * requests and
              totals['cacheWriteTokens'] == 1 * requests,
              f'{totals}')
        check('usageComplete with no cost', totals['usageComplete'] is True
              and totals['cost'] is None and totals['currency'] is None,
              f'{totals}')
        check('bridge evidence recorded',
              any((tmp / 'happy' / 'evidence').iterdir()))
    finally:
        _safe_close(happy_bridge)

    # A2. forced context pruning keeps system/instructions
    pruning_server = ScriptedModelServer()
    servers.append(pruning_server)
    p_marker = f'SMOKE_PRUNE_{token}'
    pruning_server.expect_system = [sys_marker, skill_marker]
    pruning_server.expect_last_user = [p_marker]
    prune_state = {'turns': 0}

    def prune_turn(messages, body):
        prune_state['turns'] += 1
        if prune_state['turns'] <= 2:
            char = 'x' if prune_state['turns'] == 1 else 'y'
            return {'tool_calls': [tool_call(
                f'prune_call_{prune_state["turns"]}', 'bash',
                {'command': f'node -e "console.log(\'{char}\'.repeat(7500))"'
                 if prune_state['turns'] == 1
                 else f'node -e "console.log(\'{char}\'.repeat(7500))" > '
                      'prune-out.txt'})]}
        return {'content': 'Pruning batch complete.'}

    pruning_server.turn_fn = prune_turn
    prune_ws = tmp / 'prune' / 'ws'
    prune_ws.mkdir(parents=True)
    prune_bridge = start_bridge(pruning_server, prune_ws, system_prompt,
                                tmp / 'prune' / 'evidence',
                                pi_context_bytes=16384)
    try:
        ok, code, summary, elapsed = _prompt_outcome(
            prune_bridge, f'{p_marker}: generate large tool outputs',
            time.monotonic() + 60)
        check('pruned-context prompt completed', ok,
              f'code={code} summary={summary!r}')
        check('system + skill instructions survived pruning',
              not pruning_server.failures, '; '.join(pruning_server.failures))
    finally:
        _safe_close(prune_bridge)

    # B. malformed tool calls execute no partial effects
    bad_cases = (
        ('truncated tool-call JSON',
         [{'id': 'bad_1', 'type': 'function', 'function': {
             'name': 'write',
             'arguments': '{"path": "notes/evil.txt", "content": "evil'}}]),
        ('duplicate tool-call ids',
         [tool_call('dup_1', 'write', {'path': 'notes/evil.txt',
                                       'content': 'evil'}),
          tool_call('dup_1', 'write', {'path': 'notes/evil2.txt',
                                       'content': 'evil2'})]),
        ('unknown tool name',
         [tool_call('bad_2', 'frobnicate', {'path': 'notes/evil.txt'})]),
        ('tool call without id',
         [{'type': 'function', 'function': {
             'name': 'write',
             'arguments': json.dumps({'path': 'notes/evil.txt',
                                      'content': 'evil'})}}]),
    )
    for case_name, calls in bad_cases:
        bad_server = ScriptedModelServer(turn_fn=lambda messages, body, c=calls:
                                         {'tool_calls': c})
        servers.append(bad_server)
        bad_ws = tmp / 'malformed' / case_name.replace(' ', '-') / 'ws'
        bad_ws.mkdir(parents=True)
        bad_bridge = start_bridge(bad_server, bad_ws, system_prompt,
                                  tmp / 'malformed' / 'evidence')
        try:
            settle_failure(f'malformed response ({case_name})', bad_bridge,
                           bad_server)
            check(f'{case_name}: no partial effects',
                  not (bad_ws / 'notes').exists(),
                  'notes/ was created despite invalid tool call')
        finally:
            _safe_close(bad_bridge)

    # C. 401 fails promptly
    auth_server = ScriptedModelServer(status=401)
    servers.append(auth_server)
    auth_ws = tmp / 'unauth' / 'ws'
    auth_ws.mkdir(parents=True)
    auth_bridge = start_bridge(auth_server, auth_ws, system_prompt,
                               tmp / 'unauth' / 'evidence')
    try:
        settle_failure('401 endpoint', auth_bridge, auth_server)
        check('401 failed without retry delays', len(auth_server.requests) == 1,
              f'{len(auth_server.requests)} requests')
    finally:
        _safe_close(auth_bridge)

    # D. abort settles without hanging
    slow_server = ScriptedModelServer(delay=30)
    servers.append(slow_server)
    slow_ws = tmp / 'abort' / 'ws'
    slow_ws.mkdir(parents=True)
    abort_bridge = start_bridge(slow_server, slow_ws, system_prompt,
                                tmp / 'abort' / 'evidence')
    try:
        outcome: dict = {}
        thread = threading.Thread(target=_prompt_into, args=(
            outcome, abort_bridge, 'abort me', time.monotonic() + 300))
        thread.start()
        time.sleep(1.5)
        aborted_at = time.monotonic()
        abort_error = None
        try:
            abort_bridge.abort()
        except Exception as exc:
            abort_error = str(exc)
        thread.join(timeout=15)
        elapsed = time.monotonic() - aborted_at
        ok = outcome.get('result', (True, None, None, 0.0))[0]
        check('abort settled the active prompt', not thread.is_alive()
              and not ok and abort_error is None,
              f'elapsed={elapsed:.1f}s abortError={abort_error!r}')
        check('abort settled promptly (<10s)', elapsed < 10,
              f'{elapsed:.1f}s')
    finally:
        _safe_close(abort_bridge)

    # E. EOF / child death settle without hanging
    eof_server = ScriptedModelServer(delay=30)
    servers.append(eof_server)
    eof_ws = tmp / 'eof' / 'ws'
    eof_ws.mkdir(parents=True)
    eof_bridge = start_bridge(eof_server, eof_ws, system_prompt,
                              tmp / 'eof' / 'evidence')
    try:
        outcome: dict = {}
        thread = threading.Thread(target=_prompt_into, args=(
            outcome, eof_bridge, 'eof me', time.monotonic() + 300))
        thread.start()
        time.sleep(1.5)
        closed_at = time.monotonic()
        stdin = getattr(getattr(eof_bridge, 'process', None), 'stdin', None)
        if stdin is not None:
            stdin.close()
            thread.join(timeout=15)
            elapsed = time.monotonic() - closed_at
            ok = outcome.get('result', (True, None, None, 0.0))[0]
            check('child stdin EOF settled the prompt',
                  not thread.is_alive() and not ok,
                  f'elapsed={elapsed:.1f}s')
            check('EOF settled promptly (<15s)', elapsed < 15,
                  f'{elapsed:.1f}s')
        else:
            check('child stdin EOF settled the prompt', False,
                  'bridge exposes no stdin pipe; skipping EOF probe')
    finally:
        _safe_close(eof_bridge)

    crash_server = ScriptedModelServer(delay=30)
    servers.append(crash_server)
    crash_ws = tmp / 'crash' / 'ws'
    crash_ws.mkdir(parents=True)
    crash_bridge = start_bridge(crash_server, crash_ws, system_prompt,
                                tmp / 'crash' / 'evidence')
    try:
        outcome: dict = {}
        thread = threading.Thread(target=_prompt_into, args=(
            outcome, crash_bridge, 'crash me', time.monotonic() + 300))
        thread.start()
        time.sleep(1.5)
        killed_at = time.monotonic()
        processes.stop_process(crash_bridge.process,
                               deadline=time.monotonic() + 10)
        thread.join(timeout=15)
        elapsed = time.monotonic() - killed_at
        ok = outcome.get('result', (True, None, None, 0.0))[0]
        check('child death settled the prompt', not thread.is_alive()
              and not ok, f'elapsed={elapsed:.1f}s')
        check('child death settled promptly (<15s)', elapsed < 15,
              f'{elapsed:.1f}s')
    finally:
        _safe_close(crash_bridge)

    # F. call retries and no cumulative token cap
    cap_server = ScriptedModelServer(status=429, retry_after='1')
    servers.append(cap_server)
    cap_ws = tmp / 'cap-calls' / 'ws'
    cap_ws.mkdir(parents=True)
    cap_bridge = start_bridge(cap_server, cap_ws, system_prompt,
                              tmp / 'cap-calls' / 'evidence',
                              pi_max_model_calls=2)
    try:
        settle_failure('call cap with 429 retries', cap_bridge, cap_server)
        check('call cap: no more than cap requests',
              1 <= len(cap_server.requests) <= 2,
              f'{len(cap_server.requests)} requests for cap 2')
        count = len(cap_server.requests)
        time.sleep(1.5)
        check('call cap: no request after cap',
              len(cap_server.requests) == count,
              f'grew from {count} to {len(cap_server.requests)}')
    finally:
        _safe_close(cap_bridge)

    def heavy_usage(body):
        output_tokens = body['max_tokens']
        return {'prompt_tokens': 10, 'completion_tokens': output_tokens,
                'total_tokens': 10 + output_tokens}

    token_server = ScriptedModelServer(lambda messages, body: {'content': 'done'},
                                       usage_fn=heavy_usage)
    servers.append(token_server)
    token_ws = tmp / 'no-token-cap' / 'ws'
    token_ws.mkdir(parents=True)
    token_bridge = start_bridge(token_server, token_ws, system_prompt,
                                tmp / 'no-token-cap' / 'evidence',
                                pi_max_output_tokens=400_000_000_000)
    try:
        outcomes = [_prompt_outcome(token_bridge, f'heavy prompt {index}',
                                    time.monotonic() + 60) for index in range(4)]
        charged = token_bridge.totals['inputTokens'] + token_bridge.totals['outputTokens']
        check('no token cap: every prompt dispatched past 10**12 charged tokens',
              all(outcome[0] for outcome in outcomes)
              and len(token_server.requests) == 4 and charged > 10**12,
              f'{len(token_server.requests)} requests, charged {charged}')
        check('no token cap: max_tokens stays the per-request cap',
              all(body['max_tokens'] == 400_000_000_000
                  for body in token_server.requests),
              str([body['max_tokens'] for body in token_server.requests]))
    finally:
        _safe_close(token_bridge)

    deadline_server = ScriptedModelServer(delay=10)
    servers.append(deadline_server)
    deadline_ws = tmp / 'cap-deadline' / 'ws'
    deadline_ws.mkdir(parents=True)
    deadline_bridge = start_bridge(deadline_server, deadline_ws,
                                   system_prompt, tmp / 'cap-deadline' / 'evidence')
    try:
        started = time.monotonic()
        ok, code, summary, elapsed = _prompt_outcome(
            deadline_bridge, 'deadline me', started + 2)
        check('phase deadline capped the provider wait',
              not ok and elapsed < 20, f'elapsed={elapsed:.1f}s code={code}')
    finally:
        _safe_close(deadline_bridge)

    return {'checks': checks, 'passed': all(c['passed'] for c in checks)}



_FIXTURE_AUTH_JS = r'''const path = require('path');
const { openStore } = require('./store');
const {
  createSessionService, setSessionCookie, clearSessionCookie,
  hashPassword, verifyPassword, SESSION_COOKIE,
} = require('./session');

const DATA_FILE = path.resolve(__dirname, '../data/db.json');

function readCookie(req, name) {
  const header = req.headers.cookie || '';
  for (const part of header.split(';')) {
    const eq = part.indexOf('=');
    if (eq >= 0 && part.slice(0, eq).trim() === name) {
      return part.slice(eq + 1).trim();
    }
  }
  return null;
}

async function openAuthStore() {
  return openStore({
    filePath: DATA_FILE,
    seed: { users: {}, sessions: {} },
  });
}

function mount(app, store) {
  const sessions = createSessionService(store);
  const publicUser = (user) => ({ username: user.username });
  const readUser = (state, username) =>
    (state.users && state.users[username]) || null;

  app.post('/api/register', async (req, res) => {
    const { username, password } = req.body || {};
    if (typeof username !== 'string' || !username.trim() ||
        typeof password !== 'string' || !password) {
      return res.status(400).json({ error: 'username and password are required' });
    }
    const name = username.trim();
    const passwordHash = await hashPassword(password);
    const created = await store.mutate((state) => {
      if (state.users[name]) return false;
      state.users[name] = { username: name, passwordHash };
      return true;
    });
    if (!created) {
      return res.status(409).json({ error: 'username already taken' });
    }
    const token = await sessions.create(name);
    setSessionCookie(res, token);
    res.status(201).json({ user: { username: name } });
  });

  app.post('/api/login', async (req, res) => {
    const { username, password } = req.body || {};
    const name = typeof username === 'string' ? username.trim() : '';
    const user = store.read((state) => readUser(state, name));
    const ok = user
      ? await verifyPassword(password || '', user.passwordHash)
      : false;
    if (!ok) {
      return res.status(401).json({ error: 'invalid credentials' });
    }
    const token = await sessions.create(name);
    setSessionCookie(res, token);
    res.json({ user: publicUser(user) });
  });

  app.post('/api/logout', async (req, res) => {
    await sessions.revoke(readCookie(req, SESSION_COOKIE) || '');
    clearSessionCookie(res);
    res.json({ ok: true });
  });

  app.get('/api/session', async (req, res) => {
    const session = await sessions.load(readCookie(req, SESSION_COOKIE) || '');
    if (!session) return res.json({ user: null });
    const user = store.read((state) => readUser(state, session.userId));
    res.json({ user: user ? publicUser(user) : null });
  });
}

module.exports = { openAuthStore, mount, DATA_FILE };
'''

_FIXTURE_APP_JS = r'''const express = require('express');
const fs = require('fs');
const path = require('path');

const app = express();

app.use(express.json());

app.get('/api/health', (req, res) => {
  res.json({ code: 200, message: 'Backend Ready' });
});

const frontendDistPath = path.resolve(__dirname, '../../frontend/dist');

if (fs.existsSync(frontendDistPath)) {
  app.use(express.static(frontendDistPath));

  // Keep API routes on the backend and serve the SPA for all other GET requests.
  app.get(/^(?!\/api(?:\/|$)).*/, (req, res) => {
    res.sendFile(path.join(frontendDistPath, 'index.html'));
  });
} else {
  app.get('/', (req, res) => {
    res
      .status(503)
      .type('html')
      .send('Frontend build missing: build frontend/ first');
  });
}

module.exports = app;
'''

_FIXTURE_INDEX_JS = r'''const app = require('./app');
const { openAuthStore, mount } = require('./auth');

async function main() {
  const store = await openAuthStore();
  mount(app, store);
  // Unknown API paths answer JSON 404, never the SPA shell.
  app.use('/api', (req, res) => {
    res.status(404).json({ error: 'not found' });
  });
  const port = Number(process.env.PORT || 3000);
  app.listen(port, '0.0.0.0', () => {
    console.log(`Backend listening at http://127.0.0.1:${port}`);
  });
}

main().catch((error) => {
  console.error(`backend startup failed: ${error.message}`);
  process.exit(1);
});
'''

_FIXTURE_APP_JSX = r'''import { Route, Routes } from 'react-router-dom'
import { SessionProvider, useSession } from './session.jsx'
import AuthPanel from './AuthPanel.jsx'
import HomePage from './pages/HomePage.jsx'

function Shell() {
  const { status, user, error } = useSession()
  if (status === 'loading') {
    return <span data-testid="session-status" data-status="loading">loading</span>
  }
  const statusText =
    status === 'authenticated' && user
      ? `authenticated: ${user.username}`
      : status === 'error'
        ? `error: ${error}`
        : 'anonymous'
  return (
    <div>
      <header>
        <span data-testid="session-status" data-status={status}>{statusText}</span>
        {status === 'authenticated' ? <AuthPanel mode="logout" /> : null}
      </header>
      <main>
        {status === 'authenticated' ? (
          <Routes>
            <Route path="/" element={<HomePage />} />
          </Routes>
        ) : (
          <div className="auth-panels">
            <AuthPanel mode="login" />
            <AuthPanel mode="register" />
          </div>
        )}
      </main>
    </div>
  )
}

export default function App() {
  return (
    <SessionProvider>
      <Shell />
    </SessionProvider>
  )
}
'''

_FIXTURE_AUTH_PANEL_JSX = r'''import { useState } from 'react'
import { apiJson } from './api.js'
import { useSession } from './session.jsx'

export default function AuthPanel({ mode }) {
  const { setSession, clear } = useSession()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [message, setMessage] = useState('')

  const submit = async (event) => {
    event.preventDefault()
    setMessage('')
    try {
      if (mode === 'logout') {
        await apiJson('POST', '/api/logout')
        clear()
        return
      }
      const payload = await apiJson(
        'POST',
        mode === 'login' ? '/api/login' : '/api/register',
        { username, password },
      )
      setSession(payload.user)
      setUsername('')
      setPassword('')
    } catch (error) {
      setMessage(error.message)
    }
  }

  if (mode === 'logout') {
    return <button type="button" onClick={submit}>Logout</button>
  }
  return (
    <form data-testid={`${mode}-form`} onSubmit={submit}>
      <h2>{mode === 'login' ? 'Login' : 'Register'}</h2>
      <label>
        Username
        <input name="username" value={username}
          onChange={(event) => setUsername(event.target.value)} />
      </label>
      <label>
        Password
        <input name="password" type="password" value={password}
          onChange={(event) => setPassword(event.target.value)} />
      </label>
      <button type="submit">{mode === 'login' ? 'Login' : 'Register'}</button>
      {message ? <p data-testid="auth-message" role="alert">{message}</p> : null}
    </form>
  )
}
'''


def _apply_auth_fixture(app: Path) -> None:
    backend_src = app / 'backend' / 'src'
    app_js = backend_src / 'app.js'
    if 'api/register' in app_js.read_text(encoding='utf-8'):
        return  # template already ships auth routes; use them as-is
    (backend_src / 'auth.js').write_text(_FIXTURE_AUTH_JS, encoding='utf-8')
    app_js.write_text(_FIXTURE_APP_JS, encoding='utf-8')
    (backend_src / 'index.js').write_text(_FIXTURE_INDEX_JS, encoding='utf-8')
    frontend_src = app / 'frontend' / 'src'
    if frontend_src.is_dir():
        (frontend_src / 'App.jsx').write_text(_FIXTURE_APP_JSX,
                                              encoding='utf-8')
        (frontend_src / 'AuthPanel.jsx').write_text(_FIXTURE_AUTH_PANEL_JSX,
                                                    encoding='utf-8')


_PLAYWRIGHT_CONFIG_CJS = r'''const path = require('path');
module.exports = {
  testDir: __dirname,
  timeout: 120000,
  expect: { timeout: 20000 },
  workers: 1,
  retries: 0,
  reporter: [
    ['list'],
    ['json', { outputFile: path.join(__dirname, 'pw-results.json') }],
  ],
  use: { screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  projects: [{ name: 'smoke-chromium', use: { browserName: 'chromium' } }],
};
'''

_PLAYWRIGHT_SPEC_JS = r'''const { test, expect } = require('@playwright/test');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const BACKEND_DIR = process.env.SMOKE_BACKEND_DIR;
const PORT = Number(process.env.SMOKE_PORT);
const BASE_URL = `http://127.0.0.1:${PORT}`;
const DATA_FILE = path.join(BACKEND_DIR, 'data', 'db.json');
const SCREENSHOT_DIR = process.env.SMOKE_ARTIFACT_DIR;
const INITIAL_BACKEND_PID = Number(process.env.SMOKE_BACKEND_PID);

function startBackend() {
  const child = spawn(process.execPath, ['src/index.js'], {
    cwd: BACKEND_DIR,
    env: { ...process.env, PORT: String(PORT) },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let buffer = '';
  child.stdout.on('data', (chunk) => { buffer += chunk; });
  child.stderr.on('data', (chunk) => { buffer += chunk; });
  return { child, stderr: () => buffer };
}

async function waitReady(proc) {
  for (let i = 0; i < 150; i++) {
    if (proc.child.exitCode !== null) {
      throw new Error(`backend exited early: ${proc.stderr()}`);
    }
    try {
      const response = await fetch(`${BASE_URL}/api/health`);
      if (response.ok) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error('backend never became ready');
}

async function stopBackend(proc) {
  if (!proc || proc.child.exitCode !== null) return;
  proc.child.kill();
  for (let i = 0; i < 50; i++) {
    if (proc.child.exitCode !== null) return;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  proc.child.kill('SIGKILL');
}

function killByPid(pid) {
  if (!pid) return;
  try { process.kill(pid); } catch {}
}

test('generic auth fixture: register/login/refresh/restart/logout', async ({ page, context }) => {
  const status = () => page.getByTestId('session-status');

  await page.goto(BASE_URL);
  await expect(status()).toHaveText(/anonymous/);
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'smoke-01-anonymous.png') });

  // Bad login stays anonymous and sets no session cookie.
  await page.getByTestId('login-form').locator('[name="username"]').fill('sam');
  await page.getByTestId('login-form').locator('[name="password"]').fill('wrong-password');
  await page.getByTestId('login-form').getByRole('button', { name: 'Login' }).click();
  await expect(status()).toHaveText(/anonymous/);
  await expect(page.getByTestId('login-form').getByTestId('auth-message')).not.toBeEmpty();
  expect((await context.cookies()).some((cookie) => cookie.name === 'pi_session')).toBe(false);

  // Register -> authenticated with a session cookie.
  await page.getByTestId('register-form').locator('[name="username"]').fill('sam');
  await page.getByTestId('register-form').locator('[name="password"]').fill('correct-horse');
  await page.getByTestId('register-form').getByRole('button', { name: 'Register' }).click();
  await expect(status()).toHaveText(/authenticated: sam/);
  expect((await context.cookies()).some((cookie) => cookie.name === 'pi_session')).toBe(true);
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'smoke-02-authenticated.png') });

  // Refresh recovers the session through /api/session.
  await page.reload();
  await expect(status()).toHaveText(/authenticated: sam/);

  // Backend restart: the durable session survives and reload recovers it.
  killByPid(INITIAL_BACKEND_PID);
  await new Promise((resolve) => setTimeout(resolve, 1000));
  const restarted = startBackend();
  await waitReady(restarted);
  await page.reload();
  await expect(status()).toHaveText(/authenticated: sam/);
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'smoke-03-recovered-after-restart.png') });

  // Logout revokes the session: anonymous now, after refresh, and after restart.
  await page.getByRole('button', { name: 'Logout' }).click();
  await expect(status()).toHaveText(/anonymous/);
  expect((await context.cookies()).some((cookie) => cookie.name === 'pi_session')).toBe(false);
  await page.reload();
  await expect(status()).toHaveText(/anonymous/);
  await stopBackend(restarted);
  const restarted2 = startBackend();
  await waitReady(restarted2);
  await page.reload();
  await expect(status()).toHaveText(/anonymous/);
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'smoke-04-anonymous-after-logout.png') });
  await stopBackend(restarted2);
});

test('store helper: failed persistence does not publish mutated state', async () => {
  const { openStore } = require(path.join(BACKEND_DIR, 'src', 'store.js'));
  const probe = path.join(BACKEND_DIR, 'data', 'persist-probe.json');
  const store = await openStore({ filePath: probe, seed: { count: 1 } });
  await expect(store.mutate(() => { throw new Error('boom'); })).rejects.toThrow('boom');
  expect(store.read((state) => state.count)).toBe(1);
  await store.mutate((state) => { state.count = 2; });
  expect(store.read((state) => state.count)).toBe(2);
  fs.rmSync(probe, { force: true });
});

test('store helper: corrupt storage refuses to reseed', async () => {
  fs.mkdirSync(path.dirname(DATA_FILE), { recursive: true });
  fs.writeFileSync(DATA_FILE, '{"users": {"sam":', 'utf8');
  const before = fs.readFileSync(DATA_FILE, 'utf8');
  const proc = startBackend();
  const exitCode = await new Promise((resolve) => {
    proc.child.on('exit', (code) => resolve(code));
    setTimeout(() => resolve('timeout'), 15000);
  });
  expect(exitCode).not.toBe('timeout');
  expect(exitCode).not.toBe(0);
  expect(proc.stderr()).toContain('refusing to reseed');
  expect(fs.readFileSync(DATA_FILE, 'utf8')).toBe(before);
});
'''


def _chromium_executable(grader_dir: Path, deadline: float) -> Path:
    node = _node_bin(deadline)
    from arc_pi import processes
    result = processes.run_command(
        [node, '-e',
         "console.log(require('@playwright/test').chromium.executablePath())"],
        cwd=grader_dir, deadline=deadline)
    if result.returncode != 0:
        raise SmokeError('failed to resolve Playwright Chromium: '
                         + result.stderr[-500:])
    executable = Path(result.stdout.strip().strip('"'))
    if not executable.exists():
        raise SmokeError(f'Playwright Chromium missing at {executable}; run '
                         f'`npm --prefix grader ci` and '
                         f'`npx --prefix grader playwright install chromium`')
    return executable


def _ui_smoke(tmp: Path, deadline: float) -> dict:
    from arc_pi import processes

    template = PROJECT_DIR / 'template'
    grader = PROJECT_DIR / 'grader'
    cli = grader / 'node_modules' / '@playwright/test' / 'cli.js'
    if not cli.exists():
        raise SmokeError(f'missing {cli}; run `npm --prefix grader ci` first')
    _chromium_executable(grader, deadline)

    app = tmp / 'ui-app'
    ignore = shutil.ignore_patterns('node_modules', 'dist', '.git')
    shutil.copytree(template / 'frontend', app / 'frontend', ignore=ignore)
    shutil.copytree(template / 'backend', app / 'backend', ignore=ignore)
    _apply_auth_fixture(app)

    _npm(['ci'], app / 'frontend', deadline)
    _npm(['ci'], app / 'backend', deadline)
    _npm(['run', 'build'], app / 'frontend', deadline)

    node = _node_bin(deadline)
    port = processes.free_port(exclude=3000)
    backend = processes.start_process([node, 'src/index.js'],
                                      cwd=app / 'backend',
                                      env={'PORT': str(port)})
    try:
        _wait_backend_ready(backend, f'http://127.0.0.1:{port}/api/health',
                            deadline)

        specs = tmp / 'specs'
        specs.mkdir()
        (specs / 'auth-smoke.spec.js').write_text(_PLAYWRIGHT_SPEC_JS,
                                                  encoding='utf-8')
        (specs / 'playwright.config.cjs').write_text(_PLAYWRIGHT_CONFIG_CJS,
                                                     encoding='utf-8')
        screenshots = ARTIFACTS_DIR / 'smoke'
        screenshots.mkdir(parents=True, exist_ok=True)

        result = processes.run_command(
            [node, str(cli), 'test', 'auth-smoke.spec.js',
             '--config=playwright.config.cjs'],
            cwd=specs,
            env={'SMOKE_BACKEND_DIR': str(app / 'backend'),
                 'SMOKE_PORT': str(port),
                 'SMOKE_BACKEND_PID': str(backend.pid),
                 'SMOKE_ARTIFACT_DIR': str(screenshots),
                 'NODE_PATH': str(grader / 'node_modules')},
            deadline=deadline)
        report_file = specs / 'pw-results.json'
        stats: dict = {}
        if report_file.exists():
            report = json.loads(report_file.read_text(encoding='utf-8'))
            if isinstance(report, list) and report:
                report = report[-1]
            stats = report.get('stats') or {} if isinstance(report, dict) else {}
        expected = stats.get('expected', 0)
        unexpected = stats.get('unexpected', 0)
        if result.returncode != 0 or unexpected != 0 or expected < 3:
            raise SmokeError(
                f'Playwright auth/session flow failed (rc={result.returncode}, '
                f'expected={expected} unexpected={unexpected}):\n'
                f'--- stdout ---\n{result.stdout[-2000:]}\n'
                f'--- stderr ---\n{result.stderr[-2000:]}')
        shot_files = sorted(str(p.relative_to(ARTIFACTS_DIR))
                            for p in screenshots.glob('*.png'))
        return {'status': 'passed', 'port': port, 'tests': expected,
                'screenshots': shot_files,
                'screenshotsDir': str(screenshots)}
    finally:
        _stop_backend(backend)



def smoke() -> dict:
    """Run the offline protocol and UI proofs. Raises SmokeError on failure."""
    started = time.monotonic()
    deadline = started + 1500
    tmp = Path(tempfile.mkdtemp(prefix='arc-pi-smoke-'))
    global servers
    servers = []
    try:
        protocol = _protocol_smoke(tmp, deadline)
        if not protocol['passed']:
            failures = [item['name'] for item in protocol['checks']
                        if not item['passed']]
            raise SmokeError(f'protocol checks failed: {", ".join(failures)}')
        return {
            'status': 'passed',
            'protocol': protocol,
            'ui': _ui_smoke(tmp, deadline),
            'elapsedMs': int((time.monotonic() - started) * 1000),
        }
    finally:
        for server in servers:
            try:
                server.close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def generation_turn_factory():
    state = {'turns': 0}

    def turn(messages, body):
        state['turns'] += 1
        if state['turns'] == 1:
            return {'tool_calls': [tool_call(
                'gen_call_1', 'write',
                {'path': 'frontend/SMOKE_NOTE.txt',
                 'content': 'generated by the offline loopback smoke; '
                            'sources changed\n'})]}
        return {'content': 'Sources changed; batch implemented.'}

    return state, turn


def packaged_smoke(*, archive: Path | None = None) -> dict:
    from arc_pi import processes

    if archive is None:
        archive = PROJECT_DIR.parent / 'arc_pi_python.zip'
    archive = Path(archive)
    if not archive.exists():
        raise SmokeError(f'missing {archive}; run `python -m arc_pi.cli pack '
                         f'--prebuilt-bridge --output <path>` first')
    started = time.monotonic()
    deadline = started + 1800
    extract_root = Path(tempfile.mkdtemp(prefix='arc pi packaged smoke '))
    gen_state, gen_turn = generation_turn_factory()
    server = ScriptedModelServer(turn_fn=gen_turn)
    try:
        with zipfile.ZipFile(archive) as archive_handle:
            names = archive_handle.namelist()
            for name in names:
                if name.startswith('/') or name.startswith('\\') \
                        or '..' in name.replace('\\', '/').split('/'):
                    raise SmokeError(f'unsafe archive member {name!r}')
            if 'main.py' not in names:
                raise SmokeError('archive has no main.py at the root')
            archive_handle.extractall(extract_root)
        checks: list[dict] = []

        def check(name: str, condition: bool, detail: str = ''):
            checks.append({'name': name, 'passed': bool(condition),
                           'detail': detail})

        python = sys.executable

        # --help works from the extracted tree alone.
        help_run = processes.run_command(
            [python, 'main.py', '--help'], cwd=extract_root,
            deadline=time.monotonic() + 120)
        check('extracted main.py --help', help_run.returncode == 0
              and '--output-dir' in help_run.stdout
              and '-o' in help_run.stdout,
              f'rc={help_run.returncode}')

        # Generation with both spellings against the loopback endpoint.
        env = processes.minimal_env(extra={
            'OPENAI_API_KEY': 'smoke-key',
            'OPENAI_BASE_URL': server.base_url,
            'MODEL': 'deepseek-v4-flash',
        })
        runs = []
        for spelling in (['-o', 'run-short'], ['--output-dir', 'run-long']):
            out = extract_root / 'runs' / spelling[1]
            gen_state['turns'] = 0
            run = processes.run_command(
                [python, 'main.py', 'example/requirements.yaml',
                 spelling[0], str(out), '--type', 'web'],
                cwd=extract_root, env=env,
                deadline=time.monotonic() + 900)
            evidence = out / '.arc' / 'runner-events.jsonl'
            check(f'generation via {spelling[0]} completed',
                  run.returncode == 0 and out.is_dir()
                  and (out / 'frontend' / 'package.json').exists(),
                  f'rc={run.returncode}')
            check(f'SDK evidence under {spelling[0]} output .arc',
                  evidence.exists() and evidence.stat().st_size > 0,
                  str(evidence))
            runs.append({'spelling': spelling[0], 'output': str(out),
                         'exitCode': run.returncode})

        # Frontend build + backend HTTP response from extracted-only assets.
        app_out = extract_root / 'runs' / 'run-long'
        if not (app_out / 'frontend' / 'dist' / 'index.html').exists():
            _npm(['ci'], app_out / 'frontend', deadline)
            _npm(['run', 'build'], app_out / 'frontend', deadline)
        if not (app_out / 'backend' / 'node_modules').exists():
            _npm(['ci'], app_out / 'backend', deadline)
        node = _node_bin(deadline)
        port = processes.free_port(exclude=3000)
        backend = processes.start_process([node, 'src/index.js'],
                                          cwd=app_out / 'backend',
                                          env={'PORT': str(port)})
        try:
            _wait_backend_ready(backend,
                                f'http://127.0.0.1:{port}/api/health', deadline)
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{port}/', timeout=10) as response:
                html = response.read(400).decode('utf-8', errors='replace')
            check('backend HTTP response from extracted assets',
                  response.status == 200 and '<div id="root"></div>' in html,
                  f'status={response.status}')
        finally:
            _stop_backend(backend)

        failed = [c for c in checks if not c['passed']]
        if failed:
            raise SmokeError('packaged smoke failed: ' + '; '.join(
                f"{c['name']}: {c['detail']}" for c in failed))
        return {
            'status': 'passed',
            'archive': str(archive),
            'checks': checks,
            'runs': runs,
            'elapsedMs': int((time.monotonic() - started) * 1000),
        }
    finally:
        server.close()
        shutil.rmtree(extract_root, ignore_errors=True)
