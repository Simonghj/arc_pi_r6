from __future__ import annotations

import re
from pathlib import Path

from arc_pi.requirements import describe_node, node_text

MAX_BATCH_LEAVES = 8
MAX_BATCH_SPEC_BYTES = 20 * 1024
MERGED_BATCH_LEAVES = 6
MERGED_BATCH_SPEC_BYTES = 32 * 1024
SECONDS_PER_BATCH = 600  # implementation plus its gated review
MAX_INLINE_NODE_BYTES = 24 * 1024

CHUNK_GROWTH_RESERVE_BYTES = 192 * 1024
CHUNK_MIN_BYTES = 4 * 1024

AUTH_SKILL_RE = re.compile(
    r"auth|login|log in|sign in|register|registration|logout|log out|"
    r"session|account|登录|注册|退出",
    re.IGNORECASE,
)

SYSTEM_PREFIX = """\
You are a coding agent generating a runnable full-stack web application that satisfies a requirement tree.

Workspace contract (the benchmark runner depends on this EXACT layout):
- frontend/: web frontend with a package.json whose `npm run build` produces frontend/dist/. Use plain CSS and native fetch; keep dependencies minimal and pure JavaScript.
- backend/: Node.js + Express with a package.json whose `npm run start` honors the PORT environment variable. Persistence must be pure JavaScript: a JSON file (e.g. backend/data/db.json) loaded at startup and written back on every mutation. Hash passwords with Node's built-in crypto (scrypt), never plaintext.
- The backend serves the built frontend from frontend/dist/ and exposes JSON APIs under /api/. Only PORT is used; never bind a second fixed port.
- The current requirement tree lives under the configured read-only input root; keep all requirement text available there and read it as needed. Input roots are immutable.

Tool discipline:
- All tool paths are relative to the workspace root (the generated app directory). Never use absolute Windows paths (C:/..., D:/...) or backslash drive mappings; use forward-slash relative paths such as frontend/src/pages/HomePage.jsx. The tools cannot see outside the workspace and the read-only input roots.
- Think first, then write: plan the batch's routes, API endpoints, store changes and pages before the first write, so the code is right the first time. Prefer writing whole files over many small edits; read a file once and never re-read a file you wrote yourself in this batch.
- Keep each reply small enough to finish quickly: at most about 4 files per reply, continuing in the next turn. Write a module in the same reply as, or before, the file that imports it.
- Put independent tool calls in ONE reply: either several tool calls together or one `batch` call (up to 8 read/write/edit/bash calls, run in order). One tool call per reply wastes turns.
- Use read/bash only to inspect a specific file you are about to edit, never for broad reconnaissance of the workspace.
- Never kill all node processes (taskkill /IM node.exe, wmic ... node.exe, pkill node): that stops the agent engine too. Stop only a server you started, by its PID or port.
- Every scenario starts at `/` (its GIVEN steps start at the home page) and the user clicks visible links, buttons and menus by their exact quoted names: make every page reachable from the home page (and from the header after sign-in) and render each named control once per page.
- The home page `/` links, by exact name, to every seeded top-level record a visitor may open (and after sign-in to the user's own as well): a scenario can only reach a record it can click to from `/`.
- A step that clicks, opens or selects a quoted name needs a link, button, tab or menu item with exactly that accessible name; a heading with that text cannot be clicked.
- Read each scenario's path literally. When a GIVEN starts at the home page and a step then "opens" a named page or record ("opens the Reports page", "opens that record", "opens the visible `<title>` item") without naming the links on the way, that name is the next thing the user clicks, so the start page (home `/`, signed out or after the stated sign-in) must show a visible link with exactly that name, leading to that page or to the record named in the GIVEN. List such pages and the seeded records the scenarios open (titles as links) in the home page body, not the global header, so other pages keep one control per name.
- When a step offers alternatives ("clicks `Add` or `Update`"), the page shows exactly one of them at a time, so the step names one control.
- Render every page's heading, its section links and tabs, and the action links and buttons that depend only on the route and the signed-in user on the first render, so the user can act at once after each click; never return a whole-page loading placeholder while a request runs, show the loading text only inside the list or panel that waits for data, and keep fetched data in memory so a revisited page shows it at once.
- Reference pictures show layout only: never copy their browser chrome, ads, account widgets, sample data or side navigation that the requirement does not name; the requirement text decides which controls exist.
- The role word in the requirement decides the element: "textbox" is `<input type="text">` (or email/url/tel), "searchbox" is `<input type="search">`, "combobox" is `<select>` unless the requirement says its options use the option role, have visible names or are clicked (then build the ARIA listbox pattern: a `role="combobox"` button with `aria-expanded` that opens a `role="listbox"` of clickable `role="option"` items named by their visible text, and keyboard selection), "checkbox" is `<input type="checkbox">`, "link" is `<a>`, "button" is `<button>`; a search field the requirement calls a textbox must not be `type="search"`.
- A button whose name contains another control's whole name makes that name ambiguous for the user and for assistive technology: never give a button a name that contains an item's name ("More options for Monthly report", "Page 1 options") unless the requirement names it so (a name the requirement gives with a placeholder, such as "Options for <item name>", is used exactly, with the item's name filled in). Use a short name ("More options") inside the item's row or tab.
- Render the header on first paint: while the session is loading show the signed-out controls (the sign-in link), never an empty header.
- Every create flow opens the new record ready for the next step, and every edit acts on exactly the element the user selected.
- Records the requirement says already exist (accounts, records, files, ...) are created by backend code at startup when missing, idempotently; never by editing the data file, which is runtime state, not source. A value a scenario creates, or says does not exist yet, is never seeded.
- Users can sign in only with credentials the requirement prints. When the seed data supplies a role (an owner, admin, maintainer, member...) without naming a separate account and password for it, the seeded account whose credentials are given holds that role; never give a seeded role to an invented account nobody can sign in as.
- If the workspace already holds an application when the run starts, it is the baseline from an earlier stage: read its structure first, keep its existing pages, routes, data and behavior working, and add or change only what the requirements need.
- Every import, route, and cross-file reference must resolve to a file you have actually written: never leave an import or route pointing at a missing file. The harness build check runs after each batch, and one unresolvable import fails the whole application.
- Write small readable functions and plain code. Extend the existing app and reuse its existing owners and structure; do not rewrite working features or scaffold a second app.
- Do not add code comments or docstrings. Use concise names and straightforward control flow.
- Do not add speculative frameworks or abstractions, fake backend data, or test-specific shortcuts. Implement the exact requirement labels, routes and state transitions.
- Preserve requirement-specified accessible roles as well as labels: a link and a button are not interchangeable. Render dynamic values such as the signed-in user's name in their own text element, separate from surrounding label text.
- Implement exactly what the requirement text states; never hardcode outcomes or add test-specific shortcuts.
- Do not hand-edit package-lock.json, node_modules, .env, .git, or .arc files. `.arc/` holds run records of the platform and earlier runs, not app code: never list, open or search it, and never inspect the platform's own directories; search inside frontend/, backend/ and review-tests/.
- Do not claim any test passed or any endpoint verified: the harness runs its own scenario checks after you finish.
- Root-view chrome: the home view must expose every global control the requirement tree mentions (search field, settings button, sidebar toggle, primary navigation), because the scenarios reach later features from the home page; a missing global control blocks the user before the feature is even reached.
- Finish each reply with a brief summary of changed files and unresolved requirements."""


