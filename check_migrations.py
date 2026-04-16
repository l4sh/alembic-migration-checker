#!/usr/bin/env python3
"""Alembic migration dependency checker.

Parses migration files via AST (no DB required) and validates the
dependency graph for issues that would break `alembic upgrade head`.

Exit codes:
    0 — no issues
    1 — warnings only
    2 — at least one error
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass
class MigrationInfo:
    file_path: Path
    revision: str
    down_revision: Union[Tuple[str, ...], str, None] = None
    branch_labels: Union[Tuple[str, ...], str, None] = None
    depends_on: Union[Tuple[str, ...], str, None] = None


@dataclass
class Issue:
    severity: Severity
    check: str
    message: str
    file: Optional[str] = None


# ---------------------------------------------------------------------------
# AST parsing
# ---------------------------------------------------------------------------

_TARGET_VARS = {"revision", "down_revision", "branch_labels", "depends_on"}


def _extract_value(node: ast.expr) -> Union[str, Tuple[str, ...], None]:
    """Extract a constant string, None, or tuple of strings from an AST node."""
    if isinstance(node, ast.Constant):
        if node.value is None:
            return None
        if isinstance(node.value, str):
            return node.value
    if isinstance(node, ast.Tuple):
        elements: list[str] = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                elements.append(elt.value)
        if elements:
            return tuple(elements)
    return None


def parse_migration_file(path: Path) -> Optional[MigrationInfo]:
    """Parse a single migration file and extract revision metadata."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, OSError) as exc:
        print(f"  SKIP  {path.name}: {exc}", file=sys.stderr)
        return None

    values: dict[str, Union[str, Tuple[str, ...], None]] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in _TARGET_VARS
        ):
            values[node.targets[0].id] = _extract_value(node.value)

    revision = values.get("revision")
    if not isinstance(revision, str):
        return None  # not a migration file (e.g. __init__.py)

    return MigrationInfo(
        file_path=path,
        revision=revision,
        down_revision=values.get("down_revision"),
        branch_labels=values.get("branch_labels"),
        depends_on=values.get("depends_on"),
    )


