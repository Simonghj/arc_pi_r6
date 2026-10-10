"""Requirement contract checks without a model call: the quoted names and seed records the
requirement states, compared with the generated source, and runtime data that must not ship."""
from __future__ import annotations

import re
from pathlib import Path

QUOTED_RE = re.compile(r'[\u201c"]([^"\u201d\n]{2,60})[\u201d"]')
BACKTICK_RE = re.compile(r"`([^`\n]{2,60})`")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;])\s+")
# A value the scenario itself creates; seeding it would break the create scenario.
TRANSIENT_RE = re.compile(r"does not (?:yet )?exist|not yet (?:used|exist)|\bunused\b|candidate|"
                          r"attempted|\btarget\b|\bnew\b", re.I)
INPUT_LIKE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
SOURCE_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".html")
DATA_SUFFIXES = (".json", ".db", ".sqlite", ".sqlite3")
SKIPPED_DIRS = frozenset({"node_modules", "dist", ".git", ".arc", ".cache"})
MAX_NOTES = 30


def _is_ui_name(text: str) -> bool:
    """Quoted UI names, not example inputs (emails, passwords, slugs, ranges, placeholders)."""
    value = text.strip()
    if not value or not value[0].isalnum() or any(char in value for char in "<@`"):
        return False
    if INPUT_LIKE_RE.match(value) or re.fullmatch(r"[\d\s.,:%-]+", value):
        return False
    return not (" " not in value and re.search(r"\d", value) and re.search(r"[^\w\s]", value))


def _is_entity(value: str) -> bool:
    """Named seed records only; composite values and expressions stay out of the check."""
    return not re.search(r"[/=:]", value) and not value.startswith("=")


def ui_names(nodes: list[dict]) -> list[str]:
    """Names quoted in the descriptions (scenario steps repeat them, plus generated filler)."""
    names: list[str] = []
    for node in nodes:
        for match in QUOTED_RE.finditer(str(node.get("description") or "")):
            value = match.group(1).strip()
            if _is_ui_name(value) and value not in names:
                names.append(value)
    return names


def ui_name_templates(nodes: list[dict]) -> list[str]:
    """Every quoted control name, including templates such as "Options for <item name>"."""
    names: list[str] = []
    for node in nodes:
        for match in QUOTED_RE.finditer(str(node.get("description") or "")):
            value = match.group(1).strip()
            if value and value[0].isalnum() and value not in names:
                names.append(value)
    return names


def seed_values(nodes: list[dict]) -> list[str]:
    """Backticked GIVEN values that already exist; a sentence that marks a value as new or not
    yet existing makes every value in it transient (seeding a create target breaks the test)."""
    seeds: list[str] = []
    for node in nodes:
        for scenario in node.get("scenarios") or []:
            for step in scenario.get("steps") or []:
                if str(step.get("keyword") or "").upper() != "GIVEN":
                    continue
                for sentence in SENTENCE_SPLIT_RE.split(str(step.get("content") or "")):
                    if TRANSIENT_RE.search(sentence):
                        continue
                    for value in BACKTICK_RE.findall(sentence):
                        value = value.strip()
                        if _is_entity(value) and value not in seeds:
                            seeds.append(value)
    return seeds


def _files(root: Path, suffixes: tuple[str, ...], data: bool | None) -> list[Path]:
    if not root.is_dir():
        return []
    found = []
    for path in root.rglob("*"):
        parts = set(path.relative_to(root).parts)
        if (path.is_file() and path.suffix.lower() in suffixes and not SKIPPED_DIRS & parts
                and (data is None or ("data" in parts) == data)
                and not path.name.startswith("package")):
            found.append(path)
    return found


def _joined(paths: list[Path]) -> str:
    return re.sub(r"\s+", " ", "\n".join(path.read_text(encoding="utf-8", errors="replace")
                                         for path in paths))


def source_text(output: Path) -> str:
    return _joined(_files(output / "frontend", SOURCE_SUFFIXES, None)
                   + _files(output / "backend", SOURCE_SUFFIXES, None))


def data_files(output: Path) -> list[Path]:
    """Runtime data the app writes (backend/**/data/*.json, *.db): seeded by code at startup,
    polluted by smoke runs, so it must not ship."""
    backend = output / "backend"
    files = _files(backend, DATA_SUFFIXES, True)
    files += [path for path in _files(backend, (".db", ".sqlite", ".sqlite3"), False)
              if path not in files]
    return files


def contract_notes(output: Path, nodes: list[dict]) -> list[str]:
    """Quoted names and existing seed records that appear nowhere in the source."""
    source = source_text(output)
    backend = source + " " + _joined(data_files(output))
    notes = [f'- name "{name}" is not rendered anywhere' for name in ui_names(nodes)
             if name not in source]
    notes += [f"- seed record `{value}` is not created by the backend" for value in seed_values(nodes)
              if value not in backend]
    return notes[:MAX_NOTES]


def seeds_only_in_data(output: Path, nodes: list[dict]) -> list[str]:
    """Seed values present in the runtime data but in no source file: deleting the data would
    lose them, so the data must stay. Bare numbers name no record and match any data file."""
    data = _joined(data_files(output))
    source = source_text(output)
    return [value for value in seed_values(nodes)
            if not re.fullmatch(r"[\d\s.,:%-]+", value) and value in data and value not in source]
