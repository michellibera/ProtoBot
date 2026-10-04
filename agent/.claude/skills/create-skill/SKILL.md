---
name: create-skill
description: Create or update a reusable skill when a task was done for the second time, the user demonstrates or explains a process, or a routine procedure emerged. Use whenever something repeatable should be saved.
---

# Creating a skill

1. Pick a short kebab-case name (e.g. `weekly-report`, `olx-listing`).
2. Create `.claude/skills/<name>/SKILL.md` with frontmatter:
   ```
   ---
   name: <name>
   description: <one sentence: what it does AND when to trigger it>
   ---
   ```
3. Body: numbered steps, exact commands/URLs/selectors that worked, pitfalls learned, how to verify success,
   and what needs user approval. Keep it under ~100 lines; put bulky reference in `references/*.md` next to it.
4. Add one line to `memory/MEMORY.md` under "Skille".
5. Tell the user in one sentence that a skill was saved and what it does.
6. When a skill turns out wrong or outdated, fix it right away and note the lesson in `memory/log/learnings.md`.

Do not write skills for one-off things. A skill should save real effort next time.
