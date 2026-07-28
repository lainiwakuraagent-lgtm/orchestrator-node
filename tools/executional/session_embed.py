#!/usr/bin/env python3
"""
session_embed.py — Generate vector embeddings for analytics.db sessions.

Uses Ollama (nomic-embed-text, 768 dims) to embed session summaries.
Stores results in sessions.embedding (BLOB) and session_embeddings vec0 table.

Gated on Ollama availability — exits 0 (non-fatal) if unreachable.

Usage:
  python3 tools/session_embed.py [--limit N] [--all]

  --limit N    Embed at most N sessions (default: 50 per run)
  --all        Embed all sessions without limit
  --status     Show embedding coverage stats, then exit
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
    """Re-exec this script with the sqlite-vec venv python if not already using it."""
    if sys.executable != str(VENV_PYTHON):
        if not VENV_PYTHON.exists():
            print("session_embed: venv not found at", VENV_PYTHON, file=sys.stderr)
            print("session_embed: run: python3 -m venv", VENV_PYTHON.parent, file=sys.stderr)
            sys.exit(1)
        result = subprocess.run([str(VENV_PYTHON), __file__] + sys.argv[1:])
        sys.exit(result.returncode)


def check_ollama():
    """Return True if Ollama is reachable and has the embedding model."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        models = [m["name"].split(":")[0] for m in data.get("models", [])]
        if EMBED_MODEL not in models:
            print(f"session_embed: Ollama reachable but '{EMBED_MODEL}' not installed.", file=sys.stderr)
            print(f"  Run: ollama pull {EMBED_MODEL}", file=sys.stderr)
            return False
        return True
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        print(f"session_embed: Ollama not reachable at {OLLAMA_URL} — skipping.", file=sys.stderr)
        return False


def get_embedding(text):
    """Call Ollama to get a 768-dim embedding for text. Returns list[float] or None."""
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
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def embedding_to_blob(vec):
    """Pack a list of floats into a little-endian BLOB for sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


def load_vec_extension(conn):
    """Load the sqlite-vec extension into a connection."""
    import sqlite_vec  # only available in venv
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def ensure_vec_table(conn):
    """Create the session_embeddings virtual table if it doesn't exist."""
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS session_embeddings
        USING vec0(session_id INTEGER, embedding FLOAT[{EMBED_DIMS}])
    """)
    conn.commit()


def build_session_text(row):
    """Build a searchable text representation of a session row."""
    parts = []
    if row["session_type"]:
        parts.append(row["session_type"])
    if row["trigger_mode"]:
        parts.append(row["trigger_mode"])
    if row["summary"]:
        parts.append(row["summary"])
    if row["handoff"]:
        parts.append(row["handoff"][:200])
    return " | ".join(parts) if parts else row["session_key"]


def run_embed(limit=50, embed_all=False):
    import sqlite_vec  # ensure we're in venv

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    load_vec_extension(conn)
    ensure_vec_table(conn)

    query = "SELECT id, session_key, session_type, trigger_mode, summary, handoff FROM sessions WHERE embedding IS NULL"
    if not embed_all:
        query += f" LIMIT {limit}"

    rows = conn.execute(query).fetchall()
    if not rows:
        print("session_embed: all sessions already have embeddings (╥﹏╥ nothing to do)")
        conn.close()
        return

    print(f"session_embed: embedding {len(rows)} sessions...")
    done = skipped = 0
    for row in rows:
        text = build_session_text(row)
        vec = get_embedding(text)
        if vec is None:
            skipped += 1
            continue
        if len(vec) != EMBED_DIMS:
            print(f"  warning: session {row['session_key']} got {len(vec)} dims (expected {EMBED_DIMS}) — skipped")
            skipped += 1
            continue

        blob = embedding_to_blob(vec)
        conn.execute("UPDATE sessions SET embedding=? WHERE id=?", (blob, row["id"]))

        # Upsert into virtual table (vec0 doesn't support ON CONFLICT; delete+insert)
        conn.execute("DELETE FROM session_embeddings WHERE session_id=?", (row["id"],))
        conn.execute("INSERT INTO session_embeddings(session_id, embedding) VALUES (?,?)",
                     (row["id"], blob))
        done += 1

    conn.commit()
    conn.close()
    print(f"session_embed: done — embedded={done} skipped={skipped}")


def show_status():
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    try:
        embedded = conn.execute("SELECT COUNT(*) FROM sessions WHERE embedding IS NOT NULL").fetchone()[0]
    except Exception:
        embedded = 0
    conn.close()
    pct = (embedded / total * 100) if total else 0
    print(f"session_embed status: {embedded}/{total} sessions embedded ({pct:.1f}%)")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if not check_ollama():
        sys.exit(0)

    run_embed(limit=args.limit, embed_all=args.all)


if __name__ == "__main__":
    re_exec_in_venv()
    main()
