## What

## Why

## Checklist
- [ ] No new config key without a reader and a line in `.env.example` (R1)
- [ ] New/changed gate declares `on_missing` (R5)
- [ ] Strategy change → `docs/strategies/<KEY>.md` updated, artefact row filled, sample size stated (R13, R14)
- [ ] Anything that can place an order is behind `exec/gateway.py` and respects mode + halt (R9)
- [ ] Tests: unit for pure code; fixture-replay for venue code