def group_batches(
    ordered_nodes: list[dict],
    top_folder_map: dict[str, str | None],
    *,
    max_leaves: int = MAX_BATCH_LEAVES,
    max_bytes: int = MAX_BATCH_SPEC_BYTES,
) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_folder: object = object()
    current_bytes = 0
    for node in ordered_nodes:
        folder = top_folder_map.get(str(node.get("id") or ""))
        size = len(describe_node(node).encode("utf-8"))
        if (
            current
            and folder == current_folder
            and len(current) < max_leaves
            and current_bytes + size <= max_bytes
        ):
            current.append(node)
            current_bytes += size
        else:
            if current:
                batches.append(current)
            current = [node]
            current_folder = folder
            current_bytes = size
    if current:
        batches.append(current)
    return batches


def fit_batches(
    batches: list[list[dict]],
    target: int,
    *,
    max_leaves: int = MERGED_BATCH_LEAVES,
    max_bytes: int = MERGED_BATCH_SPEC_BYTES,
) -> list[list[dict]]:
    """Merge the smallest adjacent pair until at most ``target`` batches remain (order kept)."""
    merged = [list(batch) for batch in batches]
    sizes = [sum(len(describe_node(node).encode("utf-8")) for node in batch) for batch in merged]
    while len(merged) > max(1, target):
        pairs = [
            (sizes[i] + sizes[i + 1], i) for i in range(len(merged) - 1)
            if len(merged[i]) + len(merged[i + 1]) <= max_leaves
            and sizes[i] + sizes[i + 1] <= max_bytes
        ]
        if not pairs:
            break
        _, i = min(pairs)
        merged[i:i + 2] = [merged[i] + merged[i + 1]]
        sizes[i:i + 2] = [sizes[i] + sizes[i + 1]]
    return merged


