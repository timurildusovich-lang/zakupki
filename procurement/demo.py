from __future__ import annotations

from .common import WorkflowError, read_json
from .engine import Engine
from .store import empty_state


def run_demo(root):
    engine = Engine(root, "test")
    if engine.store.read()["requests"]:
        return {"status": "already_exists", "note": "Тестовая история сохранена. Демонстрация не перезаписывает запросы пользователя."}
    checks = [{"supplier_id": s["id"], "brand": b, "complete": True, "checked_at": "2026-10-01T09:00:00+03:00", "sources": ["fixture_mailbox", "fixture_drive"]}
              for s in engine.catalog["suppliers"] for b in s["brands"]]
    engine.inventory_import({"checks": checks, "prices": [{"id": "fixture_fresh_davines", "supplier_id": "test_eu", "brands": ["Davines"], "kinds": ["stock"],
        "received_at": "2026-09-29T10:00:00+03:00", "source": "fixture", "url": "fixtures/prices/Davines stock.csv"}]}, now="2026-10-01T09:00:00+03:00")
    request = engine.prepare({"brands": ["Davines", "Tigi"], "kinds": ["stock", "preorder"], "supplier_ids": ["test_eu", "test_ru"],
        "source_text": "ТЕСТ: запросить сток и предзаказ Davines и Tigi"}, actor="Тестовый постановщик", now="2026-10-01T10:00:00+03:00")
    request = engine.approve(request["id"], request["preview_digest"], "Симулятор подтверждения", now="2026-10-01T10:01:00+03:00")
    for delivery in request["deliveries"]:
        engine.dispatch(request["id"], delivery["id"], now="2026-10-01T10:02:00+03:00")
    eu, ru = request["deliveries"]
    engine.ingest({"id": "fixture_reply_price", "from": "prices@example.invalid", "delivery_id": eu["id"], "subject": "Current price lists",
        "body": "Hello! Please find our current pre-order and stock lists attached.", "received_at": "2026-10-01T11:15:00+03:00", "links": [],
        "attachments": [{"name": "Mixed price.csv", "path": "fixtures/prices/Mixed price.csv"}]}, now="2026-10-01T11:15:00+03:00")
    engine.classify("fixture_reply_price", {"kind": "price", "coverage": [{"brand": i["brand"], "kind": i["kind"]} for i in eu["items"]],
        "evidence": {"attachment_names": ["Mixed price.csv"]}, "actor": "Тестовый классификатор"}, now="2026-10-01T11:16:00+03:00")
    engine.ingest({"id": "fixture_reply_question", "from": "supplier@example.invalid", "delivery_id": ru["id"], "subject": "Объём заказа",
        "body": "Добрый день! Уточните, какой объём заказа вас интересует и есть ли целевые цены?", "received_at": "2026-10-01T12:00:00+03:00", "attachments": [], "links": []}, now="2026-10-01T12:00:00+03:00")
    engine.classify("fixture_reply_question", {"kind": "question", "actor": "Тестовый классификатор"}, now="2026-10-01T12:01:00+03:00")
    # Independent older request demonstrates the entire six-message sequence.
    older_checks = [{"supplier_id": "test_silent", "brand": "Alterna", "complete": True, "checked_at": "2026-09-17T09:00:00+03:00", "sources": ["fixture_mailbox", "fixture_drive"]}]
    engine.inventory_import({"checks": older_checks}, now="2026-09-17T09:00:00+03:00")
    silent = engine.prepare({"brands": ["Alterna"], "kinds": ["stock"], "supplier_ids": ["test_silent"], "source_text": "ТЕСТ: запросить сток Alterna"},
        "Тестовый постановщик", now="2026-09-17T15:00:00+03:00")
    engine.approve(silent["id"], silent["preview_digest"], "Симулятор подтверждения", now="2026-09-17T15:00:00+03:00")
    engine.dispatch(silent["id"], silent["deliveries"][0]["id"], now="2026-09-17T15:00:00+03:00")
    for day in ["18", "21", "22", "23", "24", "25"]:
        engine.tick(f"2026-09-{day}T15:00:00+03:00", simulate=True)
    draft = engine.prepare({"brands": ["Alterna"], "kinds": ["preorder"], "supplier_ids": ["test_ru"],
        "source_text": "ТЕСТ: запросить предзаказ Alterna. Черновик для изменения текста и адресатов."}, "Тестовый постановщик", now="2026-10-01T13:00:00+03:00")
    return {"status": "created", "requests": len(engine.store.read()["requests"]), "messages_simulated": len(engine.store.read()["outbox"]),
            "draft_request_id": draft["id"], "external_messages_sent": 0}
