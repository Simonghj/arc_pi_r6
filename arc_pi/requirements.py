from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml


class RequirementError(ValueError):
    """Named input error for requirement-tree problems."""


# load


def load_requirement_tree(req_path: Path) -> dict:
    """Load requirements.yaml/yml, accepting root/requirement wrappers."""
    if req_path.is_dir():
        req_file = None
        for name in ("requirements.yaml", "requirements.yml"):
            candidate = req_path / name
            if candidate.is_file():
                req_file = candidate
                break
        if req_file is None:
            raise RequirementError(f"no requirements.yaml/requirements.yml in directory {req_path}")
    else:
        req_file = req_path
        if not req_file.is_file():
            raise RequirementError(f"requirements file not found: {req_path}")
    try:
        data = yaml.safe_load(req_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RequirementError(f"invalid YAML in {req_file}: {exc}") from exc
    if isinstance(data, dict) and "id" not in data:
        for wrapper in ("root", "requirement"):
            if isinstance(data.get(wrapper), dict):
                data = data[wrapper]
                break
    if not isinstance(data, dict) or not str(data.get("id") or "").strip():
        raise RequirementError(f"invalid requirements.yaml in {req_path}: no root id")
    return data


def validate_requirement_tree(tree: dict) -> None:
    if not isinstance(tree, dict):
        raise RequirementError("requirement tree must be a mapping")
    seen: set[str] = set()

    def check(node: object) -> None:
        if not isinstance(node, dict):
            raise RequirementError("requirement node must be a mapping")
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            raise RequirementError("requirement node is missing an id")
        if node_id in seen:
            raise RequirementError(f"duplicate requirement id {node_id!r}")
        seen.add(node_id)
        node_type = str(node.get("type") or "").upper()
        children = node.get("children")
        if children is None:
            children = []
        if not isinstance(children, list) or any(not isinstance(c, dict) for c in children):
            raise RequirementError(f"node {node_id}: children must be a list of mappings")
        deps = node.get("dependencies")
        if deps is None:
            deps = []
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
            raise RequirementError(f"node {node_id}: dependencies must be a list of strings")
        if node_type == "ATOMIC" or (not children and node_type != "FOLDER"):
            description = str(node.get("description") or "").strip()
            scenarios = node.get("scenarios") or []
            if not description and not scenarios:
                raise RequirementError(f"atomic node {node_id} has no description or scenarios")
        for child in children:
            check(child)

    check(tree)
    if not flatten_atomic(tree):
        raise RequirementError("requirement tree contains no atomic work")
    topo_order(tree)


# traversal


def flatten_atomic(node: dict, out: list | None = None) -> list[dict]:
    """Leaves in document order. Nodes typed ATOMIC, or untyped leaves, count."""
    if out is None:
        out = []
    node_type = str(node.get("type") or "").upper()
    if node_type == "ATOMIC" or (not node.get("children") and node_type != "FOLDER"):
        out.append(node)
    for child in node.get("children") or []:
        if isinstance(child, dict):
            flatten_atomic(child, out)
    return out


def _descendant_atomic_ids(tree: dict) -> dict[str, list[str]]:
    """Map every node id (folders included) to its ATOMIC descendants' ids."""
    mapping: dict[str, list[str]] = {}

    def walk(node: dict) -> list[str]:
        node_id = str(node.get("id") or "")
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        node_type = str(node.get("type") or "").upper()
        if node_type == "ATOMIC" or (not children and node_type != "FOLDER"):
            ids = [node_id]
        else:
            ids = []
            for child in children:
                ids.extend(walk(child))
        if node_id:
            mapping[node_id] = ids
        return ids

    walk(tree)
    return mapping


def topo_order(tree: dict) -> list[dict]:
    nodes = flatten_atomic(tree)
    descendants = _descendant_atomic_ids(tree)
    ids = [str(n.get("id")) for n in nodes]
    known = set(ids)
    if len(ids) != len(known):
        raise RequirementError(f"duplicate node ids in requirement tree: {sorted(ids)}")

    deps: dict[str, list[str]] = {}
    for node, node_id in zip(nodes, ids):
        expanded: list[str] = []
        for dep in node.get("dependencies") or []:
            dep_id = str(dep)
            if dep_id == node_id:
                raise RequirementError(f"node {node_id} lists itself as a dependency")
            if dep_id not in descendants:
                continue  # an earlier stage's requirement: already part of the starting app
            for atom in descendants[dep_id]:
                if atom in known and atom != node_id and atom not in expanded:
                    expanded.append(atom)
        deps[node_id] = expanded

    done: set[str] = set()
    remaining = list(zip(ids, nodes))
    ordered: list[dict] = []
    while remaining:
        pick = next(((i, n) for i, n in remaining if all(d in done for d in deps[i])), None)
        if pick is None:
            stuck = [(i, [d for d in deps[i] if d not in done]) for i, _ in remaining]
            raise RequirementError(f"dependency cycle among leaf nodes: {stuck}")
        remaining.remove(pick)
        done.add(pick[0])
        ordered.append(pick[1])
    return ordered


def external_dependencies(tree: dict) -> list[str]:
    """Dependency ids that are not in this tree: staged requirement files list only the new
    requirements, and their dependencies point at earlier stages (built into the starting app)."""
    descendants = _descendant_atomic_ids(tree)
    found: list[str] = []
    for node in flatten_atomic(tree):
        for dep in node.get("dependencies") or []:
            if str(dep) not in descendants and str(dep) not in found:
                found.append(str(dep))
    return found


def ancestors_of(node_id: str, ordered_nodes: list[dict]) -> list[str]:
    """Transitive dependencies of ``node_id`` in the given topological order."""
    by_id = {str(n.get("id")): n for n in ordered_nodes}
    seen: set[str] = set()
    stack = [str(d) for d in (by_id.get(node_id, {}).get("dependencies") or [])]
    while stack:
        dep = stack.pop()
        if dep in seen or dep not in by_id or dep == node_id:
            continue
        seen.add(dep)
        stack.extend(str(d) for d in (by_id[dep].get("dependencies") or []))
    return [str(n.get("id")) for n in ordered_nodes if str(n.get("id")) in seen]


def node_fingerprint(node: dict) -> str:
    """Stable hash of the requirement content (not of child nodes)."""
    payload = {
        "id": str(node.get("id") or ""),
        "name": str(node.get("name") or ""),
        "description": str(node.get("description") or ""),
        "scenarios": node.get("scenarios") or [],
        "dependencies": [str(d) for d in (node.get("dependencies") or [])],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def top_folder_of_leaves(tree: dict) -> dict[str, str | None]:
    """Map each leaf id to its top-level (direct child of root) folder id."""
    mapping: dict[str, str | None] = {}

    def walk(node: dict, top_id: str | None, is_root: bool) -> None:
        own_id = str(node.get("id") or "") or None
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        leaf = not children or str(node.get("type") or "").upper() == "ATOMIC"
        top = None if is_root else (top_id or own_id)
        if leaf and own_id:
            mapping[own_id] = top
        for child in children:
            walk(child, top, False)

    walk(tree, None, True)
    return mapping


def node_index(tree: dict) -> dict[str, dict]:
    """Every node by id, folders included."""
    index: dict[str, dict] = {}

    def walk(node: dict) -> None:
        node_id = str(node.get("id") or "")
        if node_id:
            index[node_id] = node
        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child)

    walk(tree)
    return index


def root_id(tree: dict) -> str:
    return str(tree.get("id") or "")


# description


def describe_node(node: dict) -> str:
    """Full requirement text for one atomic node (prompt payload)."""
    lines = [f"### {node.get('id', '')}: {node.get('name', '')}"]
    description = str(node.get("description") or "").strip()
    if description:
        lines.append(description)
    scenarios = node.get("scenarios") or []
    if isinstance(scenarios, list) and scenarios:
        lines.append("Scenarios:")
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                continue
            lines.append(f"- {scenario.get('name', 'scenario')}")
            for step in scenario.get("steps") or []:
                if isinstance(step, dict):
                    lines.append(f"    {step.get('keyword', '')} {step.get('content', '')}")
    deps = node.get("dependencies") or []
    if deps:
        lines.append(f"Depends on: {', '.join(map(str, deps))}")
    return "\n".join(lines)


def describe_folder(node: dict) -> str:
    """Folder-level architectural context (informational only)."""
    lines = [f"### {node.get('id', '')}: {node.get('name', '')}"]
    description = str(node.get("description") or "").strip()
    if description:
        lines.append(description)
    return "\n".join(lines)


def node_text(node: dict) -> str:
    """Ancestor/leaf text used by skill selection (name + description)."""
    return f"{node.get('name', '')}\n{node.get('description', '')}".strip()