def _node_chain(tree: dict, node_id: str) -> list[dict]:
    def find(node: dict, trail: list[dict]) -> list[dict] | None:
        next_trail = trail + [node]
        if str(node.get("id") or "") == node_id:
            return next_trail
        for child in node.get("children") or []:
            if isinstance(child, dict):
                hit = find(child, next_trail)
                if hit:
                    return hit
        return None

    return find(tree, []) or []


def combined_chain_text(tree: dict, node_id: str) -> str:
    """Name+description of the node and its ancestor folders (root excluded)."""
    chain = _node_chain(tree, node_id)
    root = str(tree.get("id") or "")
    parts = [node_text(node) for node in chain if str(node.get("id") or "") != root]
    return "\n".join(part for part in parts if part)


def wants_auth_skill(tree: dict, node_ids: list[str]) -> bool:
    """True when any combined ancestor/leaf text matches the auth pattern."""
    return any(AUTH_SKILL_RE.search(combined_chain_text(tree, node_id)) for node_id in node_ids)


ALWAYS_SKILL = "web-implementation"  # loaded once into the system prompt
REVIEW_SKILL = "review-and-debug"
E2E_SKILL = "playwright-e2e"


def select_skills(tree: dict, node_ids: list[str], *, repair: bool = False) -> list[str]:
    """Per-prompt skills on top of the system prompt's always-on skill."""
    names: list[str] = []
    if wants_auth_skill(tree, node_ids):
        names.append("auth-session-consistency")
    if repair:
        names.append(REVIEW_SKILL)
    return names


def root_contract(tree: dict) -> str:
    """Product-wide rules from the ROOT description (exact names, seed data...)."""
    text = str(tree.get("description") or "").strip()
    return f"## Product-wide requirements (apply to every page)\n{text}" if text else ""


def folder_contracts(tree: dict, node_ids: list[str]) -> str:
    """Deduplicated ancestor FOLDER descriptions for a batch (root excluded)."""
    root = str(tree.get("id") or "")
    seen: dict[str, str] = {}
    for node_id in node_ids:
        for node in _node_chain(tree, node_id)[:-1]:
            folder_id = str(node.get("id") or "")
            if folder_id and folder_id != root and folder_id not in seen:
                seen[folder_id] = f"### {folder_id}: {node_text(node)}"
    if not seen:
        return ""
    return "## Section contracts for this batch\n" + "\n\n".join(seen.values())


def skill_text(skills_dir: Path, names: list[str]) -> tuple[str, list[str]]:
    """Concatenated skill bodies plus the names whose SKILL.md is absent."""
    parts: list[str] = []
    absent: list[str] = []
    for name in names:
        path = skills_dir / name / "SKILL.md"
        if path.is_file():
            parts.append(f"[Skill: {name}]\n{path.read_text(encoding='utf-8').strip()}")
        else:
            absent.append(name)
    return "\n\n".join(parts), absent


def _locate_node(requirement_dir: Path, node_id: str) -> tuple[str, int, int] | None:
    """(filename, first_line, last_line) of the node's YAML block, 1-based."""
    for name in ("requirements.yaml", "requirements.yml"):
        path = requirement_dir / name
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        pattern = re.compile(r"^(\s*)(?:-\s*)?id:\s*" + re.escape(node_id) + r"\s*(?:#.*)?$")
        for index, line in enumerate(lines):
            match = pattern.match(line)
            if not match:
                continue
            indent = len(match.group(1))
            end = index + 1
            while end < len(lines):
                candidate = lines[end]
                if candidate.strip() and (len(candidate) - len(candidate.lstrip())) <= indent:
                    break
                end += 1
            return (name, index + 1, end)
    return None


