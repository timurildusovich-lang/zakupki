from __future__ import annotations

import json
from pathlib import Path

from .common import iso, read_json
from .store import Store


def render(root: Path):
    settings = read_json(root / "config" / "settings.json")
    catalog = read_json(root / "data" / "catalog.json") if (root / "data" / "catalog.json").exists() else {"suppliers": [], "brands": [], "warnings": []}
    payload = {"generated_at": iso(), "states": {name: Store(root, name).read() for name in ["test", "mail_test", "live"]},
               "catalog": catalog, "settings": {"mode": settings["mode"], "sender": settings["mail"]["sender"],
               "calendar": settings["calendar"], "signature": bool(settings["mail"]["signature_ru"] or settings["mail"]["signature_en"]),
               "external_enabled": settings["external_side_effects_enabled"], "mail_test_recipient": settings.get("mail_test", {}).get("recipient"),
               "mail_test_sender": settings.get("mail_test", {}).get("sender", settings["mail"]["sender"]),
               "live_connection": read_json(root / "data/live/connection.json") if (root / "data/live/connection.json").exists() else None,
               "mail_test_connection": read_json(root / "data/mail_test/connection.json") if (root / "data/mail_test/connection.json").exists() else None}}
    encoded = json.dumps(payload, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = (root / "web" / "history.html").read_text(encoding="utf-8")
    target = root / "index.html"
    target.write_text(template.replace("__HISTORY_DATA__", encoded), encoding="utf-8")
    return str(target)
