# CLAUDE.md

@AGENTS.md
@docs/AI_GUIDE.md

The agent guide above is the canonical instruction set for this repo (kept in
AGENTS.md so it works for any coding agent); the vendored AI_GUIDE holds the
cross-repo house rules. Claude-specific notes:

- Full docs: [DESIGN.md](DESIGN.md) (decision log — append to it when you
  change a decision), [STYLE_GUIDE.md](STYLE_GUIDE.md),
  [CONTRIBUTING.md](CONTRIBUTING.md), [tools/README.md](tools/README.md),
  [docs/request-flow.md](docs/request-flow.md) (the CLI-argv-to-JSONL
  decision path), [docs/environment.md](docs/environment.md) (every env
  var), [docs/data-files.md](docs/data-files.md) (every file ajl reads or
  writes), [docs/commands/](docs/commands/) (one reference per custom
  command: `ssm get/update/put/params`, `s3 scan/list`, `cache`, `decrypt`).
- The module docstrings in `src/ajl/*.py` are the deepest architecture docs
  (config schemas, guarantees). Update them when behavior changes — they are
  documentation, not decoration.
- Root-level scratch files (`ssm-params-*.jsonl`, `ajl-session-info.json`,
  `models/`) and `alpha-scripts/` are experiments; leave them alone unless
  asked.
