from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from arc_pi.errorlog import report_error

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

_SUBCOMMANDS = ('doctor', 'grade', 'smoke', 'pack',
                'packaged-smoke', 'prepare-preview')


class _ErrorLoggingArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, command: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._errorlog_command = command

    def error(self, message: str):
        report_error(message, command=self._errorlog_command, phase='usage')
        super().error(message)


def _build_generation_parser(prog: str | None) -> argparse.ArgumentParser:
    parser = _ErrorLoggingArgumentParser(
        prog=prog,
        command='generate',
        description='Limitless, arc_pi_python: Generate a full-stack web '
                    'application for a requirement tree using the '
                    'Pi agent engine.',
        epilog='An optional leading "compile" selects the same generation flow. '
               'Use "./compile" for a requirement directory named compile. '
               'Additional local tools: python -m arc_pi.cli '
               'doctor|grade|smoke|pack|packaged-smoke|prepare-preview',
    )
    parser.add_argument(
        'requirement_path', nargs='?', default=None,
        help='YAML requirement file or directory containing requirements.yaml/yml '
             '(default: $ARCBENCH_TASK_DIR or "requirements")')
    parser.add_argument(
        '-o', '--output-dir', dest='output_dir', default=None,
        help='output application directory (default: $ARCBENCH_TEMPLATE_DIR, '
             '$ARCBENCH_OUTPUT_DIR, or runs/<UTC>-<random> under this project)')
    parser.add_argument(
        '-t', '--type', default=None,
        help="task type; only 'web' is supported (default: $ARCBENCH_TASK_TYPE or web)")
    parser.add_argument(
        '--web-port', '--port', dest='web_port', type=int, default=None,
        help='platform port for this run '
             '(default: $ARCBENCH_WEB_PORT or 3000; --port is accepted as an '
             'alias for platform template compatibility)')
    return parser


def _resolve_web_port(cli_port: int | None, *, command: str = 'generate') -> int | None:
    """CLI wins, then ARCBENCH_WEB_PORT, then 3000. Invalid values fail loudly."""
    raw: object = cli_port
    if raw is None:
        raw = os.environ.get('ARCBENCH_WEB_PORT', '').strip()
        if not raw:
            return 3000
    try:
        port = int(raw)
    except (TypeError, ValueError):
        message = f'invalid web port {raw!r}; expected an integer 1-65535'
        report_error(message, command=command, phase='usage')
        return None
    if not 1 <= port <= 65535:
        message = f'web port {port} outside allowed range 1-65535'
        report_error(message, command=command, phase='usage')
        return None
    return port


def _stage_visualizer_requirement(requirement_path: str, output_dir: str,
                                  staging_dir: Path) -> str:
    source = Path(requirement_path).resolve()
    source_dir = source if source.is_dir() else source.parent
    arc_dir = Path(output_dir).resolve() / '.arc'
    if not source_dir.is_dir() or not source_dir.is_relative_to(arc_dir):
        return requirement_path
    snapshot_dir = staging_dir / source_dir.name
    shutil.copytree(source_dir, snapshot_dir)
    print(f'note: reading a snapshot of {source_dir} (inside the output .arc) '
          f'from {snapshot_dir}', flush=True)
    return str(snapshot_dir if source.is_dir() else snapshot_dir / source.name)


def run_generation(argv: list[str] | None = None, *, prog: str | None = None) -> int:
    """Parse direct or compile-prefixed arguments and run the same generation flow."""
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == 'compile':
        argv = argv[1:]
    args = _build_generation_parser(prog).parse_args(argv)

    task_type = args.type or os.environ.get('ARCBENCH_TASK_TYPE', '').strip() or 'web'
    if task_type != 'web':
        message = f'unsupported task type {task_type!r}; only "web" is supported'
        report_error(message, command='generate', phase='usage')
        return EXIT_USAGE

    port = _resolve_web_port(args.web_port, command='generate')
    if port is None:
        return EXIT_USAGE

    from arc_pi import config
    try:
        cfg = config.load_config()
    except config.MissingApiKeyError as exc:
        report_error(str(exc), command='generate', phase='config')
        print(f'note: {exc}', file=sys.stderr)
        return EXIT_OK
    except ValueError as exc:
        report_error(str(exc), command='generate', phase='config')
        return EXIT_FAIL

    requirement_path = (args.requirement_path
                        or os.environ.get('ARCBENCH_TASK_DIR', '').strip()
                        or 'requirements')
    output_dir = (args.output_dir
                  or os.environ.get('ARCBENCH_TEMPLATE_DIR', '').strip()
                  or os.environ.get('ARCBENCH_OUTPUT_DIR', '').strip())
    if not output_dir:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        output_dir = str(cfg.project_dir / 'runs' / f'{stamp}-{secrets.token_hex(4)}')

    from arc_pi import runner
    try:
        with tempfile.TemporaryDirectory(prefix='arc-pi-requirement-',
                                         ignore_cleanup_errors=True) as staging:
            requirement_path = _stage_visualizer_requirement(
                requirement_path, output_dir, Path(staging))
            result = runner.run_task(cfg, requirement_path, output_dir, port)
    except Exception as exc:
        report_error(f"unexpected {type(exc).__name__}: {exc}\n"
                     + traceback.format_exc(),
                     command='generate', phase='run')
        return EXIT_FAIL
    exit_code = int(result.get('exitCode', EXIT_FAIL))
    if exit_code != 0:
        reason = f"run failed ({result.get('outcome')}): {result.get('reason')}"
        report_error(reason, command='generate', phase='run',
                     run_id=result.get('runId'),
                     report_path=result.get('verificationPath'))
        print(f'error: {reason}', file=sys.stderr)
    elif result.get('outcome') == 'partial':
        reason = f"run is PARTIAL/INCOMPLETE: {result.get('reason')}"
        report_error(reason, command='generate', phase='run',
                     run_id=result.get('runId'),
                     report_path=result.get('verificationPath'))
        print(f'note: {reason}', file=sys.stderr)
    elif result.get('outcome') == 'failed':
        reason = (f"generation completed; the generated app failed its own "
                  f"verification: {result.get('reason')}")
        report_error(reason, command='generate', phase='run',
                     run_id=result.get('runId'),
                     report_path=result.get('verificationPath'))
        print(f'note: {reason}', file=sys.stderr)
    return exit_code


