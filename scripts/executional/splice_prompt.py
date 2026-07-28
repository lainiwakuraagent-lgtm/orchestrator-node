#!/usr/bin/env python3
"""
splice_prompt.py <wrapper_template> <goal_file> <output_file> [persona_file]

Reads the wrapper prompt template and replaces {{GOAL_PLACEHOLDER}},
{{PERSONA_PLACEHOLDER}}, and {{SESSION_TYPE}} with the literal contents of
the given files (goal/persona) and the CURRENT_SESSION_TYPE env var
(session type) respectively.
All file reads are done by paths passed as argv -- no shell interpolation
of file contents into source code -- so arbitrary text (quotes, backslashes,
braces, anything) in the goal/persona files is handled safely.
"""
import os
import sys


def main():
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <template> <goal_file> <output_file> [persona_file]",
              file=sys.stderr)
        sys.exit(1)

    template_path = sys.argv[1]
    goal_path = sys.argv[2]
    output_path = sys.argv[3]
    persona_path = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None

    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    with open(goal_path, "r", encoding="utf-8") as f:
        goal = f.read().strip()

    if persona_path:
        with open(persona_path, "r", encoding="utf-8") as f:
            persona = f.read().strip()
    else:
        persona = "(no persona defined -- proceed as a neutral capable agent)"

    session_type = os.environ.get("CURRENT_SESSION_TYPE", "unknown")

    if "{{GOAL_PLACEHOLDER}}" not in template:
        print("WARNING: {{GOAL_PLACEHOLDER}} not found in template", file=sys.stderr)
    if "{{PERSONA_PLACEHOLDER}}" not in template:
        print("WARNING: {{PERSONA_PLACEHOLDER}} not found in template", file=sys.stderr)
    if "{{SESSION_TYPE}}" not in template:
        print("WARNING: {{SESSION_TYPE}} not found in template", file=sys.stderr)

    composed = template.replace("{{GOAL_PLACEHOLDER}}", goal)
    composed = composed.replace("{{PERSONA_PLACEHOLDER}}", persona)
    composed = composed.replace("{{SESSION_TYPE}}", session_type)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(composed)


if __name__ == "__main__":
    main()
