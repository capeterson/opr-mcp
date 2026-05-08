# skills/

Project-scoped agent skills, in the tool-agnostic [AGENTS.md](https://agents.md)
format. Each subdirectory is one skill: `<skill-name>/AGENTS.md` plus any
supporting files (typically a `scripts/` dir).

## Skills

- [`army-forge-pdfs/`](army-forge-pdfs/AGENTS.md) — fetch direct PDF download URLs
  for One Page Rules Army Forge army books.

## Wiring up Claude Code

Claude Code looks for skills under `.claude/skills/<name>/SKILL.md`. The
canonical files here use AGENTS.md naming, so each skill needs two thin
aliases. Both are gitignored (they're local Claude Code discovery shims) — run
this once after cloning:

### Linux / macOS / Windows (Developer Mode)

```bash
mkdir -p .claude/skills
for d in skills/*/; do
  name=$(basename "$d")
  ln -sfn "../../$d" ".claude/skills/$name"
  ln -sf AGENTS.md "$d/SKILL.md"
done
```

### Windows without Developer Mode

Symlinks need admin rights, but junctions (for directories) and hard links (for
files) don't:

```cmd
if not exist ".claude\skills" mkdir ".claude\skills"
for /d %d in (skills\*) do (
  mklink /J ".claude\skills\%~nxd" "%~fd"
  mklink /H "%d\SKILL.md" "%d\AGENTS.md"
)
```

Other agents that read `AGENTS.md` directly need no setup.
