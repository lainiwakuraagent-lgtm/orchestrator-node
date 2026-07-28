#!/usr/bin/env python3
"""
analytics_search.py — Semantic search over embedded session data.

Embeds a query via Ollama (nomic-embed-text) and finds similar sessions
using sqlite-vec KNN search over session_embeddings.

Usage:
  python3 tools/analytics_search.py "query string" [--top N] [--json]

  --top N      Return top N results (default: 5)
  --json       Output as JSON instead of markdown table
"""

import json
import sqlite3
import struct
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
DB_PATH = PROJECT_DIR / "logs" / "analytics.db"
VENV_PYTHON = PROJECT_DIR / "memory" / "work" / ".sqlite_vec_venv" / "bin" / "python"
OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIMS = 768


def re_exec_in_venv():
    if sys.executable != str(VENV_PYTHON):
        if not VENV_PYTHON.exists():
            print("analytics_search: venv not found — run session_embed.py first", file=sys.stderr)
            sys.exit(1)
        result = subprocess.run([str(VENV_PYTHON), __file__] + sys.argv[1:])
        sys.exit(result.returncode)


def get_embedding(text):
    payload = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data.get("embedding")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        print(f"analytics_search: Ollama error — {e}", file=sys.stderr)
        return None


def embedding_to_blob(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def load_vec_extension(conn):
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def search(query_text, top_k=5, as_json=False):
    import sqlite_vec  # ensure venv

    vec = get_embedding(query_text)
    if vec is None:
        print("analytics_search: could not embed query — is Ollama running?", file=sys.stderr)
        sys.exit(1)
    if len(vec) != EMBED_DIMS:
        print(f"analytics_search: unexpected embedding dims {len(vec)}", file=sys.stderr)
        sys.exit(1)

    blob = embedding_to_blob(vec)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    load_vec_extension(conn)

    # Check if session_embeddings exists
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_embeddings'"
    ).fetchone()
    if not exists:
        print("analytics_search: session_embeddings table not found.", file=sys.stderr)
        print("  Run: python3 tools/session_embed.py --limit 100", file=sys.stderr)
        conn.close()
        sys.exit(1)

    embedded_count = conn.execute("SELECT COUNT(*) FROM session_embeddings").fetchone()[0]
    if embedded_count == 0:
        print("analytics_search: no embeddings yet — run session_embed.py first", file=sys.stderr)
        conn.close()
        sys.exit(1)

    rows = conn.execute("""
        SELECT
            s.session_key,
            s.started_at,
            s.session_type,
            s.summary,
            s.tasks_completed,
            e.distance
        FROM session_embeddings e
        JOIN sessions s ON s.id = e.session_id
        WHERE e.embedding MATCH ?
          AND k = ?
        ORDER BY e.distance
    """, (blob, top_k)).fetchall()

    conn.close()

    results = [dict(r) for r in rows]

    if as_json:
        print(json.dumps(results, indent=2))
        return

    if not results:
        print("analytics_search: no results found")
        return

    # Markdown table
    print(f"\n### Semantic search: '{query_text}' (top {top_k})\n")
    print(f"{'session_key':<22} {'date':<12} {'type':<15} {'tasks':>5} {'dist':>7}  summary")
    print("-" * 100)
    for r in results:
        date = (r["started_at"] or "")[:10]
        stype = (r["session_type"] or "")[:14]
        summary = (r["summary"] or "")[:55]
        tasks = r["tasks_completed"] or 0
        dist = f"{r['distance']:.4f}"
        print(f"{r['session_key']:<22} {date:<12} {stype:<15} {tasks:>5} {dist:>7}  {summary}")
    print()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?", default="", help="Search query")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.query:
        parser.print_help()
        sys.exit(1)

    search(args.query, top_k=args.top, as_json=args.json)


if __name__ == "__main__":
    re_exec_in_venv()
    main()