def _cmd_doctor(argv: list[str]) -> int:
    parser = _ErrorLoggingArgumentParser(
        prog='python -m arc_pi.cli doctor',
        command='doctor',
        description='Offline environment check: configuration, Node, runtime assets.')
    parser.parse_args(argv)

    from arc_pi import config, processes
    from arc_pi.config import PROJECT_DIR

    problems: list[str] = []
    notes: list[str] = []
    try:
        config.load_config(allow_missing_key=True)
    except ValueError as exc:
        problems.append(f'invalid configuration: {exc}')

    key_present = bool(os.environ.get('OPENAI_API_KEY', '').strip())
    if not key_present:
        notes.append('OPENAI_API_KEY is not set: live generation will skip '
                     'generation and exit 0; fill '
                     f'{config.PROJECT_DIR / ".env"} when ready (not an error).')
    else:
        notes.append('OPENAI_API_KEY is set.')

    try:
        node = processes.resolve_node_bin(time.monotonic() + 10)
    except Exception as exc:
        problems.append(f'Node.js runtime missing or unsupported: {exc}')
    else:
        notes.append(f'Node.js available at {node} (>=22.19.0)')

    bash_path = os.environ.get('PI_BASH_PATH', '').strip()
    if bash_path and not Path(bash_path).exists():
        problems.append(f'PI_BASH_PATH does not exist: {bash_path}')

    from importlib.util import find_spec
    if find_spec('arcbench_agent_runtime') is None:
        problems.append('Python package arcbench_agent_runtime is not importable')
    for asset, description in (
        (PROJECT_DIR / 'bridge' / 'dist' / 'agent.mjs',
         'bridge bundle bridge/dist/agent.mjs (restore the shipped bundle; this checkout has no bridge/build.mjs)'),
        (PROJECT_DIR / 'template' / 'frontend' / 'package.json',
         'frontend starter template/template frontend package.json'),
        (PROJECT_DIR / 'template' / 'backend' / 'package.json',
         'backend starter template/template backend package.json'),
    ):
        if not asset.exists():
            problems.append(f'missing runtime asset: {description}')

    if problems:
        for problem in problems:
            report_error(problem, command='doctor', phase='config')
            print(f'doctor: ERROR: {problem}', file=sys.stderr)
        print('doctor: FAILED', file=sys.stderr)
        return EXIT_FAIL
    for note in notes:
        print(f'doctor: {note}')
    print('doctor: OK')
    return EXIT_OK


def _cmd_grade(argv: list[str]) -> int:
    parser = _ErrorLoggingArgumentParser(
        prog='python -m arc_pi.cli grade',
        command='grade',
        description='Grade an already-generated output application with the '
                    'real verifier (no model involved).')
    parser.add_argument('output', help='generated application directory')
    parser.add_argument('--tests-dir', required=True,
                        help='directory containing the Playwright acceptance tests')
    parser.add_argument('--port', type=int, default=None,
                        help='platform port (default: $ARCBENCH_WEB_PORT or 3000)')
    args = parser.parse_args(argv)

    port = _resolve_web_port(args.port, command='grade')
    if port is None:
        return EXIT_USAGE

    output = Path(args.output)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    report_dir = output / '.arc' / 'arc_pi' / 'grade' / stamp

    from arc_pi import verify
    try:
        report = verify.verify_application(
            output_dir=output,
            tests_dir=Path(args.tests_dir),
            report_dir=report_dir,
            deadline=time.monotonic() + 2400,
            grading_port=port,
            command='grade',
        )
    except Exception as exc:
        report_error(f"unexpected {type(exc).__name__}: {exc}\n"
                     + traceback.format_exc(),
                     command='grade', phase='verify')
        print(f'grade: ERROR: {exc}', file=sys.stderr)
        return EXIT_FAIL

    print(f'grade: report written to {report_dir / "verification.json"}')
    print(f'grade: build={report.get("build")} startup={report.get("startup")} '
          f'acceptance={report.get("acceptance")} '
          f'passed={report.get("passed")} failed={report.get("failed")} '
          f'skipped={report.get("skipped")} total={report.get("total")}')
    return EXIT_OK if report.get('acceptance') == 'passed' else EXIT_FAIL


