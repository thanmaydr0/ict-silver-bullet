"""Offline contract export: no configuration, credentials, or database calls."""
import json
from pathlib import Path
from brain.dashboard_models import DashboardSnapshot

if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "frontend" / "src" / "dashboard.schema.json"
    target.write_text(json.dumps(DashboardSnapshot.model_json_schema(mode="serialization"), indent=2) + "\n", encoding="utf-8")
