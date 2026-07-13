#!/usr/bin/env python3
"""
resolve_session_type.py
Task 37/38 — Session Type System Backend

Resolves the session type from priority sources and assembles type-specific
context (prompt injection + preloaded context files) for wake.sh to use.

Priority order:
  1. SESSION_TYPE env var (explicit override)
  2. config/session_schedule.json one_off match (datetime ± SLOT_TOLERANCE)
  3. config/session_schedule.json recurring match (slot ± SLOT_TOLERANCE)
  4. default: "execution"

Usage:
  python3 scripts/resolve_session_type.py \\
    --project-dir /path/to/agent_project \\
    --trigger-mode nightly \\
    --output /tmp/session_type_result.json

Output JSON:
  session_type:        resolved type id
  resolution_source:   env_var | one_off | recurring | default
  prompt_content:      contents of the type's prompt_file (or "")
  assembled_context:   concatenated context_files (or "")
  focus_hint:          type's focus_hint text (or "")
  behavioral_overrides: dict from type YAML
  memory_discipline:   strict | normal (from behavioral_overrides)
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SLOT_TOLERANCE_MINUTES = 10


def parse_args():
    p = argparse.ArgumentParser(description="Resolve session type for wake.sh")
    p.add_argument("--project-dir", required=True, help="Agent project root directory")
    p.add_argument("--trigger-mode", default="nightly",
                   choices=["nightly", "emergency", "manual"],
                   help="Current trigger mode")
    p.add_argument("--output", required=True, help="Output JSON file path")
    return p.parse_args()


def hm_diff_minutes(h1: int, m1: int, h2: int, m2: int) -> int:
    """Absolute minute difference between two HH:MM times (handles midnight wrap)."""
    total1 = h1 * 60 + m1
    total2 = h2 * 60 + m2
    diff = abs(total1 - total2)
    return min(diff, 1440 - diff)


def resolve_type(project_dir: Path, trigger_mode: str) -> tuple:
    """Returns (session_type: str, resolution_source: str)."""

    # Priority 1: SESSION_TYPE env var
    env_type = os.environ.get("SESSION_TYPE", "").strip()
    if env_type:
        return env_type, "env_var"

    schedule_file = project_dir / "config" / "session_schedule.json"
    if schedule_file.exists():
        try:
            data = json.loads(schedule_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}

        now = datetime.now()

        # Priority 2: one_off entries
        changed = False
        for entry in data.get("one_off", []):
            if entry.get("fired", True):
                continue
            if entry.get("trigger") != trigger_mode:
                continue
            dt_str = entry.get("datetime", "")
            if not dt_str:
                continue
            try:
                dt = datetime.fromisoformat(dt_str)
                # Compare without tzinfo — compare wall-clock local time
                dt_naive = dt.replace(tzinfo=None)
                diff_min = abs((now - dt_naive).total_seconds() / 60)
                if diff_min <= SLOT_TOLERANCE_MINUTES:
                    entry["fired"] = True
                    changed = True
                    result_type = entry["session_type"]
                    # Atomic write-back for fired flag
                    if changed:
                        tmp = schedule_file.with_suffix(".json.tmp")
                        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                        tmp.replace(schedule_file)
                    return result_type, "one_off"
            except (ValueError, TypeError):
                continue

        # Priority 3: recurring entries
        for entry in data.get("recurring", []):
            if not entry.get("enabled", True):
                continue
            if entry.get("trigger") != trigger_mode:
                continue
            slot = entry.get("slot", "")
            try:
                slot_h, slot_m = map(int, slot.split(":"))
            except (ValueError, AttributeError):
                continue
            diff = hm_diff_minutes(now.hour, now.minute, slot_h, slot_m)
            if diff <= SLOT_TOLERANCE_MINUTES:
                return entry["session_type"], "recurring"

    # Priority 4: default
    return "execution", "default"


def load_yaml_simple(path: Path) -> dict:
    """
    Minimal YAML loader for the simple key-value + list structure used in
    session type configs. Handles: str values, block scalars (>), lists (- items),
    and nested dicts (2-space indent). Does not handle anchors, multi-doc, etc.
    Falls back to {} on parse error.
    """
    # Try real YAML first if available
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        pass
    except Exception:
        return {}

    # Fallback: hand-rolled minimal parser
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        result = {}
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.rstrip()
            if not stripped or stripped.lstrip().startswith("#"):
                i += 1
                continue
            if ":" in stripped and not stripped.startswith(" "):
                key, _, rest = stripped.partition(":")
                key = key.strip()
                rest = rest.strip()
                if rest in (">", "|", ""):
                    # Block scalar or dict — collect indented lines
                    j = i + 1
                    sub_lines = []
                    while j < len(lines):
                        sub = lines[j]
                        if not sub.strip() and sub_lines:
                            sub_lines.append("")
                            j += 1
                            continue
                        if sub and not sub[0].isspace():
                            break
                        sub_lines.append(sub.strip())
                        j += 1
                    # Check if sub_lines are list items
                    if sub_lines and sub_lines[0].startswith("- "):
                        result[key] = [s[2:].strip() for s in sub_lines if s.startswith("- ")]
                    elif sub_lines and sub_lines[0].startswith("#"):
                        result[key] = {}
                    elif sub_lines:
                        result[key] = " ".join(s for s in sub_lines if s)
                    i = j
                elif rest.startswith("["):
                    # Inline list — skip (uncommon in our YAMLs)
                    result[key] = []
                    i += 1
                else:
                    result[key] = rest.strip('"').strip("'")
                    i += 1
            else:
                i += 1
        return result
    except Exception:
        return {}


def load_type_config(project_dir: Path, session_type: str) -> dict:
    """Load and return the session type YAML config dict."""
    type_file = project_dir / "config" / "session_types" / f"{session_type}.yaml"
    if not type_file.exists():
        return {}
    return load_yaml_simple(type_file)


def assemble_context(project_dir: Path, context_files: list) -> str:
    """
    Read context files and concatenate them with headers.
    Files that don't exist are silently skipped.
    Returns empty string if no files found.
    """
    parts = []
    for rel_path in context_files:
        if not isinstance(rel_path, str):
            continue
        abs_path = project_dir / rel_path
        if not abs_path.exists():
            continue
        try:
            content = abs_path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"### {rel_path}\n\n{content}")
        except OSError:
            continue

    if not parts:
        return ""

    return "\n\n---\n\n".join(parts)


def load_prompt_content(project_dir: Path, prompt_file: str) -> str:
    """Load the type-specific prompt file contents."""
    if not prompt_file:
        return ""
    prompt_path = project_dir / prompt_file
    if not prompt_path.exists():
        return ""
    try:
        return prompt_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def main():
    args = parse_args()
    project_dir = Path(args.project_dir).resolve()

    session_type, resolution_source = resolve_type(project_dir, args.trigger_mode)
    config = load_type_config(project_dir, session_type)

    context_files = config.get("context_files") or []
    if not isinstance(context_files, list):
        context_files = []

    assembled_context = assemble_context(project_dir, context_files)

    prompt_file = config.get("prompt_file") or ""
    if not isinstance(prompt_file, str):
        prompt_file = ""
    prompt_content = load_prompt_content(project_dir, prompt_file)

    behavioral_overrides = config.get("behavioral_overrides") or {}
    if not isinstance(behavioral_overrides, dict):
        behavioral_overrides = {}

    result = {
        "session_type": session_type,
        "resolution_source": resolution_source,
        "prompt_content": prompt_content,
        "assembled_context": assembled_context,
        "focus_hint": (config.get("focus_hint") or "").strip(),
        "behavioral_overrides": behavioral_overrides,
        "memory_discipline": behavioral_overrides.get("memory_discipline", "normal"),
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    # Print summary to stderr for wake.log capture
    print(
        f"session_type={session_type} source={resolution_source} "
        f"prompt={'yes' if prompt_content else 'no'} "
        f"context_files={len(context_files)} "
        f"memory_discipline={result['memory_discipline']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
