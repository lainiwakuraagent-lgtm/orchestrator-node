#!/usr/bin/env python3
"""
memory_search.py — Search across the agent memory corpus.

Built 2026-07-07 by @Lain.
Motivation: each session I read the same 3 static files. "What did I decide about X?"
had no fast answer. This is that fast answer.

Usage:
  python3 tools/memory_search.py <query> [options]

Options:
  --scope all|sessions|work|core|logs   (default: all)
  --context N                           lines of context around each match (default: 2)
  --case-sensitive                      (default: case-insensitive)
  --max N                               max matches to display (default: 100)
  --files-only                          show only filenames, no line content

Examples:
  python3 tools/memory_search.py "tailscale ssh"
  python3 tools/memory_search.py "ideapad-5" --scope sessions
  python3 tools/memory_search.py "Phase 4" --context 3 --scope work
  python3 tools/memory_search.py "relationship_update" --files-only
"""

import os
import re
import sys
import argparse
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("PROJECT_DIR", Path(__file__).resolve().parent.parent.parent))
MEMORY_DIR = PROJECT_DIR / "memory"

SCOPES = {
    "all": [
        MEMORY_DIR / "sessions",
        MEMORY_DIR / "work",
        MEMORY_DIR / "learnings.md",
        MEMORY_DIR / "learnings_digest.md",
        MEMORY_DIR / "progress.md",
        MEMORY_DIR / "latest_summary.md",
        MEMORY_DIR / "narrative_log.md",
        MEMORY_DIR / "index.md",
        MEMORY_DIR / "conversation.md",
    ],
    "sessions": [MEMORY_DIR / "sessions"],
    "work": [MEMORY_DIR / "work"],
    "core": [
        MEMORY_DIR / "learnings_digest.md",
        MEMORY_DIR / "progress.md",
        MEMORY_DIR / "latest_summary.md",
        MEMORY_DIR / "narrative_log.md",
    ],
    "logs": [PROJECT_DIR / "logs"],
}

READABLE_SUFFIXES = {".md", ".txt", ".csv", ".log", ".sh", ".py"}


def collect_files(paths):
    """Collect all readable files from a list of paths (files or directories)."""
    files = []
    for p in paths:
        if not p.exists():
            continue
        if p.is_file() and p.suffix in READABLE_SUFFIXES:
            files.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix in READABLE_SUFFIXES:
                    files.append(f)
    return files


def search_file(path, pattern, context_lines):
    """Return list of match records for a file."""
    try:
        text = path.read_text(errors="replace")
        lines = text.splitlines()
    except Exception:
        return []

    results = []
    for i, line in enumerate(lines):
        if pattern.search(line):
            start = max(0, i - context_lines)
            end = min(len(lines), i + context_lines + 1)
            results.append({
                "line_num": i + 1,
                "context": lines[start:end],
                "context_start": start + 1,
            })
    return results


def merge_overlapping(matches, context_lines):
    """Merge match records whose context windows overlap."""
    if not matches:
        return []

    merged = []
    current = matches[0]
    current_end = current["context_start"] + len(current["context"]) - 1

    for m in matches[1:]:
        m_start = m["context_start"]
        if m_start <= current_end + 1:
            # Overlapping — extend current block
            new_end = m["context_start"] + len(m["context"]) - 1
            if new_end > current_end:
                # Extend context list
                extra_start = current_end + 1
                extra_end = new_end
                # We'd need the original lines to extend properly.
                # For simplicity, just track which line_nums are matches.
                current["_extra_end"] = extra_end
            current_end = max(current_end, m["context_start"] + len(m["context"]) - 1)
            if "extra_matches" not in current:
                current["extra_matches"] = []
            current["extra_matches"].append(m["line_num"])
        else:
            merged.append(current)
            current = m
            current_end = current["context_start"] + len(current["context"]) - 1

    merged.append(current)
    return merged


def format_separator(char="─", width=52):
    return f"╾{'─' * width}╼"


def main():
    parser = argparse.ArgumentParser(
        description="Search the agent memory corpus",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("query", help="Search term (substring, regex-escaped by default)")
    parser.add_argument(
        "--scope",
        choices=list(SCOPES.keys()),
        default="all",
        help="Which memory scope to search (default: all)",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=2,
        metavar="N",
        help="Lines of context around each match (default: 2)",
    )
    parser.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Case-sensitive search (default: case-insensitive)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=100,
        metavar="N",
        help="Max matches to display (default: 100)",
    )
    parser.add_argument(
        "--files-only",
        action="store_true",
        help="List matching filenames only, no line content",
    )

    args = parser.parse_args()

    flags = 0 if args.case_sensitive else re.IGNORECASE
    try:
        pattern = re.compile(re.escape(args.query), flags)
    except re.error as e:
        print(f"Invalid query: {e}", file=sys.stderr)
        sys.exit(1)

    paths = SCOPES[args.scope]
    files = collect_files(paths)

    sep = format_separator()
    print(sep)
    mode = "case-sensitive" if args.case_sensitive else "case-insensitive"
    print(f'  memory_search: "{args.query}"')
    print(f"  scope={args.scope}  context={args.context}  {mode}")
    print(sep)
    print()

    total_matches = 0
    file_count = 0
    stopped_early = False

    for fpath in files:
        if total_matches >= args.max:
            stopped_early = True
            break

        matches = search_file(fpath, pattern, args.context)
        if not matches:
            continue

        rel = fpath.relative_to(PROJECT_DIR)
        n = len(matches)
        suffix = "es" if n != 1 else ""
        print(f"── {rel}  ({n} match{suffix})")

        if args.files_only:
            total_matches += n
            file_count += 1
            print()
            continue

        print()

        # Merge overlapping context windows for cleaner output
        prev_end = -1
        for m in matches:
            if total_matches >= args.max:
                stopped_early = True
                break

            ctx_start = m["context_start"]
            ctx_end = ctx_start + len(m["context"]) - 1
            match_ln = m["line_num"]

            # Gap separator between non-adjacent context blocks
            if prev_end >= 0 and ctx_start > prev_end + 1:
                print(f"   {'·' * 3}")

            for j, ctx_line in enumerate(m["context"]):
                lineno = ctx_start + j
                is_match = (lineno == match_ln)
                marker = "▶" if is_match else " "
                # Truncate very long lines
                display = ctx_line[:200] + ("…" if len(ctx_line) > 200 else "")
                print(f"   {marker} {lineno:5d} │ {display}")

            prev_end = ctx_end
            total_matches += 1

        print()
        file_count += 1

    if total_matches == 0:
        print("  (no matches found)\n")
    elif stopped_early:
        print(f"  (stopped at {args.max} matches — use --max N for more)\n")

    print(sep)
    file_s = "s" if file_count != 1 else ""
    match_s = "es" if total_matches != 1 else ""
    print(f"  {total_matches} match{match_s} in {file_count} file{file_s}")
    print(sep)


if __name__ == "__main__":
    main()
