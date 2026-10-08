# ict-silver-bullet

Production runs on ONE AWS EC2 Windows Server instance (m7i-flex.large, 2 vCPU / 8 GiB).
The same instance runs the MetaTrader 5 terminal and independent Python processes.
Each of `brain/`, `executor/`, and `backtest/` has its own virtual environment.
Brain handles analysis, scoring, LLM review, risk checks, and the dashboard.
Brain writes proposed trades to Supabase.
Brain never places real orders and never imports MetaTrader5.
Executor is the only code that communicates with the local MT5 terminal.
Executor feeds candles to Supabase and places, manages, and closes real orders.
Executor also reports equity and runs the watchdog.
Supabase provides the shared database boundary between processes.
Backtest is reserved for history pull, replay, and Monte Carlo analysis.
Brain and backtest remain OS-agnostic; Windows-specific code belongs in executor or deployment scripts.
There is no Hugging Face hosting, Linux host, or second machine.
This scaffold contains interface contracts and entry-point stubs, with no trading logic.

## Setup

Use Python 3.10 or newer. On the EC2 instance, create `brain/.env` and
`executor/.env` by hand from `.env.example`, keeping only relevant process settings.
Credentials are never generated or filled in by an automated agent.
Process environment values override the corresponding package's `.env` values.
`load_config()` validates settings on demand and caches the result; imports need no credentials.
Brain news API keys are optional: a client without a key is disabled.
`load_dashboard_config()` separately validates dashboard credentials.

Create separate virtual environments and install each process's own `requirements.txt`.
Brain's optional CPU sentiment dependencies are in `brain/requirements-ml.txt`.
Never install ML dependencies or executor requirements in test sandboxes.
Install `requirements-dev.txt` and `python-dotenv` for local scaffold checks.
From this directory, run `python -m pytest` and
`python -c "import brain.config, executor.config, brain.db.supabase_client"`.
Future process entry points are listed in `AGENTS.md`; they currently raise `NotImplementedError`.
Deployment scripts and instructions will be added in a later phase.

## Deployment

Deploy on the always-on EC2 Windows instance using
[the Windows deployment guide](deploy/windows/README-deploy.md). It documents
the HTTPS clone with a read-only token, manual `.env` creation, bootstrap,
interactive logon tasks, reboot test, safe updates and troubleshooting.

Run `deploy/windows/bootstrap.ps1`, then `deploy/windows/install_tasks.ps1`
with the MT5 executable path. Auto-logon must be configured; disconnect RDP
instead of signing out. Restrict dashboard TCP port 7860 to your IP.
The dashboard runs separately with `python -m brain.dashboard`, requires
`DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`, and refreshes every 10 seconds.
It serves the Vite/React frontend and authenticated FastAPI endpoints on the
same port. Node.js 24 LTS is required to build the frontend during bootstrap or
updates; production does not run a Node service. See
[frontend development and checks](frontend/README.md) and the deployment guide's
**First upgrade from Gradio to Vite** steps for existing installations.
Use `status.ps1` for task/log status and `update.ps1` for guarded main updates.
See the guide for schema-dependent limits on floating P/L and setup evidence.
