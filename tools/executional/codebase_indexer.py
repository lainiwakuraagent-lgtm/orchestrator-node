#!/usr/bin/env python3
"""
codebase_indexer.py — Generate a structural map of a codebase.

Walks tools/, scripts/, src/ and produces a compact Markdown brief
(~300-500 tokens) covering directory structure, entry points, and
key functions/classes. AST-based for Python; regex for Bash. No LLM.

Usage:
  python3 tools/codebase_indexer.py [project_dir] [--output path]

  project_dir   Project root (default: current directory)
  --output      Output path (default: memory/codebase_briefs/<name>.md)
  --stdout      Print to stdout instead of writing file
  --max-files   Max files per directory shown (default: 20)
"""

import ast
import argparse
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# Directories to walk with their modes
SCAN_DIRS = [
    ("tools", "python+bash"),
    ("scripts", "bash"),
    ("src", "python"),
]
# Directories to list filenames only
LIST_ONLY_DIRS = ["prompts", "config"]

MAX_ITEMS_PER_FILE = 6
MAX_FILES_PER_DIR = 20
TOKEN_BUDGET = 600  # rough target; enforced by capping


def extract_python_info(path):
    """Extract top-level function/class names and first docstring line via ast."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (SyntaxError, OSError):
        return ["  # (parse error)"]

    items = []
    # Module-level docstring
    module_doc = ast.get_docstring(tree, clean=False)
    if module_doc:
        first_line = module_doc.split("\n")[0].strip()[:80]
        if first_line:
            items.append(f"  # {first_line}")

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False) or ""
            doc_line = doc.split("\n")[0].strip()[:60] if doc else ""
            suffix = f"  — {doc_line}" if doc_line else ""
            items.append(f"  def {node.name}(){suffix}")
            if len(items) >= MAX_ITEMS_PER_FILE:
                break
        elif isinstance(node, ast.ClassDef):
            items.append(f"  class {node.name}")
            # Add immediate methods
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not child.name.startswith("_") or child.name == "__init__":
                        items.append(f"    def {child.name}()")
                if len(items) >= MAX_ITEMS_PER_FILE:
                    break
            if len(items) >= MAX_ITEMS_PER_FILE:
                break

    return items[:MAX_ITEMS_PER_FILE]


def extract_bash_info(path):
    """Extract first description comment and function names from a Bash script."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    items = []
    # First meaningful comment (skip shebang + blank lines)
    for line in lines[:15]:
        stripped = line.strip()
        if stripped.startswith("#") and not stripped.startswith("#!"):
            comment = stripped.lstrip("#").strip()
            if comment and len(comment) > 3:
                items.append(f"  # {comment[:80]}")
                break

    # Function names
    for line in lines:
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\)\s*\{?", line)
        if m:
            fname = m.group(1)
            if fname not in ("if", "while", "for", "case"):
                items.append(f"  function {fname}()")
        if len(items) >= MAX_ITEMS_PER_FILE:
            break

    return items[:MAX_ITEMS_PER_FILE]


def scan_directory(project_dir, subdir, mode, max_files):
    """Return list of (filename, items) for a directory."""
    target = project_dir / subdir
    if not target.exists():
        return []

    results = []
    if "python" in mode:
        py_files = sorted(target.glob("*.py"))
    else:
        py_files = []
    if "bash" in mode:
        sh_files = sorted(target.glob("*.sh"))
    else:
        sh_files = []

    all_files = py_files + sh_files
    # Sort: .py before .sh, then alphabetically within each group
    for f in all_files[:max_files]:
        if f.suffix == ".py":
            items = extract_python_info(f)
        else:
            items = extract_bash_info(f)
        results.append((f.name, items))

    return results


def list_directory(project_dir, subdir):
    """Return filenames only for a directory."""
    target = project_dir / subdir
    if not target.exists():
        return []
    files = [f.name for f in sorted(target.iterdir()) if f.is_file()]
    return files


def detect_entry_points(project_dir):
    """Heuristic: common entry point file names."""
    candidates = [
        ("scripts/executional/wake.sh", "main launcher"),
        ("scripts/conversational/conversation.sh", "conversational layer"),
        ("scripts/orchestrator.py", "task orchestrator"),
        ("tools/executional/session_trigger_server.py", "manual trigger server"),
        ("tools/conversational/telegram_webhook_handler.py", "Telegram webhook handler"),
        ("src/main.py", "main entry point"),
        ("main.py", "main entry point"),
        ("app.py", "application entry point"),
    ]
    found = []
    for rel, desc in candidates:
        if (project_dir / rel).exists():
            found.append((rel, desc))
    return found


def estimate_tokens(text):
    """Rough token estimate: chars / 4."""
    return len(text) // 4


def build_brief(project_dir, max_files=MAX_FILES_PER_DIR):
    project_dir = Path(project_dir).resolve()
    name = project_dir.name
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [f"# Codebase Brief: {name}", f"Generated: {now}", ""]

    # Directory map
    dir_counts = []
    for subdir, _ in SCAN_DIRS:
        target = project_dir / subdir
        if target.exists():
            n = len(list(target.iterdir()))
            dir_counts.append(f"{subdir}/ ({n} files)")
    for subdir in LIST_ONLY_DIRS:
        target = project_dir / subdir
        if target.exists():
            n = len(list(target.iterdir()))
            dir_counts.append(f"{subdir}/ ({n} files)")
    if dir_counts:
        lines.append("## Directory map")
        lines.append("   ".join(dir_counts))
        lines.append("")

    # Entry points
    entry_points = detect_entry_points(project_dir)
    if entry_points:
        lines.append("## Entry points")
        for rel, desc in entry_points:
            lines.append(f"- {rel} — {desc}")
        lines.append("")

    # Key modules per scanned directory
    for subdir, mode in SCAN_DIRS:
        files = scan_directory(project_dir, subdir, mode, max_files)
        if not files:
            continue
        lines.append(f"## {subdir}/")
        lines.append("")
        for fname, items in files:
            lines.append(f"### {fname}")
            lines.extend(items)
            lines.append("")

    # List-only directories
    for subdir in LIST_ONLY_DIRS:
        filenames = list_directory(project_dir, subdir)
        if not filenames:
            continue
        lines.append(f"## {subdir}/")
        lines.append("  " + "  |  ".join(filenames[:15]))
        lines.append("")

    brief = "\n".join(lines)

    # Token budget enforcement: if over budget, trim to entry points + directory map only
    if estimate_tokens(brief) > TOKEN_BUDGET * 1.5:
        trimmed = []
        for line in lines:
            trimmed.append(line)
            if estimate_tokens("\n".join(trimmed)) > TOKEN_BUDGET:
                trimmed.append("...")
                trimmed.append("(brief truncated — run codebase_indexer.py for full output)")
                break
        brief = "\n".join(trimmed)

    return brief


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", nargs="?", default=".", help="Project root directory")
    parser.add_argument("--output", help="Output file path")
    parser.add_argument("--stdout", action="store_true", help="Print to stdout only")
    parser.add_argument("--max-files", type=int, default=MAX_FILES_PER_DIR)
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.exists():
        print(f"codebase_indexer: directory not found: {project_dir}")
        raise SystemExit(1)

    brief = build_brief(project_dir, max_files=args.max_files)

    if args.stdout:
        print(brief)
        return

    if args.output:
        out_path = Path(args.output)
    else:
        briefs_dir = project_dir / "memory" / "codebase_briefs"
        briefs_dir.mkdir(parents=True, exist_ok=True)
        out_path = briefs_dir / f"{project_dir.name}.md"

    out_path.write_text(brief, encoding="utf-8")
    tokens = estimate_tokens(brief)
    print(f"codebase_indexer: wrote {out_path} (~{tokens} tokens)")


if __name__ == "__main__":
    main()
