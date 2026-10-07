# AGENTS.md — rules for every coding agent in this repo

## Project
ICT Silver Bullet automated forex system. Production = ONE AWS EC2 Windows
Server instance (m7i-flex.large, 2 vCPU / 8 GiB) running the MetaTrader 5
terminal plus separate Python processes (separate venvs for brain/ and
executor/). No Hugging Face, no other hosts.

## Git workflow
- Work directly on `main`. No branches, PRs, worktrees or tags.
- Other agents work AT THE SAME TIME in their own clones on DIFFERENT files.
  Only create/modify the files listed under "Files you own" in your prompt
  (and their tests). Never edit another phase's files. Never edit
  requirements*.txt, .env.example, config.py, logging_setup.py, pytest.ini or
  this file — if you need a change there, say so in your final report.
- Publish: `git add <your files only>` -> `git commit -m "phase-N: <summary>"`
  -> `git pull --rebase origin main` -> `git push origin main`. If the push is
  rejected, pull --rebase and push again. Never force-push. Never push a state
  where your own tests fail.

## Platform rules
- Production is Windows, but `brain/` and `backtest/` code must be
  OS-agnostic (pathlib, no Windows-only calls) so tests run in a Linux
  sandbox. Windows-specific code lives only in `executor/` and
  `deploy/windows/`.
- `brain/` must NEVER import MetaTrader5. Never install or import
  MetaTrader5 in your own environment; executor modules may import it at the
  top and simply cannot be run here — write them against the documented API.
- Never install torch/transformers in your sandbox. Import them lazily inside
  functions so tests run without them.
- Use `zoneinfo` + the `tzdata` package for all timezone work. All datetimes
  are timezone-aware; store UTC. Use `logging` (via setup_logging), never print.

## Secrets & network
- Never create a `.env`, never ask for or invent real keys. All tests must
  mock every network / database / LLM / MT5 call.

## Process entry points (deploy scripts depend on these exact names)
  python -m brain.app                 # pipeline loop
  python -m brain.dashboard           # Gradio dashboard
  python -m executor.mt5_bridge       # MT5 -> Supabase candle feeder
  python -m executor.order_executor   # polls signals, places/manages orders
  python -m executor.trade_reporter   # equity snapshot loop
  python -m executor.watchdog         # alerts
Each module has a `main()` that runs forever, handles KeyboardInterrupt
cleanly, and survives per-iteration exceptions (log, back off, continue).

## Interface contracts
brain/db/supabase_client.py, executor/db_client.py and executor/mt5_bridge.py
began as stubs with fixed signatures. Code against those signatures. Do not
change them; if you think one must change, report it instead.

## Definition of done
Your phase's own tests pass (run only your own test directories unless told
otherwise). Your final report lists: files created, test results, every
`# NOTE:` judgment call, and anything another phase must know.
