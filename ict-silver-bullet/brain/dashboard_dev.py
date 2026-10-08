"""Local API only; run Vite separately. Uses the same real authentication rules."""
from brain.config import load_dashboard_config
from brain.dashboard_api import create_dashboard
from brain.logging_setup import setup_logging


def main():
    import uvicorn
    setup_logging("brain")
    config = load_dashboard_config()
    uvicorn.run(create_dashboard(config, development=True), host="127.0.0.1", port=config.DASHBOARD_PORT,
                workers=1, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
