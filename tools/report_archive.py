#!/usr/bin/env python3
"""
report_archive.py — Goal 9, Task 68

Long-term searchable archive for session/milestone/digest reports.
Uses SQLite FTS5 for full-text search (stdlib only, no external deps).

DB: state/report_archive.db
Reports indexed from: state/reports/*.md

Commands:
  index            Scan state/reports/ and index any unarchived reports
  search QUERY     Full-text search across all archived reports
  list [--type T] [--since DATE]  List archived report metadata
  get ID           Print full content of archived report by row ID

Usage:
  python3 tools/report_archive.py index
  python3 tools/report_archive.py search "nexus asuka"
  python3 tools/report_archive.py list --type session --since 2026-07-10
  python3 tools/report_archive.py get 7
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("PROJECT_DIR", Path(__file__).parent.parent))
DB_PATH = PROJECT_DIR / "state" / "report_archive.db"
REPORTS_DIR = PROJECT_DIR / "state" / "reports"


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS report_archive (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT NOT NULL,
            report_type  TEXT NOT NULL,
            source_file  TEXT UNIQUE,
            content      TEXT NOT NULL,
            archived_at  TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS reports_fts
        USING fts5(
            date,
            report_type,
            source_file,
            content,
            content='report_archive',
            content_rowid='id',
            tokenize='porter unicode61'
        );

        CREATE TRIGGER IF NOT EXISTS reports_ai
        AFTER INSERT ON report_archive BEGIN
            INSERT INTO reports_fts(rowid, date, report_type, source_file, content)
            VALUES (new.id, new.date, new.report_type, new.source_file, new.content);
        END;

        CREATE TRIGGER IF NOT EXISTS reports_ad
        AFTER DELETE ON report_archive BEGIN
            INSERT INTO reports_fts(reports_fts, rowid, date, report_type, source_file, content)
            VALUES ('delete', old.id, old.date, old.report_type, old.source_file, old.content);
        END;
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_type(filename: str) -> str:
    """Infer report type from filename."""
    name = filename.lower()
    if "session" in name:
        return "session"
    if "milestone" in name:
        return "milestone"
    if "digest" in name:
        return "digest"
    # Date-prefixed session reports like 2026-07-13_1.md
    if re.match(r"\d{4}-\d{2}-\d{2}_\d+\.md", name):
        return "session"
    return "report"


def _infer_date(filename: str, content: str) -> str:
    """Best-effort date extraction: filename prefix, then 'Generated:' line."""
    # Try YYYY-MM-DD prefix
    m = re.match(r"(\d{4}-\d{2}-\d{2})", filename)
    if m:
        return m.group(1)
    # Try 'Generated: YYYY-MM-DD ...' in content
    m = re.search(r"Generated:\s*(\d{4}-\d{2}-\d{2})", content)
    if m:
        return m.group(1)
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_index(conn: sqlite3.Connection, verbose: bool = True) -> int:
    """Scan state/reports/ and archive any new .md files."""
    if not REPORTS_DIR.exists():
        print("No reports directory found.", file=sys.stderr)
        return 0

    already = {
        row["source_file"]
        for row in conn.execute("SELECT source_file FROM report_archive")
    }

    added = 0
    for path in sorted(REPORTS_DIR.glob("*.md")):
        if path.name in already:
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        date = _infer_date(path.name, content)
        rtype = _infer_type(path.name)
        now = datetime.now(tz=__import__('datetime').timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT OR IGNORE INTO report_archive (date, report_type, source_file, content, archived_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (date, rtype, path.name, content, now),
        )
        added += 1
        if verbose:
            print(f"  archived: {path.name} [{rtype}]")

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM report_archive").fetchone()[0]
    print(f"index: +{added} new, {total} total in archive")
    return added


def cmd_search(conn: sqlite3.Connection, query: str, limit: int = 10) -> None:
    """Full-text search over archived reports."""
    if not query.strip():
        print("No query provided.", file=sys.stderr)
        return

    rows = conn.execute(
        """
        SELECT ra.id, ra.date, ra.report_type, ra.source_file,
               snippet(reports_fts, 3, '[', ']', '...', 20) AS excerpt
        FROM reports_fts
        JOIN report_archive ra ON ra.id = reports_fts.rowid
        WHERE reports_fts MATCH ?
        ORDER BY bm25(reports_fts)
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()

    if not rows:
        print(f"No results for: {query!r}")
        return

    print(f"\nSearch results for: {query!r}  ({len(rows)} match(es))\n")
    print(f"{'ID':>4}  {'Date':<12}  {'Type':<10}  {'File':<30}  Excerpt")
    print("-" * 100)
    for row in rows:
        excerpt = row["excerpt"].replace("\n", " ")[:60]
        print(f"{row['id']:>4}  {row['date']:<12}  {row['report_type']:<10}  "
              f"{(row['source_file'] or ''):<30}  {excerpt}")
    print()


def cmd_list(
    conn: sqlite3.Connection,
    report_type: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> None:
    """List archived reports (metadata only)."""
    sql = "SELECT id, date, report_type, source_file, archived_at FROM report_archive WHERE 1=1"
    params: list = []
    if report_type:
        sql += " AND report_type = ?"
        params.append(report_type)
    if since:
        sql += " AND date >= ?"
        params.append(since)
    sql += " ORDER BY date DESC, id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("No archived reports match.")
        return

    print(f"\n{'ID':>4}  {'Date':<12}  {'Type':<10}  {'File':<35}  Archived at")
    print("-" * 90)
    for row in rows:
        print(f"{row['id']:>4}  {row['date']:<12}  {row['report_type']:<10}  "
              f"{(row['source_file'] or ''):<35}  {row['archived_at'][:16]}")
    print(f"\n{len(rows)} report(s) listed.")


def cmd_get(conn: sqlite3.Connection, report_id: int) -> None:
    """Print full content of a report by ID."""
    row = conn.execute(
        "SELECT id, date, report_type, source_file, content FROM report_archive WHERE id = ?",
        (report_id,),
    ).fetchone()
    if not row:
        print(f"No report with id={report_id}", file=sys.stderr)
        return
    print(f"--- Report #{row['id']} | {row['date']} | {row['report_type']} | {row['source_file']} ---\n")
    print(row["content"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report archive — FTS5 search over @Lain session/milestone/digest reports"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("index", help="Scan state/reports/ and archive new files")

    sp = sub.add_parser("search", help="Full-text search over archived reports")
    sp.add_argument("query", help="Search terms (FTS5 syntax supported)")
    sp.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")

    lp = sub.add_parser("list", help="List archived report metadata")
    lp.add_argument("--type", dest="report_type", help="Filter by type: session|milestone|digest|report")
    lp.add_argument("--since", help="Filter by date (YYYY-MM-DD)")
    lp.add_argument("--limit", type=int, default=50)

    gp = sub.add_parser("get", help="Print full content of a report by ID")
    gp.add_argument("id", type=int)

    args = parser.parse_args()
    conn = _connect()

    if args.command == "index":
        cmd_index(conn)
    elif args.command == "search":
        cmd_search(conn, args.query, args.limit)
    elif args.command == "list":
        cmd_list(conn, args.report_type, args.since, args.limit)
    elif args.command == "get":
        cmd_get(conn, args.id)

    conn.close()


if __name__ == "__main__":
    main()
