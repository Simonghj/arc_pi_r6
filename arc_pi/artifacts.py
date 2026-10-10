from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import zipfile
from pathlib import Path

from .processes import resolve_node_bin, run_command

PROJECT_DIR = Path(__file__).resolve().parents[1]

HASH_EXCLUDE = frozenset({".git", ".arc", ".venv", "node_modules", "dist",
                          "test-results", "playwright-report"})
SNAPSHOT_EXCLUDE = HASH_EXCLUDE
# Directories restore_app must never touch (at any depth).
RESTORE_KEEP = frozenset({".git", ".arc", "node_modules"})
# Names excluded from packed archives at any depth.
PACK_EXCLUDE = frozenset({".env", ".venv", "node_modules", ".git", ".arc",
                          "tasks", "runs", "output", "test-results",
                          "playwright-report", ".cache", "dist", "__pycache__"})

# Everything that must end up in the shipped zip (main.py at the root).
PACK_ENTRIES = (
    ("file", "main.py"),
    ("dir-python", "arc_pi"),
    ("dir", "arcbench_agent_runtime"),
    ("file", "bridge/package.json"),
    ("file", "bridge/package-lock.json"),
    ("dir", "bridge/dist"),
    ("dir", "template/frontend"),
    ("dir", "template/backend"),
    ("file", "grader/package.json"),
    ("file", "grader/package-lock.json"),
    ("dir", "skills"),
    ("file", "example/requirements.yaml"),
    ("file", "requirements.txt"),
    ("file", ".env"),
    ("file", "README.md"),
)

# primitives


def _remove_tree(path: Path) -> None:
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


def atomic_json(path, data) -> None:
    """Write ``data`` as UTF-8 JSON via a temp file and ``os.replace``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _walk_files(root: Path, exclude_names: frozenset, depth: int = 0) -> list[Path]:
    """Deterministic (sorted) file walk, pruning excluded names at any depth."""
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


def source_hash(path, *, extra_exclude=frozenset()) -> str:
    root = Path(path)
    digest = hashlib.sha256()
    excluded = HASH_EXCLUDE | set(extra_exclude)
    if root.is_file():
        digest.update(root.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(root.read_bytes())
        digest.update(b"\0")
        return digest.hexdigest()
    for file in _walk_files(root, frozenset(excluded)):
        relative = file.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# app snapshots


def _snapshot_ignore(directory, names):
    return sorted(SNAPSHOT_EXCLUDE & set(names))


def _restore_ignore(directory, names):
    return sorted(RESTORE_KEEP & set(names))


def snapshot_app(source, destination) -> Path:
    source, destination = Path(source), Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for entry in sorted(os.scandir(source), key=lambda item: item.name):
        if entry.name in SNAPSHOT_EXCLUDE:
            continue
        target = destination / entry.name
        if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
            shutil.copytree(entry.path, target, ignore=_snapshot_ignore, symlinks=True)
        elif entry.is_file(follow_symlinks=False) or entry.is_symlink():
            shutil.copy2(entry.path, target, follow_symlinks=False)
    return destination


def restore_app(snapshot, destination) -> Path:
    snapshot, destination = Path(snapshot), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    snapshot_files = {file.relative_to(snapshot).as_posix()
                      for file in _walk_files(snapshot, SNAPSHOT_EXCLUDE)}
    snapshot_dirs: set[str] = set()
    for file in snapshot_files:
        segments = file.split("/")
        for index in range(1, len(segments)):
            snapshot_dirs.add("/".join(segments[:index]))

    def prune(current: Path, prefix: str) -> None:
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name)
        except OSError:
            return
        for entry in entries:
            if entry.name in RESTORE_KEEP:
                continue
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                if relative in snapshot_files or relative in snapshot_dirs:
                    prune(Path(entry.path), relative)
                else:
                    _remove_tree(Path(entry.path))
            elif relative not in snapshot_files:
                _remove_tree(Path(entry.path))

    prune(destination, "")
    shutil.copytree(snapshot, destination, dirs_exist_ok=True,
                    ignore=_restore_ignore, symlinks=True)
    return destination


# pack


def _pack_files(root: Path, python_only: bool) -> list[Path]:
    out: list[Path] = []
    for entry in sorted(os.scandir(root), key=lambda item: item.name):
        if entry.name in PACK_EXCLUDE or entry.name.endswith(".zip"):
            continue
        if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
            out.extend(_pack_files(Path(entry.path), python_only))
        elif entry.is_file(follow_symlinks=False) and (
                not python_only or entry.name.endswith(".py")):
            out.append(Path(entry.path))
    return out


def pack(*, deadline=None, project_dir=None, output: Path | None = None,
         prebuilt_bridge: bool = False) -> Path:
    project_dir = Path(project_dir) if project_dir is not None else PROJECT_DIR
    if deadline is None:
        deadline = time.monotonic() + 600
    node = resolve_node_bin(deadline)
    bundle = project_dir / "bridge" / "dist" / "agent.mjs"
    if prebuilt_bridge:
        if not bundle.is_file() or bundle.stat().st_size == 0:
            raise RuntimeError(
                f"prebuilt bridge bundle missing or empty at {bundle}; "
                "rebuild from bridge sources or drop --prebuilt-bridge")
        check = run_command([node, "--check", str(bundle)],
                            cwd=project_dir, deadline=deadline)
        if check.returncode != 0:
            raise RuntimeError(f"prebuilt bridge bundle failed syntax check "
                               f"(exit {check.returncode}): {check.stderr[-2000:]}")
        print(f"pack: keeping prebuilt bridge bundle "
              f"(sha256 {hashlib.sha256(bundle.read_bytes()).hexdigest()})")
    else:
        build_script = project_dir / "bridge" / "build.mjs"
        if not build_script.exists():
            raise RuntimeError(f"bridge/build.mjs missing at {project_dir}; "
                               "recover the bridge sources or use "
                               "--prebuilt-bridge to keep the existing bundle")
        result = run_command([node, "bridge/build.mjs"], cwd=project_dir, deadline=deadline)
        if result.returncode != 0:
            raise RuntimeError(f"bridge bundle failed (exit {result.returncode}): "
                               f"{result.stderr[-2000:]}")
    if not bundle.exists():
        raise RuntimeError("bridge bundle did not produce dist/agent.mjs")
    missing = [rel for kind, rel in PACK_ENTRIES if not (project_dir / rel).exists()]
    if missing:
        raise RuntimeError("missing pack inputs: " + ", ".join(missing))

    target = Path(output) if output is not None else project_dir.parent / "arc_pi_python.zip"
    if target.exists():
        raise RuntimeError(f"refusing to overwrite existing archive {target}; "
                           "choose a fresh --output path")
    staged = target.with_name(f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.zip.tmp")
    try:
        with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as archive:
            for kind, rel in PACK_ENTRIES:
                path = project_dir / rel
                if kind == "file":
                    archive.write(path, arcname=rel.replace(os.sep, "/"))
                else:
                    for file in _pack_files(path, python_only=(kind == "dir-python")):
                        archive.write(file, arcname=file.relative_to(project_dir).as_posix())
        with zipfile.ZipFile(staged) as archive:
            broken = archive.testzip()
            if broken is not None:
                raise RuntimeError(f"packed zip failed integrity check at {broken}")
            if "main.py" not in archive.namelist():
                raise RuntimeError("packed zip lacks main.py at its root")
        os.replace(staged, target)
    finally:
        if staged.exists():
            staged.unlink()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"packed {target} ({target.stat().st_size} bytes, sha256 {digest})")
    return target
