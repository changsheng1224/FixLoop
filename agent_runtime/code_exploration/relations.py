"""Bounded, task-local relationships derived from already observed Python files."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from uuid import uuid4

MAX_FILES = 8
MAX_NODES = 64
MAX_EDGES = 128


@dataclass(frozen=True)
class ObservedEvidence:
    observation_id: str
    tool: str
    args: dict
    hits: tuple[dict, ...]
    versions: dict[str, str]
    observed_at: str
    retrieval_result: dict = field(default_factory=dict)


@dataclass
class ParsedFile:
    symbols: list[dict] = field(default_factory=list)
    imports: list[dict] = field(default_factory=list)


def _symbol(path: str, qualname: str, kind: str, node: ast.AST) -> dict:
    line = int(getattr(node, "lineno", 1))
    end = int(getattr(node, "end_lineno", line))
    return {
        "id": f"symbol:{path}:{qualname}:{kind}:{line}",
        "kind": "symbol",
        "path": path,
        "qualified_name": qualname,
        "symbol_kind": kind,
        "range": {"start_line": line, "end_line": end + 1},
    }


def parse_python(path: str, content: str) -> ParsedFile:
    """Extract syntax facts without guessing import resolution or runtime calls."""
    tree = ast.parse(content, filename=path)
    parsed = ParsedFile()

    def visit(body: list[ast.stmt], parents: tuple[str, ...] = ()) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                qualname = ".".join((*parents, node.name))
                parsed.symbols.append(_symbol(path, qualname, kind, node))
                visit(node.body, (*parents, node.name))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parsed.imports.append(
                        {
                            "module": alias.name,
                            "name": "",
                            "alias": alias.asname or "",
                            "level": 0,
                            "line": node.lineno,
                        }
                    )
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    parsed.imports.append(
                        {
                            "module": node.module or "",
                            "name": alias.name,
                            "alias": alias.asname or "",
                            "level": node.level,
                            "line": node.lineno,
                        }
                    )

    visit(tree.body)
    return parsed


def _import_target(source: str, imp: dict, covered: set[str]) -> str:
    module = imp["module"]
    level = int(imp["level"])
    if level:
        parent = PurePosixPath(source).parent
        for _ in range(level - 1):
            parent = parent.parent
        base = "" if str(parent) == "." else str(parent) + "/"
    else:
        base = ""
    module_path = module.replace(".", "/")
    candidates = [
        f"{base}{module_path}.py" if module_path else "",
        f"{base}{module_path}/__init__.py" if module_path else "",
    ]
    name = imp["name"]
    if name and name != "*":
        stem = f"{base}{module_path}/" if module_path else base
        candidates.extend([f"{stem}{name}.py", f"{stem}{name}/__init__.py"])
    return next((candidate for candidate in candidates if candidate in covered), "")


def build_view(
    evidence: list[ObservedEvidence],
    parsed: dict[str, ParsedFile],
    *,
    epoch: str,
    revision: int,
    max_files: int = MAX_FILES,
) -> dict:
    """Build one-hop file/symbol relations over the observed file set only."""
    ordered: list[str] = []
    refs: dict[str, list[str]] = {}
    observed_at: dict[str, str] = {}
    versions: dict[str, str] = {}
    for item in evidence:
        for path, digest in item.versions.items():
            if not path.endswith(".py") or not digest:
                continue
            if path not in ordered:
                ordered.append(path)
            refs.setdefault(path, []).append(item.observation_id)
            observed_at[path] = item.observed_at
            versions[path] = digest
        for hit in item.hits:
            path = str(hit.get("path", ""))
            if path in versions:
                ordered.remove(path)
                ordered.insert(0, path)
    selected = ordered[:max_files]
    covered = set(selected)
    seed_refs: dict[str, str] = {}
    for item in evidence:
        for hit in item.hits:
            path = str(hit.get("path", ""))
            if path in covered:
                seed_refs[path] = item.observation_id
    nodes: list[dict] = []
    edges: list[dict] = []
    for path in selected:
        nodes.append({"id": f"file:{path}", "kind": "file", "path": path})
    for path in selected:
        data = parsed.get(path)
        if data is None:
            continue
        for symbol in data.symbols:
            if len(nodes) >= MAX_NODES or len(edges) >= MAX_EDGES:
                break
            nodes.append(symbol)
            edges.append(
                {
                    "kind": "contains",
                    "from": f"file:{path}",
                    "to": symbol["id"],
                    "source": "ast",
                    "resolution": "syntactic",
                    "observation_refs": refs.get(path, []),
                    "path": path,
                    "range": symbol["range"],
                    "dependency_versions": {path: versions[path]},
                    "observed_at": observed_at.get(path, ""),
                }
            )
        for imp in data.imports:
            if len(edges) >= MAX_EDGES:
                break
            target = _import_target(path, imp, covered)
            if not target:
                module_id = f"module:{imp['module'] or imp['name']}"
                if not any(node["id"] == module_id for node in nodes):
                    if len(nodes) >= MAX_NODES:
                        break
                    nodes.append(
                        {
                            "id": module_id,
                            "kind": "module",
                            "path": "",
                            "module": imp["module"] or imp["name"],
                        }
                    )
            kind = "test_imports" if PurePosixPath(path).name.startswith("test_") else "imports"
            edges.append(
                {
                    "kind": kind,
                    "from": f"file:{path}",
                    "to": f"file:{target}" if target else module_id,
                    "source": "ast",
                    "resolution": "candidate" if target else "unresolved",
                    "observation_refs": refs.get(path, []),
                    "path": path,
                    "range": {"start_line": imp["line"], "end_line": imp["line"] + 1},
                    "dependency_versions": (
                        {path: versions[path], target: versions[target]}
                        if target
                        else {path: versions[path]}
                    ),
                    "observed_at": observed_at.get(path, ""),
                }
            )
    for item in evidence:
        if item.tool != "code_lookup" or item.args.get("operation") != "references":
            continue
        anchor = str(item.args.get("path", "")).replace("\\", "/")
        if anchor not in covered:
            continue
        for hit in item.hits:
            target = hit.get("path", "")
            if target not in covered or len(edges) >= MAX_EDGES:
                continue
            edges.append(
                {
                    "kind": "references",
                    "from": f"file:{target}",
                    "to": f"file:{anchor}",
                    "source": hit.get("source", "text"),
                    "resolution": hit.get("resolution", "candidate"),
                    "observation_refs": [item.observation_id],
                    "path": target,
                    "range": hit.get("range"),
                    "dependency_versions": {
                        path: digest for path, digest in item.versions.items() if path in covered
                    },
                    "observed_at": item.observed_at,
                }
            )
    inclusion_paths = []
    for path in selected:
        incoming = next(
            (
                edge
                for edge in edges
                if edge["kind"] in {"imports", "test_imports"} and edge["to"] == f"file:{path}"
            ),
            None,
        )
        if incoming is not None:
            inclusion_paths.append(
                {
                    "seed": incoming["path"],
                    "relation": incoming["kind"],
                    "target": path,
                    "reason": "observed_import_target",
                    "observation_refs": list(
                        dict.fromkeys([*incoming["observation_refs"], *refs.get(path, [])])
                    ),
                }
            )
        else:
            inclusion_paths.append(
                {
                    "seed": path,
                    "relation": "observation",
                    "target": path,
                    "reason": "retrieval_hit" if path in seed_refs else "observed_dependency",
                    "observation_refs": refs.get(path, []),
                }
            )
    return {
        "epoch": epoch,
        "view_revision": revision,
        "observation_refs": list(
            dict.fromkeys(oid for path in selected for oid in refs.get(path, []))
        ),
        "dependency_versions": {path: versions[path] for path in selected},
        "covered_files": selected,
        "inclusion_paths": inclusion_paths,
        "coverage": {
            "observed_files": len(ordered),
            "included_files": len(selected),
            "nodes": len(nodes),
            "edges": len(edges),
        },
        "truncation_reasons": (["max_files"] if len(ordered) > len(selected) else [])
        + (["max_nodes"] if len(nodes) >= MAX_NODES else [])
        + (["max_edges"] if len(edges) >= MAX_EDGES else []),
        "nodes": nodes,
        "edges": edges,
    }


def new_epoch() -> str:
    return uuid4().hex
