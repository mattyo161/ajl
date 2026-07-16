<!-- ai_guide_version: ae817d364380 -->
<!-- vendored from mattyo161/AI_GUIDE — edit there, then re-run bin/adopt -->
# House rules (AI_GUIDE)

Cross-repo conventions for humans and AI agents. Repo rules (`AGENTS.md`)
override these on conflict. Canonical source: `mattyo161/AI_GUIDE`.

## Routing new rules

Would the rule be wrong in any other repo? **No** → AI_GUIDE's GUIDE.md.
**Yes** → this repo's AGENTS.md. **Machine-only** → CLAUDE.local.md.

## Git workflow

- Conventional commits: `feat:` `fix:` `docs:` `test:` `refactor:` `chore:`,
  imperative subject, body explains *why* when it isn't obvious.
- Run the repo's tests and lint before committing; state results plainly.
- Never commit directly to a shared branch others are mid-review on; never
  rewrite pushed history without `--force-with-lease` and a reason.

## Documentation standard (every repo converges on this)

- `AGENTS.md` — canonical agent instructions: commands first (real, runnable),
  map, style, boundaries. Target < 150 lines.
- `CLAUDE.md` — a shim: `@AGENTS.md` + `@docs/AI_GUIDE.md` + Claude-only notes.
- `DESIGN.md` — append-only decision log (ADR-lite): what was decided, why,
  what was rejected. Strike through superseded entries; never delete.
- Deep dives live in `docs/`; module docstrings document design, not syntax.
- Write for two audiences at once: human narrative first, terse directives
  after. Documentation is teaching; the code is the memory.

## Code & output discipline

- For CLIs: stdout is data only (JSONL where applicable); everything
  human-facing goes to stderr, prefixed with the tool name.
- Errors are contained: per-item failures report to stderr and keep the
  stream alive; exit non-zero at the end.
- Match the surrounding code's idiom, comment density, and naming. Comments
  state constraints code can't express — never narrate the next line.

## Shell style

Break multi-command chains across lines — one command per line, trailing `\`,
leading `&&`:

```shell
cd /some/path \
  && git fetch origin -q \
  && git status --short
```

## Boundaries

**Always:** run tests before commit; check what a delete/overwrite targets
before doing it; record contract-changing decisions in DESIGN.md.

**Ask first:** pushing to a remote you haven't pushed to before in the
session; publishing anything (PyPI, releases, external services); destructive
or hard-to-reverse operations; scope changes beyond the request.

**Never:** commit secrets, tokens, or account identifiers into public repos;
force-push over unreviewed work; silently drop a failing case to make a run
look green; leave scratch files untracked in the worktree without mentioning
them.
