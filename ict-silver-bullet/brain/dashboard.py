"""Serve the Vite dashboard and API with python -m brain.dashboard."""

from brain.config import load_dashboard_config
from brain.logging_setup import setup_logging


def main() -> None:
    logger = setup_logging("brain")
    config = load_dashboard_config()  # Never bind without validated credentials.
    from brain.dashboard_api import create_dashboard, runtime_options
    import uvicorn

    app = create_dashboard(config)
    _, proxy = runtime_options()
    # If later using Tailscale (free personal plan), set DASHBOARD_HOST to
    # 127.0.0.1 or the tailnet IP instead of exposing port 7860.
    try:
        uvicorn.run(app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT,
                    workers=1, proxy_headers=bool(proxy), forwarded_allow_ips=proxy, access_log=False)
    except KeyboardInterrupt:
        logger.info("Dashboard stopped")


if __name__ == "__main__":
    main()
