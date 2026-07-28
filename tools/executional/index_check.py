#!/usr/bin/env python3
"""index_check.py — Validate file references in memory/index.md.

Parses index.md for `path | date | description` lines, checks
whether each local file path still exists, and reports missing ones.

Usage:
  python3 tools/index_check.py [--verbose] [--missing-only]

  --verbose       Show all paths (default: show summary + missing)
  --missing-only  Only show missing paths, no summary
"""

import argparse
import os
import re
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INDEX_FILE = os.path.join(PROJECT_DIR, "memory", "index.md")
GIT_ROOT = os.path.dirname(PROJECT_DIR)  # /home/andrii/lain

# Lines matching: `<path>` | date | description
INDEX_LINE_RE = re.compile(r"^`([^`]+)`\s*\|")


def classify_path(raw: str) -> tuple[str, str | None]:
    """Return (kind, resolved_path).
    kind: 'local', 'external', 'github_ref', 'unknown'
    resolved_path: absolute path for local, None for others.
    """
    raw = raw.strip()

    if raw.startswith("https://") or raw.startswith("http://"):
        return ("external", None)

    if raw.startswith("[github]") or raw.startswith("[gh]"):
        return ("github_ref", None)

    # andrii-mazurchuk/... or lainiwakuraagent-lgtm/... without [github] prefix
    # heuristic: no leading / and contains exactly one slash in a "user/repo" pattern at start
    if re.match(r'^[a-z][a-z0-9_-]*/[a-z]', raw) and not raw.startswith("/"):
        # Could be a github repo ref or a relative path — check if it has extension
        if not os.path.splitext(raw.split("/")[-1])[1] and "/" in raw[:20]:
            return ("github_ref", None)

    # Absolute path
    if raw.startswith("/"):
        return ("local", raw)

    # Relative path — try PROJECT_DIR first, then GIT_ROOT
    candidate = os.path.join(PROJECT_DIR, raw)
    if os.path.exists(candidate):
        return ("local", candidate)
    git_root_candidate = os.path.join(GIT_ROOT, raw)
    if os.path.exists(git_root_candidate):
        return ("local", git_root_candidate)
    # Return the PROJECT_DIR candidate as the "expected" path for missing report
    return ("local", candidate)


def parse_index(index_file: str) -> list[dict]:
    """Parse index.md and return list of entry dicts."""
    entries = []
    try:
        with open(index_file, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                m = INDEX_LINE_RE.match(line.strip())
                if not m:
                    continue
                raw_path = m.group(1)
                # Extract date from second pipe segment
                parts = line.strip().split("|")
                date = parts[1].strip() if len(parts) > 1 else "?"
                description = parts[2].strip()[:60] if len(parts) > 2 else ""

                kind, resolved = classify_path(raw_path)
                entries.append({
                    "lineno": lineno,
                    "raw": raw_path,
                    "date": date,
                    "description": description,
                    "kind": kind,
                    "resolved": resolved,
                })
    except Exception as e:
        print(f"ERROR reading index: {e}", file=sys.stderr)
        sys.exit(1)
    return entries


def check_entries(entries: list[dict]) -> tuple[list, list, list, list]:
    """Return (present, missing, external, github_ref) lists."""
    present, missing, external, github_refs = [], [], [], []
    for e in entries:
        if e["kind"] == "external":
            external.append(e)
        elif e["kind"] == "github_ref":
            github_refs.append(e)
        elif e["kind"] == "local" and e["resolved"]:
            path = e["resolved"]
            if os.path.exists(path):
                present.append(e)
            else:
                missing.append(e)
        else:
            # unknown
            missing.append(e)
    return present, missing, external, github_refs


def main():
    parser = argparse.ArgumentParser(description="Validate memory/index.md file references")
    parser.add_argument("--verbose", action="store_true", help="Show all entries")
    parser.add_argument("--missing-only", action="store_true", help="Only show missing")
    args = parser.parse_args()

    entries = parse_index(INDEX_FILE)
    present, missing, external, github_refs = check_entries(entries)

    total_local = len(present) + len(missing)
    total = len(entries)

    if not args.missing_only:
        print(f"\nIndex Check — memory/index.md")
        print(f"{'=' * 50}")
        print(f"  Total entries:   {total}")
        print(f"  Local paths:     {total_local}  ({len(present)} present, {len(missing)} missing)")
        print(f"  External URLs:   {len(external)}")
        print(f"  GitHub refs:     {len(github_refs)}")
        print()

    if missing:
        print(f"MISSING ({len(missing)}):")
        for e in missing:
            rel = os.path.relpath(e["resolved"], PROJECT_DIR) if e["resolved"] else e["raw"]
            print(f"  [{e['date']}] {rel}")
            if args.verbose:
                print(f"           {e['description']}")
    elif not args.missing_only:
        print("MISSING: none — all local paths exist.")

    if args.verbose and not args.missing_only:
        print(f"\nPRESENT ({len(present)}):")
        for e in present:
            rel = os.path.relpath(e["resolved"], PROJECT_DIR) if e["resolved"] else e["raw"]
            print(f"  [{e['date']}] {rel}")

        if external:
            print(f"\nEXTERNAL ({len(external)}) — not checked:")
            for e in external:
                print(f"  [{e['date']}] {e['raw'][:72]}")

        if github_refs:
            print(f"\nGITHUB REFS ({len(github_refs)}) — not checked:")
            for e in github_refs:
                print(f"  [{e['date']}] {e['raw'][:72]}")

    if missing:
        sys.exit(1)


if __name__ == "__main__":
    main()