def load_migrations(dir_path: Path) -> List[MigrationInfo]:
    """Load all migration files from a directory."""
    migrations: list[MigrationInfo] = []
    for path in sorted(dir_path.glob("*.py")):
        if path.name.startswith("__"):
            continue
        info = parse_migration_file(path)
        if info is not None:
            migrations.append(info)
    return migrations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_list(val: Union[str, Tuple[str, ...], None]) -> List[str]:
    """Normalize a revision reference to a flat list of strings."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    return list(val)


# ---------------------------------------------------------------------------
# Graph analysis
# ---------------------------------------------------------------------------

@dataclass
class MigrationGraph:
    migrations: List[MigrationInfo]
    branch_mode: str = "error"
    merge_mode: str = "error"
    ordering_mode: str = "off"

    # built in __post_init__
    by_revision: Dict[str, MigrationInfo] = field(default_factory=dict, repr=False)
    successors: Dict[str, List[str]] = field(default_factory=lambda: defaultdict(list), repr=False)
    parents: Dict[str, List[str]] = field(default_factory=dict, repr=False)
    _duplicate_issues: List[Issue] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        # Build lookup, detect duplicates
        for m in self.migrations:
            if m.revision in self.by_revision:
                other = self.by_revision[m.revision]
                self._duplicate_issues.append(Issue(
                    severity=Severity.ERROR,
                    check="duplicate_revision",
                    message=(
                        f"Duplicate revision '{m.revision}' in "
                        f"{m.file_path.name} and {other.file_path.name}"
                    ),
                    file=m.file_path.name,
                ))
            else:
                self.by_revision[m.revision] = m

        # Build edges
        self.successors = defaultdict(list)
        for m in self.migrations:
            if m.revision not in self.by_revision:
                continue  # skip duplicate
            parent_ids = _to_list(m.down_revision) + _to_list(m.depends_on)
            self.parents[m.revision] = parent_ids
            for pid in parent_ids:
                self.successors[pid].append(m.revision)

    # -- individual checks --------------------------------------------------

    def _check_duplicates(self) -> List[Issue]:
        return list(self._duplicate_issues)

    def _check_missing_dependencies(self) -> List[Issue]:
        issues: list[Issue] = []
        for rev, parent_ids in self.parents.items():
            m = self.by_revision[rev]
            for pid in parent_ids:
                if pid not in self.by_revision:
                    issues.append(Issue(
                        severity=Severity.ERROR,
                        check="missing_dependency",
                        message=f"Migration '{rev}' references unknown dependency '{pid}'",
                        file=m.file_path.name,
                    ))
        return issues

    def _check_cycles(self) -> List[Issue]:
        """Kahn's algorithm — nodes left after topo-sort are in cycles."""
        in_degree: dict[str, int] = {rev: 0 for rev in self.by_revision}
        for rev, parent_ids in self.parents.items():
            for pid in parent_ids:
                if pid in self.by_revision:
                    in_degree[rev] = in_degree.get(rev, 0) + 1

        queue = deque(rev for rev, deg in in_degree.items() if deg == 0)
        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for succ in self.successors.get(node, []):
                if succ in in_degree:
                    in_degree[succ] -= 1
                    if in_degree[succ] == 0:
                        queue.append(succ)

        if visited < len(self.by_revision):
            cycle_nodes = [rev for rev, deg in in_degree.items() if deg > 0]
            return [Issue(
                severity=Severity.ERROR,
                check="circular_dependency",
                message=f"Circular dependency involving {len(cycle_nodes)} migration(s): {', '.join(cycle_nodes[:10])}",
            )]
        return []

    def _get_heads(self) -> List[str]:
        """Revisions with no successors."""
        all_revisions = set(self.by_revision.keys())
        has_successor = {pid for succs in self.successors.values() for pid in succs if pid in all_revisions}
        # A head is a revision that never appears as a parent (i.e. no successor points to it)
        # Actually: a head has no successors — no one lists it as their parent
        heads: list[str] = []
        for rev in all_revisions:
            succs = [s for s in self.successors.get(rev, []) if s in self.by_revision]
            if not succs:
                heads.append(rev)
        return sorted(heads)

    def _get_bases(self) -> List[str]:
        """Revisions with no parents (down_revision is None)."""
        return sorted(
            rev for rev, pids in self.parents.items()
            if not pids
        )

    def _check_divergent_heads(self) -> List[Issue]:
        heads = self._get_heads()
        if len(heads) > 1:
            head_details = ", ".join(
                f"{h} ({self.by_revision[h].file_path.name})" for h in heads
            )
            return [Issue(
                severity=Severity.ERROR,
                check="divergent_heads",
                message=f"Found {len(heads)} heads (expected 1): {head_details}",
            )]
        return []

    def _check_branching(self) -> List[Issue]:
        severity = Severity.ERROR if self.branch_mode == "error" else Severity.WARNING
        issues: list[Issue] = []
        for rev in self.by_revision:
            succs = [s for s in self.successors.get(rev, []) if s in self.by_revision]
            if len(succs) > 1:
                m = self.by_revision[rev]
                succ_list = ", ".join(succs)
                issues.append(Issue(
                    severity=severity,
                    check="branching",
                    message=f"Revision '{rev}' has {len(succs)} successors (fork point): {succ_list}",
                    file=m.file_path.name,
                ))
        return issues

    def _check_merge_migrations(self) -> List[Issue]:
        severity = Severity.ERROR if self.merge_mode == "error" else Severity.WARNING
        issues: list[Issue] = []
        for m in self.by_revision.values():
            down_list = _to_list(m.down_revision)
            if len(down_list) > 1:
                issues.append(Issue(
                    severity=severity,
                    check="merge_migration",
                    message=f"Migration '{m.revision}' is a merge of {len(down_list)} parents: {', '.join(down_list)}",
                    file=m.file_path.name,
                ))
        return issues

    def _topo_order(self) -> List[str]:
        """Return revisions in topological order (Kahn's). Empty if cycles exist."""
        in_degree: dict[str, int] = {rev: 0 for rev in self.by_revision}
        for rev, parent_ids in self.parents.items():
            for pid in parent_ids:
                if pid in self.by_revision:
                    in_degree[rev] = in_degree.get(rev, 0) + 1

        queue = deque(rev for rev, deg in in_degree.items() if deg == 0)
        order: list[str] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for succ in sorted(self.successors.get(node, [])):
                if succ in in_degree:
                    in_degree[succ] -= 1
                    if in_degree[succ] == 0:
                        queue.append(succ)
        return order

    def _check_file_ordering(self) -> List[Issue]:
        """Check that filename alphabetical order matches dependency order."""
        if self.ordering_mode == "off":
            return []

        severity = Severity.ERROR if self.ordering_mode == "error" else Severity.WARNING
        topo = self._topo_order()
        if len(topo) != len(self.by_revision):
            return []  # cycles present, skip — already reported

        # Map revision -> position in dependency order
        topo_pos = {rev: i for i, rev in enumerate(topo)}
        # Map revision -> filename for sorting
        file_order = sorted(
            self.by_revision.keys(),
            key=lambda rev: self.by_revision[rev].file_path.name,
        )

        issues: list[Issue] = []
        for rev, parent_ids in self.parents.items():
            m = self.by_revision[rev]
            for pid in parent_ids:
                if pid not in self.by_revision:
                    continue
                parent_m = self.by_revision[pid]
                # Child filename must sort after parent filename
                if m.file_path.name <= parent_m.file_path.name:
                    issues.append(Issue(
                        severity=severity,
                        check="file_ordering",
                        message=(
                            f"Migration '{rev}' depends on '{pid}' but its filename "
                            f"sorts before its parent: {m.file_path.name} <= {parent_m.file_path.name}"
                        ),
                        file=m.file_path.name,
                    ))
        return issues

    def _check_orphans(self) -> List[Issue]:
        """Migrations unreachable from any base via forward traversal."""
        bases = self._get_bases()
        visited: set[str] = set()
        queue = deque(bases)
        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            for succ in self.successors.get(node, []):
                if succ in self.by_revision and succ not in visited:
                    queue.append(succ)

        orphans = set(self.by_revision.keys()) - visited
        # Don't flag orphans whose parents are missing — already caught by missing_dependency
        missing_parent_revs = {
            rev for rev, pids in self.parents.items()
            if any(pid not in self.by_revision for pid in pids)
        }
        true_orphans = orphans - missing_parent_revs

        issues: list[Issue] = []
        for rev in sorted(true_orphans):
            m = self.by_revision[rev]
            issues.append(Issue(
                severity=Severity.ERROR,
                check="orphan",
                message=f"Migration '{rev}' is unreachable from any base",
                file=m.file_path.name,
            ))
        return issues

    # -- run all checks ------------------------------------------------------

    def check_all(self) -> List[Issue]:
        issues: list[Issue] = []
        issues.extend(self._check_duplicates())
        issues.extend(self._check_missing_dependencies())
        issues.extend(self._check_cycles())
        issues.extend(self._check_divergent_heads())
        issues.extend(self._check_branching())
        issues.extend(self._check_merge_migrations())
        issues.extend(self._check_orphans())
        issues.extend(self._check_file_ordering())
        return issues


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def format_report(graph: MigrationGraph, issues: List[Issue]) -> str:
    lines: list[str] = []
    lines.append("=== Migration Check Report ===")
    lines.append("")

    # Summary
    heads = graph._get_heads()
    bases = graph._get_bases()
    lines.append("Summary:")
    lines.append(f"  Total migrations: {len(graph.by_revision)}")

    if bases:
        base_strs = [
            f"{b} ({graph.by_revision[b].file_path.name})" for b in bases
        ]
        lines.append(f"  Base(s): {', '.join(base_strs)}")
    else:
        lines.append("  Base(s): (none)")

    if heads:
        head_strs = [
            f"{h} ({graph.by_revision[h].file_path.name})" for h in heads
        ]
        lines.append(f"  Head(s): {', '.join(head_strs)}")
    else:
        lines.append("  Head(s): (none)")

    lines.append("")

    # Issues
    errors = [i for i in issues if i.severity == Severity.ERROR]
    warnings = [i for i in issues if i.severity == Severity.WARNING]
    lines.append(f"Issues: ({len(errors)} error(s), {len(warnings)} warning(s))")
    lines.append("")

    if not issues:
        lines.append("  No issues found. Migration chain is clean.")
    else:
        for issue in issues:
            tag = issue.severity.value.upper()
            lines.append(f"  {tag} [{issue.check}] {issue.message}")
            if issue.file:
                lines.append(f"    File: {issue.file}")
            lines.append("")

    lines.append("")

    # Result
    if errors:
        lines.append(f"Result: FAIL ({len(errors)} error(s))")
    elif warnings:
        lines.append(f"Result: WARN ({len(warnings)} warning(s))")
    else:
        lines.append("Result: PASS")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Alembic migration files for dependency issues.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes:\n"
            "  0  no issues\n"
            "  1  warnings only\n"
            "  2  at least one error\n"
        ),
    )
    parser.add_argument(
        "migrations_dir",
        type=Path,
        help="Path to the Alembic versions directory",
    )
    parser.add_argument(
        "--branch-mode",
        choices=["warn", "error"],
        default="error",
        help="Treat branching migrations as warn or error (default: error)",
    )
    parser.add_argument(
        "--merge-mode",
        choices=["warn", "error"],
        default="error",
        help="Treat merge migrations as warn or error (default: error)",
    )
    parser.add_argument(
        "--ordering-mode",
        choices=["off", "warn", "error"],
        default="off",
        help="Check filename order matches dependency order (default: off)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    migrations_dir: Path = args.migrations_dir
    if not migrations_dir.is_dir():
        print(f"Error: '{migrations_dir}' is not a directory", file=sys.stderr)
        return 2

    migrations = load_migrations(migrations_dir)
    if not migrations:
        print("No migration files found.")
        return 0

    graph = MigrationGraph(
        migrations=migrations,
        branch_mode=args.branch_mode,
        merge_mode=args.merge_mode,
        ordering_mode=args.ordering_mode,
    )
    issues = graph.check_all()

    report = format_report(graph, issues)
    print(report)

    errors = [i for i in issues if i.severity == Severity.ERROR]
    warnings = [i for i in issues if i.severity == Severity.WARNING]

    if errors:
        return 2
    if warnings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
