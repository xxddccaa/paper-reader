import json
from .config import get_settings

def skill_catalog() -> list[dict]:
    path = get_settings().project_dir / "skills" / "registry.json"
    if not path.is_file():
        return []
    raw = json.loads(path.read_text())
    records = raw.get("skills", []) if isinstance(raw, dict) else raw
    result = []
    for record in records:
        entry = {key: record[key] for key in ("id", "name", "description")}
        entry["version"] = str(record.get("version", record.get("commit", "unknown")))
        entry["available"] = bool(record.get("available", True))
        if record.get("unavailable_reason"):
            entry["unavailable_reason"] = record["unavailable_reason"]
        result.append(entry)
    return result