def _acceptance_hints(node: dict, budget: int) -> str:
    lines: list[str] = []
    used = 0
    for scenario in node.get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        line = f"- {scenario.get('name', 'scenario')}"
        if used + len(line.encode("utf-8")) > budget:
            break
        lines.append(line)
        used += len(line.encode("utf-8"))
        for step in scenario.get("steps") or []:
            if not isinstance(step, dict):
                continue
            line = f"    {step.get('keyword', '')} {step.get('content', '')}"
            if used + len(line.encode("utf-8")) > budget:
                break
            lines.append(line)
            used += len(line.encode("utf-8"))
    if not lines:
        return "(no scenarios)"
    return "\n".join(lines)


def _oversized_reference(node: dict, full_text: str, requirement_dir: Path, budget: int) -> str:
    node_id = str(node.get("id") or "")
    located = _locate_node(requirement_dir, node_id)
    if located:
        reference = f"{located[0]}:{located[1]}-{located[2]}"
    else:
        reference = str(requirement_dir)
    size = len(full_text.encode("utf-8"))
    return (
        f"### {node_id}: {node.get('name', '')}\n"
        f"Full requirement text is too large to inline ({size} bytes). "
        f"It is available read-only at {reference}; read it before implementing.\n"
        f"Acceptance context:\n{_acceptance_hints(node, budget)}"
    )


def _node_specs(
    group_nodes: list[dict],
    requirement_dir: Path | None,
    max_inline_bytes: int,
) -> list[str]:
    specs: list[str] = []
    for node in group_nodes:
        text = describe_node(node)
        if requirement_dir is not None and len(text.encode("utf-8")) > max_inline_bytes:
            specs.append(_oversized_reference(node, text, requirement_dir, max_inline_bytes))
        else:
            specs.append(text)
    return specs


def _hard_split_bytes(text: str, budget: int) -> list[str]:
    pieces: list[str] = []
    remaining = text
    while len(remaining.encode("utf-8")) > budget:
        raw = remaining.encode("utf-8")
        cut = budget
        while cut > 0:
            try:
                raw[:cut].decode("utf-8")
                break
            except UnicodeDecodeError:
                cut -= 1
        if cut <= 0:
            cut = budget  # single multibyte char larger than budget: keep it whole
        pieces.append(raw[:cut].decode("utf-8"))
        remaining = raw[cut:].decode("utf-8")
    if remaining:
        pieces.append(remaining)
    return pieces


def _split_specs(specs: list[str], slice_budget: int) -> list[str]:
    slices: list[str] = []
    current: list[str] = []
    current_bytes = 0

    def flush() -> None:
        nonlocal current, current_bytes
        if current:
            slices.append("\n\n".join(current))
            current = []
            current_bytes = 0

    for spec in specs:
        size = len(spec.encode("utf-8"))
        if size <= slice_budget:
            if current and current_bytes + 2 + size > slice_budget:
                flush()
            current.append(spec)
            current_bytes += size + (2 if len(current) > 1 else 0)
        else:
            flush()
            pieces = _hard_split_bytes(spec, slice_budget)
            for piece in pieces:
                current.append(piece)
                current_bytes = len(piece.encode("utf-8"))
                flush()
    flush()
    return slices


def batch_prompt(
    group_nodes: list[dict],
    skills_text: str,
    batch_index: int,
    *,
    requirement_dir: Path | None = None,
    file_listing: str = "",
    max_inline_bytes: int = MAX_INLINE_NODE_BYTES,
) -> str:
    sections = [
        f"Batch {batch_index}: implement the following requirement node(s) completely: "
        "backend API, persistence, and the frontend UI to exercise them. The application "
        "skeleton in the current working directory already exists; extend it, do not "
        "scaffold a second app.",
    ]
    if file_listing:
        sections.append(
            "## Current application source files (use these paths directly; "
            "do not re-explore the workspace)\n" + file_listing
        )
    if skills_text:
        sections.append("## Skills (mandatory rules; follow them exactly)")
        sections.append(skills_text)
    sections.append("## Requirement nodes")
    sections.append("\n\n".join(_node_specs(group_nodes, requirement_dir, max_inline_bytes)))
    sections.append(
        "Nodes already implemented by earlier batches must not be re-scaffolded or broken. "
        "Implement the batch now with concrete file edits. After your edits, run local smoke "
        "checks only against the port the harness allows (via bash, bounded to the remaining "
        "budget). Report: changed file paths, and any part of this batch still unresolved."
    )
    return "\n\n".join(sections)


