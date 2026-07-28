#!/usr/bin/env python3
"""
tag_skill_lookup.py — Resolve a task's SOP tag to a skill file path.

Convention: tag "bugfix" → skill directory "sop-bugfix" → file "skills/sop-bugfix/SKILL.md"

This is a deterministic lookup — exact string match, no fuzzy search,
no agent discretion. If the skill file doesn't exist, fail immediately.

Usage:
    python3 scripts/tag_skill_lookup.py --project-dir /path/to/agent_project --tag bugfix
    python3 scripts/tag_skill_lookup.py --project-dir /path/to/agent_project --list
    python3 scripts/tag_skill_lookup.py --project-dir /path/to/agent_project --validate-all
"""

import argparse
import sys
from pathlib import Path


SKILLS_DIR_NAME = "skills"


def resolve_skill_path(project_dir: Path, tag: str) -> Path | None:
    """Resolve a tag to its skill file path. Returns None if not found."""
    skill_dir = project_dir / SKILLS_DIR_NAME / f"sop-{tag}"
    skill_file = skill_dir / "SKILL.md"
    if skill_file.exists():
        return skill_file
    return None


def list_available_skills(project_dir: Path) -> list[tuple[str, Path]]:
    """List all available sop-* skills as (tag, path) tuples."""
    skills_dir = project_dir / SKILLS_DIR_NAME
    if not skills_dir.exists():
        return []
    results = []
    for d in sorted(skills_dir.iterdir()):
        if d.is_dir() and d.name.startswith("sop-"):
            skill_file = d / "SKILL.md"
            if skill_file.exists():
                tag = d.name[4:]  # strip "sop-" prefix
                results.append((tag, skill_file))
    return results


def validate_tag(project_dir: Path, tag: str) -> bool:
    """Check if a tag has a corresponding skill file. For plan-time validation."""
    return resolve_skill_path(project_dir, tag) is not None


def main():
    parser = argparse.ArgumentParser(description="Tag → skill lookup")
    parser.add_argument("--project-dir", required=True, help="Agent project root")
    parser.add_argument("--tag", default=None, help="Tag to resolve")
    parser.add_argument("--list", action="store_true", help="List all available skills")
    parser.add_argument("--validate-all", action="store_true",
                        help="Check all known SOP tags have skill files")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()

    if args.list:
        skills = list_available_skills(project_dir)
        if not skills:
            print("No sop-* skills found in skills/ directory.")
            sys.exit(0)
        for tag, path in skills:
            print(f"  {tag} -> {path}")
        return

    if args.validate_all:
        known_tags = ["bugfix", "feature", "refactor", "chore", "docs",
                       "repo-work", "fleet", "security", "infra"]
        missing = []
        for tag in known_tags:
            if not validate_tag(project_dir, tag):
                missing.append(tag)
        if missing:
            print(f"MISSING skill files for tags: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)
        print(f"All {len(known_tags)} SOP tags validated.")
        return

    if not args.tag:
        print("ERROR: --tag required (or use --list / --validate-all)", file=sys.stderr)
        sys.exit(1)

    path = resolve_skill_path(project_dir, args.tag)
    if path is None:
        print(f"ERROR: No skill file for tag '{args.tag}' "
              f"(expected: {project_dir / SKILLS_DIR_NAME / f'sop-{args.tag}' / 'SKILL.md'})",
              file=sys.stderr)
        sys.exit(1)

    print(str(path))


if __name__ == "__main__":
    main()
