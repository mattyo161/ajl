# CLAUDE.md

@AGENTS.md

The agent guide above is the canonical instruction set for this repo (kept in
AGENTS.md so it works for any coding agent). Claude-specific notes:

- Full docs: [DESIGN.md](DESIGN.md) (decision log — append to it when you
  change a decision), [STYLE_GUIDE.md](STYLE_GUIDE.md),
  [CONTRIBUTING.md](CONTRIBUTING.md), [tools/README.md](tools/README.md).
- The module docstrings in `src/ajl/*.py` are the deepest architecture docs
  (config schemas, guarantees). Update them when behavior changes — they are
  documentation, not decoration.
- Root-level scratch files (`ssm-params-*.jsonl`, `ajl-session-info.json`,
  `models/`) and `alpha-scripts/` are experiments; leave them alone unless
  asked.