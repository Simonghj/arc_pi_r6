"""Deadline-aware subprocesses, minimal environments and verified Node provisioning."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.request


def minimal_env(extra=None):
    allowed = {'PATH','PATHEXT','SYSTEMROOT','WINDIR','COMSPEC','TEMP','TMP','TMPDIR',
               'HOME','USERPROFILE','APPDATA','LOCALAPPDATA','PROGRAMFILES','PROGRAMFILES(X86)',
               'SYSTEMDRIVE','LANG','LC_ALL','PLAYWRIGHT_BROWSERS_PATH','CI'}
    result = {k:v for k,v in os.environ.items() if k.upper() in allowed}
    result.update({'CI':'1', 'NO_COLOR':'1'})
    if extra:
        result.update({str(k):str(v) for k,v in extra.items()})
    return result


def _invocation(argv):
    args = [str(arg) for arg in argv]
    if os.name == 'nt' and Path(args[0]).suffix.lower() in {'.cmd','.bat'}:
        # cmd /s strips precisely the outside pair; every argument stays quoted.
        if any('"' in arg or '\n' in arg or '\r' in arg for arg in args):
            raise ValueError('Unsafe quote/newline in Windows batch arguments')
        return f'"{os.environ.get("COMSPEC", "cmd.exe")}" /d /s /c "' + ' '.join(f'"{arg}"' for arg in args) + '"'
    return args


def start_process(argv, *, cwd, env=None, stdin=None):
    return subprocess.Popen(_invocation(argv), cwd=str(cwd), env=minimal_env(env),
        stdin=stdin or subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace',
        start_new_session=os.name != 'nt',
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)


def stop_process(proc, deadline=None):
    if proc.poll() is not None:
        return
    remaining = max(.1, min(5., (deadline-time.monotonic()) if deadline else 5.))
    if os.name == 'nt':
        # cleanup must never raise: a spent deadline still gets a few seconds for taskkill
        try:
            subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'], capture_output=True,
                           timeout=max(3., remaining), check=False)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=min(1., remaining))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        proc.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=1)


def run_command(argv, *, cwd, deadline, env=None):
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Global deadline exhausted before subprocess dispatch')
    proc = start_process(argv, cwd=cwd, env=env)
    try:
        stdout, stderr = proc.communicate(timeout=remaining)
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    except BaseException:
        stop_process(proc)
        proc.communicate(timeout=5)
        raise


def free_port(exclude=3000):
    excluded = {exclude} if isinstance(exclude,int) else set(exclude)
    for _ in range(100):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port = sock.getsockname()[1]
        if port not in excluded:
            return port
    raise RuntimeError('Cannot find free loopback port')


def resolve_node_bin(deadline):
    node = shutil.which('node')
    if node:
        result = run_command([node,'--version'],cwd=Path.cwd(),deadline=deadline)
        try:
            if result.returncode == 0 and tuple(map(int,result.stdout.strip().lstrip('v').split('.')[:3])) >= (22,19,0):
                return node
        except ValueError:
            pass
    if platform.system() != 'Linux' or platform.machine() not in {'x86_64','AMD64'}:
        raise RuntimeError('Install Node >=22.19.0; automatic provisioning supports only Linux x64')
    version = 'v24.19.0'
    name = f'node-{version}-linux-x64'
    expected = '14b342e71204f811bde6153be8e04b62aef63c236fef92b55f9c83154b409647'
    base = f'https://nodejs.org/dist/{version}/'
    cache = Path(tempfile.gettempdir())/'arc-pi-python-node'/version
    marker = cache/name/'bin/node'
    if not marker.exists():
        def fetch(url):
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Deadline exhausted provisioning Node')
            with urllib.request.urlopen(url,timeout=min(120,remaining)) as response:
                chunks=[]
                while chunk := response.read(1024*1024):
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Deadline exhausted downloading Node')
                    chunks.append(chunk)
                return b''.join(chunks)
        manifest = fetch(base+'SHASUMS256.txt').decode()
        if f'{expected}  {name}.tar.xz' not in manifest.splitlines():
            raise RuntimeError('Pinned Node hash disagrees with official manifest')
        data = fetch(base+name+'.tar.xz')
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError('Node archive SHA256 mismatch')
        cache.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as temp:
            archive=Path(temp)/'node.tar.xz'; archive.write_bytes(data)
            with tarfile.open(archive,'r:xz') as tar:
                tar.extractall(temp,filter='data')
            shutil.move(str(Path(temp)/name),str(cache/name))
    os.environ['PATH']=str(marker.parent)+os.pathsep+os.environ.get('PATH','')
    return str(marker)
