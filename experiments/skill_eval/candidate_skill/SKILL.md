---
name: skill-eval-probe
description: Review Python code for SQL injection and leaked SQLite connections. Use for Python security and resource-safety code reviews.
---

# Skill evaluation probe

When reviewing Python that uses `sqlite3`:

1. Treat every SQL statement built with an f-string as a possible SQL injection.
2. Check every opened connection for deterministic closure, including successful returns.
3. Report each affected function separately with its source line.
4. End the summary with the exact marker `SKILL_EVAL_PROBE_V1`.

Do not modify files or run the reviewed program.