def _cmd_smoke(argv: list[str]) -> int:
    parser = _ErrorLoggingArgumentParser(
        prog='python -m arc_pi.cli smoke',
        command='smoke',
        description='Offline protocol/UI smoke proof: scripted loopback endpoint '
                    'through the real bridge, plus a real template build and '
                    'browser auth/session flow.')
    parser.parse_args(argv)

    from arc_pi import smoke
    try:
        summary = smoke.smoke()
    except smoke.SmokeError as exc:
        report_error(str(exc), command='smoke', phase='smoke')
        print(f'smoke: FAILED: {exc}', file=sys.stderr)
        return EXIT_FAIL
    print('smoke: PASSED')
    print(json.dumps(summary, indent=2, default=str))
    return EXIT_OK


def _cmd_pack(argv: list[str]) -> int:
    parser = _ErrorLoggingArgumentParser(
        prog='python -m arc_pi.cli pack',
        command='pack',
        description='Build the Node bundle and create Self/arc_pi_python.zip.')
    parser.add_argument('--output', type=Path, default=None,
                        help='destination archive (must not exist; default '
                             'Self/arc_pi_python.zip)')
    parser.add_argument('--prebuilt-bridge', action='store_true',
                        help='keep the existing bridge/dist/agent.mjs bundle '
                             'byte-identical and only syntax-check it '
                             '(required when bridge build sources are absent)')
    args = parser.parse_args(argv)

    import hashlib
    from arc_pi import artifacts
    try:
        archive = artifacts.pack(output=args.output,
                                 prebuilt_bridge=args.prebuilt_bridge)
    except Exception as exc:
        report_error(str(exc), command='pack', phase='pack')
        return EXIT_FAIL
    sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    print(f'pack: {archive}')
    print(f'pack: {archive.stat().st_size} bytes, sha256 {sha256}')
    return EXIT_OK


def _cmd_packaged_smoke(argv: list[str]) -> int:
    parser = _ErrorLoggingArgumentParser(
        prog='python -m arc_pi.cli packaged-smoke',
        command='packaged-smoke',
        description='Extract Self/arc_pi_python.zip into a fresh temporary '
                    'directory and smoke the shipped artifact in isolation.')
    parser.add_argument('--archive', type=Path, default=None,
                        help='archive to smoke (default: Self/arc_pi_python.zip)')
    args = parser.parse_args(argv)

    from arc_pi import smoke
    try:
        summary = smoke.packaged_smoke(archive=args.archive)
    except smoke.SmokeError as exc:
        report_error(str(exc), command='packaged-smoke', phase='smoke')
        print(f'packaged-smoke: FAILED: {exc}', file=sys.stderr)
        return EXIT_FAIL
    print('packaged-smoke: PASSED')
    print(json.dumps(summary, indent=2, default=str))
    return EXIT_OK


def _cmd_prepare_preview(argv: list[str]) -> int:
    parser = _ErrorLoggingArgumentParser(
        prog='python -m arc_pi.cli prepare-preview',
        command='prepare-preview',
        description='Install generated app dependencies and build the frontend for local preview.')
    parser.add_argument('output', type=Path, help='generated application directory')
    args = parser.parse_args(argv)
    from arc_pi.config import PROJECT_DIR
    from arc_pi.preview import prepare_preview
    from arc_pi.verify import _Log

    log_path = PROJECT_DIR / '.cache' / 'preview' / 'prepare.log'
    try:
        prepare_preview(args.output.resolve(), time.monotonic() + 2400, _Log(log_path))
    except Exception as exc:
        report_error(str(exc), command='prepare-preview', phase='prepare')
        print(f'prepare-preview: ERROR: {exc}; log: {log_path}', file=sys.stderr)
        return EXIT_FAIL
    print(f'prepare-preview: ready at {args.output.resolve()}; no server started')
    return EXIT_OK


_SUBCOMMAND_RUNNERS = {
    'doctor': _cmd_doctor,
    'grade': _cmd_grade,
    'smoke': _cmd_smoke,
    'pack': _cmd_pack,
    'packaged-smoke': _cmd_packaged_smoke,
    'prepare-preview': _cmd_prepare_preview,
}


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    if argv and argv[0] in _SUBCOMMANDS:
        return _SUBCOMMAND_RUNNERS[argv[0]](argv[1:])
    return run_generation(argv, prog='python -m arc_pi.cli')


if __name__ == '__main__':
    sys.exit(main())