def chunked_batch_prompts(
    group_nodes: list[dict],
    skills_text: str,
    batch_index: int,
    *,
    requirement_dir: Path | None = None,
    file_listing: str = "",
    chunk_target_bytes: int,
    max_inline_bytes: int = MAX_INLINE_NODE_BYTES,
) -> list[str]:
    base_prefix = (
        f"Batch {batch_index}: implement the following requirement node(s) completely: "
        "backend API, persistence, and the frontend UI to exercise them. The application "
        "skeleton in the current working directory already exists; extend it, do not "
        "scaffold a second app."
    )
    if file_listing:
        base_prefix += (
            "\n\n## Current application source files (use these paths directly; "
            "do not re-explore the workspace)\n" + file_listing
        )
    if skills_text:
        base_prefix += "\n\n## Skills (mandatory rules; follow them exactly)\n\n" + skills_text
    completion = (
        "Nodes already implemented by earlier batches must not be re-scaffolded or broken. "
        "Implement the batch now with concrete file edits. After your edits, run local smoke "
        "checks only against the port the harness allows (via bash, bounded to the remaining "
        "budget). Report: changed file paths, and any part of this batch still unresolved."
    )
    # Fixed per-chunk bytes: prefix + part marker + completion (last chunk) + slack.
    fixed_bytes = (
        len(base_prefix.encode("utf-8"))
        + len(completion.encode("utf-8"))
        + 256
    )
    slice_budget = max(CHUNK_MIN_BYTES, chunk_target_bytes - fixed_bytes)
    slices = _split_specs(_node_specs(group_nodes, requirement_dir, max_inline_bytes), slice_budget)
    total = len(slices)
    prompts: list[str] = []
    for index, slice_text in enumerate(slices, start=1):
        part = f"## Requirement nodes: Part {index} of {total}."
        if index > 1:
            part += " Continue exactly where the previous part ended; do not repeat completed work."
        if index < total:
            part += " Later parts continue this requirement content."
        sections = [base_prefix, part, slice_text]
        if index == total:
            sections.append(completion)
        prompts.append("\n\n".join(sections))
    return prompts


def contract_prompt(batch_index: int, notes: list[str]) -> str:
    """One follow-up for requirement names and seed records the static check did not find."""
    return "\n".join([
        f"Batch {batch_index} contract check: the requirement text of this batch names these, "
        "but the application source does not contain them:",
        *notes,
        "For each UI name (control, heading, label, message): render it with exactly that text and "
        "the role the requirement states, reachable by clicks from `/`. Skip a quoted phrase that "
        "is only an example input or a description, not something the page shows. For each seed "
        "record: create it in backend startup code when missing. Edit the files now; report the "
        "changed paths.",
    ])


def nudge_prompt() -> str:
    """One concrete tool-execution nudge for a turn with no file changes."""
    return (
        "Your last turn produced no file changes. Stop analyzing and act: read the "
        "relevant requirement files and existing sources, then CREATE or EDIT the "
        "concrete project files (frontend and backend sources) that implement the "
        "current batch. Do not describe a plan; write the files now, then run the "
        "applicable build or smoke check."
    )


def failure_digest(failures: list[str]) -> str:
    """Repair prompt body from deduplicated, capped observed failures."""
    lines = [
        "The verification run found failures. Fix ONLY the following observed "
        "failures; do not redesign unrelated features.",
        "## Observed failures",
    ]
    lines.extend(f"- {failure}" for failure in failures)
    lines.append(
        "Re-read the referenced application files and fix the entire observed interaction, "
        "including the exact text and accessible role of each control. An assertion may hide "
        "the next failure in the same flow; preserve successful changes while addressing it. "
        "Make minimal edits, then verify locally with the allowed port. "
        "Report changed paths and any failure you could not fix."
    )
    return "\n".join(lines)
