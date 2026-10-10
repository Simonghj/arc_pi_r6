from pathlib import Path
import shutil

from .verify import VerifyError, _Log, _ensure_installed, _npm_bin, _remove_tree, _run


def prepare_preview(output: Path, deadline: float, log: _Log) -> None:
    packages = [output / 'frontend', output / 'backend']
    for package in packages:
        if not (package / 'package.json').is_file():
            raise VerifyError(f'missing package.json in {package}')
    for package in packages:
        print(f'prepare-preview: installing dependencies in {package}', flush=True)
        cached = _ensure_installed(package, deadline, log)
        target = package / 'node_modules'
        _remove_tree(target)
        shutil.copytree(cached, target, symlinks=True)
    print(f'prepare-preview: building {packages[0]}', flush=True)
    build = _run([_npm_bin(), 'run', 'build'], cwd=packages[0], deadline=deadline, log=log)
    if build.code != 0:
        reason = 'timeout' if build.timed_out else f'exit {build.code}'
        raise VerifyError(f'frontend build failed ({reason}): {build.tail()}')
    if not (packages[0] / 'dist' / 'index.html').is_file():
        raise VerifyError('frontend build did not produce dist/index.html')
