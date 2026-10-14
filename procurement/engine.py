from __future__ import annotations

import copy
import hashlib
import re
import shutil
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .adapters import SMTPTransport, SimulatedTransport, make_eml, network_gate
from .calendar import freshness, is_worktime, reminder_due, work_hours
from .catalog import validate_intent
from .common import WorkflowError, digest, iso, read_json, safe_filename, timestamp, valid_email, within
from .composer import compose, validate_draft
from .mail_test import routing
from .store import Store, event, notify


def item_key(item):
    return item["brand"], item["kind"]


def approval_digest(request):
    content = {"sender": request["sender"], "environment": request["environment"], "reminder_policy": request["reminder_policy"],
                   "deliveries": [{**{k: d[k] for k in ("id", "supplier_id", "recipients", "subject", "body", "excluded")},
                                   "items": [{"brand": i["brand"], "kind": i["kind"]} for i in d["items"]]} for d in request["deliveries"]]}
    if request.get("routing_policy"):
        content["routing_policy"] = request["routing_policy"]
        content["intended_recipients"] = [d["intended_recipients"] for d in request["deliveries"]]
    return digest(content)


def pending_items(delivery):
    return [item for item in delivery["items"] if item["status"] == "waiting"]


class Engine:
    def __init__(self, root, environment="test", settings=None, catalog=None, transport=None):
        self.root = Path(root).resolve()
        self.environment = environment
        self.settings = copy.deepcopy(settings or read_json(self.root / "config" / "settings.json"))
        self.settings["_project_root"] = str(self.root)
        if environment == "test":
            self.settings = {**self.settings, "mode": "test", "external_side_effects_enabled": False,
                             "mail": {**self.settings["mail"], "send_enabled": False, "read_enabled": False},
                             "calendar": self.settings["calendar"] if settings is not None else read_json(self.root / "fixtures" / "calendar.test.json")}
        self.catalog = catalog or read_json(self.root / "data" / ("catalog.test.json" if environment == "test" else "catalog.json"))
        self.store = Store(self.root, environment)
        if environment == "test" and transport is not None and not isinstance(transport, SimulatedTransport):
            raise WorkflowError("В тестовой среде допускается только симулятор транспорта.")
        self.transport = transport or (SimulatedTransport() if environment == "test" else SMTPTransport())
        if environment == "mail_test":
            routing(self.settings)

    def request(self, state, request_id):
        found = next((r for r in state["requests"] if r["id"] == request_id), None)
        if not found:
            raise WorkflowError("Запрос не найден.")
        return found

    def delivery(self, state, request_id, delivery_id):
        request = self.request(state, request_id)
        found = next((d for d in request["deliveries"] if d["id"] == delivery_id), None)
        if not found:
            raise WorkflowError("Поставка не найдена.")
        return request, found

    def fresh_price(self, state, supplier_id, brand, kind, now):
        matches = [p for p in state["prices"] if p["supplier_id"] == supplier_id and
                   (any(item_key(i) == (brand, kind) for i in p["coverage"]) if p.get("coverage") else brand in p["brands"] and kind in p["kinds"])]
        fresh = next((p for p in reversed(matches) if freshness(p, kind, now, self.settings) == "fresh"), None)
        uncertain = [p for p in matches if freshness(p, kind, now, self.settings) in {"unknown", "uncertain"}]
        return fresh, uncertain

    def prepare_mail_check(self, actor, now=None):
        """Prepare one explicitly requested diagnostic email, with no price items or reminders."""
        if self.environment != "mail_test":
            raise WorkflowError("Диагностическое письмо доступно только в почтовом тесте.")
        policy = routing(self.settings)
        now = timestamp(now)
        with self.store.transaction() as state:
            pending = next((r for r in state["requests"] if r.get("purpose") == "mail_connection_check" and
                            r["sender"] == policy["sender"] and r["status"] in {"draft", "approved", "active"}), None)
            if pending:
                return copy.deepcopy(pending)
            request_id, delivery_id = "req_" + uuid4().hex[:12], "delivery_" + uuid4().hex[:10]
            delivery = {"id":delivery_id, "supplier_id":"mail_connection_check", "supplier_name":"Проверка почты", "language":"ru",
                        "recipients":[policy["recipient"]], "intended_recipients":[], "items":[], "subject":"Проверка почты — Закупки [TEST]",
                        "body":"Это тестовое письмо из проекта «Закупки» для проверки отправки через Mail.ru.\n\nПожалуйста, ответьте на это письмо с Gmail, чтобы проверить получение ответа.",
                        "generator":"diagnostic_template", "excluded":False, "status":"draft", "sent_at":None, "last_sent_at":None,
                        "reminder_count":0, "next_action_at":None, "reply_ids":[], "paused":False}
            request = {"id":request_id, "purpose":"mail_connection_check", "environment":self.environment, "created_at":iso(now),
                       "actor":actor, "source_text":"Отправить одно диагностическое письмо Mail.ru → Gmail", "intent":{"purpose":"mail_connection_check"},
                       "sender":policy["sender"], "status":"draft", "deliveries":[delivery], "skipped":[], "blockers":[], "approval":None,
                       "reminder_policy":copy.deepcopy(self.settings["reminders"]), "routing_policy":policy}
            request["preview_digest"] = approval_digest(request)
            state["requests"].append(request)
            event(state, "diagnostic_prepared", now, request_id=request_id)
            return copy.deepcopy(request)

    def check_inventory(self, state, supplier_id, brand, now):
        if self.environment == "mail_test":
            # Only this isolated test history supplies prices; no supplier mailbox/Drive scan is asserted.
            return True
        matches = [c for c in state["inventory_checks"] if c["supplier_id"] == supplier_id and c["brand"] == brand and c.get("complete") is True]
        max_age = timedelta(hours=self.settings["sources"]["inventory_check_max_age_hours"])
        return any(timestamp(now) - max_age <= timestamp(c["checked_at"]) <= timestamp(now) for c in matches)

    def prepare(self, intent, actor, now=None):
        now = timestamp(now)
        intent = validate_intent(intent, self.catalog)
        request_id = "req_" + uuid4().hex[:12]
        with self.store.transaction() as state:
            deliveries, skipped, blockers = [], [], []
            if self.environment == "live":
                synced_at = self.catalog.get("synced_at")
                if not synced_at or timestamp(now) - timestamp(synced_at) > timedelta(hours=self.settings["sources"]["catalog_max_age_hours"]):
                    blockers.append("Обновите рабочий справочник поставщиков.")
            matched_brands = set()
            for supplier in self.catalog["suppliers"]:
                if intent.get("supplier_ids") is not None and supplier["id"] not in intent["supplier_ids"]:
                    continue
                brands = [b for b in intent["brands"] if b in supplier["brands"]]
                if not brands or supplier.get("enabled") is False:
                    continue
                matched_brands.update(brands)
                if supplier.get("enabled") is None:
                    blockers.append(f"Не уточнено участие поставщика {supplier['name']}.")
                    continue
                if not supplier.get("language"):
                    blockers.append(f"Не указан язык {supplier['name']}.")
                    continue
                items = []
                for brand in brands:
                    if not self.check_inventory(state, supplier["id"], brand, now):
                        blockers.append(f"Нужна проверка почты и Диска: {supplier['name']} / {brand}.")
                    for kind in intent["kinds"]:
                        price, uncertain = self.fresh_price(state, supplier["id"], brand, kind, now)
                        if price:
                            skipped.append({"supplier_id": supplier["id"], "supplier_name": supplier["name"], "brand": brand, "kind": kind,
                                            "reason": "fresh_price", "price_id": price["id"], "received_at": price.get("received_at"), "url": price.get("url")})
                        elif uncertain:
                            blockers.append(f"Дата прайса неоднозначна: {supplier['name']} / {brand} / {kind}.")
                            items.append({"brand": brand, "kind": kind, "status": "waiting"})
                        else:
                            active = any(not d["excluded"] and d["supplier_id"] == supplier["id"] and d["status"] in {"waiting", "sending", "approved", "question", "needs_review"}
                                         and any(item_key(i) == (brand, kind) for i in pending_items(d))
                                         for r in state["requests"] for d in r["deliveries"])
                            if active:
                                skipped.append({"supplier_id": supplier["id"], "supplier_name": supplier["name"], "brand": brand, "kind": kind, "reason": "already_requested"})
                            else:
                                items.append({"brand": brand, "kind": kind, "status": "waiting"})
                if items:
                    text = compose(supplier["language"], items, signature=self.settings["mail"]["signature_" + supplier["language"]])
                    intended = list(dict.fromkeys(e["address"].lower() for e in supplier["emails"]))
                    recipients = [routing(self.settings)["recipient"]] if self.environment == "mail_test" else intended
                    if self.environment == "mail_test":
                        text["subject"] += " [TEST " + supplier["id"] + "]"
                    deliveries.append({"id": "delivery_" + uuid4().hex[:10], "supplier_id": supplier["id"], "supplier_name": supplier["name"],
                                       "language": supplier["language"], "recipients": recipients,
                                       "items": items, **text, "excluded": False, "status": "draft", "sent_at": None,
                                       "last_sent_at": None, "reminder_count": 0, "next_action_at": None, "reply_ids": [], "paused": False})
                    if self.environment == "mail_test":
                        deliveries[-1]["intended_recipients"] = intended
            for brand in set(intent["brands"]) - matched_brands:
                blockers.append(f"Для бренда {brand} нет выбранного поставщика.")
            request = {"id": request_id, "environment": self.environment, "created_at": iso(now), "actor": actor,
                       "source_text": intent.get("source_text", ""), "intent": intent,
                       "sender": self.settings["mail"]["sender"], "status": "draft", "deliveries": deliveries,
                       "skipped": skipped, "blockers": list(dict.fromkeys(blockers)), "approval": None,
                       "reminder_policy": copy.deepcopy(self.settings["reminders"])}
            if self.environment == "mail_test":
                request["routing_policy"] = routing(self.settings)
                request["inventory_scope"] = "mail_test_history_only"
            request["preview_digest"] = approval_digest(request)
            state["requests"].append(request)
            event(state, "request_prepared", now, request_id=request_id)
        return request

    def revise(self, request_id, changes, now=None):
        with self.store.transaction() as state:
            request = self.request(state, request_id)
            if any(d["sent_at"] for d in request["deliveries"]):
                raise WorkflowError("Уже отправленный запрос нельзя переписать. Создайте новый или приостановите напоминания.")
            by_id = {d["id"]: d for d in request["deliveries"]}
            for change in changes:
                if change.get("id") not in by_id:
                    raise WorkflowError("Неизвестная доставка в изменениях.")
                delivery = by_id[change["id"]]
                if "recipients" in change:
                    recipients = change["recipients"]
                    if not recipients or not all(valid_email(a) for a in recipients):
                        raise WorkflowError("Укажите корректные адреса получателей.")
                    if self.environment == "mail_test":
                        routing(self.settings, recipients)
                    delivery["recipients"] = list(dict.fromkeys(a.lower() for a in recipients))
                if "excluded" in change:
                    delivery["excluded"] = bool(change["excluded"])
                if "subject" in change or "body" in change:
                    text = validate_draft({**delivery, **change, "generator": "codex_or_user"})
                    delivery.update(text)
                delivery["status"] = "draft"
            request["status"] = "draft"
            request["approval"] = None
            request["preview_digest"] = approval_digest(request)
            event(state, "request_revised", now, request_id=request_id)
            return copy.deepcopy(request)

    def approve(self, request_id, preview_digest, actor, now=None):
        work_hours(self.settings)
        with self.store.transaction() as state:
            request = self.request(state, request_id)
            if self.environment == "mail_test":
                if request.get("routing_policy") != routing(self.settings):
                    raise WorkflowError("Настройки почтового теста изменились. Подготовьте новый запрос.")
                for delivery in request["deliveries"]:
                    routing(self.settings, delivery["recipients"])
            if request["blockers"]:
                raise WorkflowError("Нельзя подтверждать запрос с нерешёнными вопросами: " + "; ".join(request["blockers"]))
            if preview_digest != approval_digest(request):
                raise WorkflowError("Предпросмотр изменился. Покажите новый текст и адресатов и получите подтверждение.")
            if request["sender"] != self.settings["mail"]["sender"] or request["reminder_policy"] != self.settings["reminders"]:
                raise WorkflowError("Настройки отправителя или напоминаний изменились. Создайте новый предпросмотр.")
            if not any(not d["excluded"] for d in request["deliveries"]):
                raise WorkflowError("Нет писем для подтверждения.")
            if any(d["sent_at"] for d in request["deliveries"]):
                raise WorkflowError("Запрос уже выполняется.")
            request["approval"] = {"digest": preview_digest, "actor": actor, "at": iso(now), "environment": self.environment}
            request["status"] = "approved"
            for delivery in request["deliveries"]:
                if not delivery["excluded"]:
                    delivery["status"] = "approved"
            event(state, "request_approved", now, request_id=request_id, actor=actor, simulated=self.environment == "test")
            return copy.deepcopy(request)

    def dispatch(self, request_id, delivery_id, now=None, reminder=False, draft=None):
        now = timestamp(now)
        if not is_worktime(now, self.settings):
            raise WorkflowError("Сейчас нерабочее время. Письмо оставлено в очереди.")
        if self.environment != "test":
            if self.settings["mode"] != self.environment:
                raise WorkflowError("Среда отправки не совпадает с режимом подключения.")
            network_gate(self.settings, "smtp")
            checked = self.store.directory / "connection.json"
            connection = read_json(checked) if checked.is_file() else {}
            if connection.get("status") != "connected" or connection.get("sender") != self.settings["mail"]["sender"]:
                raise WorkflowError("Проверка подключения почты не пройдена. Выполните --env " + self.environment + " mail-check после настройки .env.")
        with self.store.transaction() as state:
            request, delivery = self.delivery(state, request_id, delivery_id)
            if self.environment == "mail_test":
                routing(self.settings, delivery["recipients"])
                if request.get("routing_policy") != routing(self.settings):
                    raise WorkflowError("Маршрут почтового теста изменился после согласования.")
            if delivery["excluded"] or delivery["paused"]:
                raise WorkflowError("Отправка для этого поставщика выключена.")
            if not request["approval"] or request["approval"]["digest"] != approval_digest(request):
                raise WorkflowError("Нужно подтверждение показанного содержания и адресатов.")
            if request["sender"] != self.settings["mail"]["sender"] or request["reminder_policy"] != self.settings["reminders"]:
                raise WorkflowError("Настройки изменились после подтверждения.")
            if reminder:
                if request.get("purpose") == "mail_connection_check":
                    raise WorkflowError("Для диагностического письма напоминания не назначены.")
                if delivery["status"] not in {"waiting", "question"} or not delivery["next_action_at"] or now < timestamp(delivery["next_action_at"]):
                    raise WorkflowError("Напоминание ещё не требуется.")
                if delivery["reminder_count"] >= self.settings["reminders"]["max_count"]:
                    raise WorkflowError("Пять напоминаний уже отправлены.")
                attempt = delivery["reminder_count"] + 1
                items = pending_items(delivery)
                if not items:
                    raise WorkflowError("Все запрошенные прайсы получены.")
                text = validate_draft(draft) if draft else compose(delivery["language"], items, reminder=attempt,
                                                                 signature=self.settings["mail"]["signature_" + delivery["language"]])
                previous = [o for o in state["outbox"] if o["delivery_id"] == delivery_id]
                if any(o["subject"] == text["subject"] or o["body"] == text["body"] for o in previous):
                    raise WorkflowError("Напоминание должно иметь новую тему и новый текст.")
                if self.environment != "test" and draft is None:
                    raise WorkflowError("Codex должен подготовить текст рабочего напоминания.")
            else:
                if delivery["status"] != "approved" or delivery["sent_at"]:
                    raise WorkflowError("Первое письмо уже отправлено или не подтверждено.")
                attempt, items = 0, pending_items(delivery)
                text = validate_draft(delivery)
                for item in items:
                    if self.fresh_price(state, delivery["supplier_id"], item["brand"], item["kind"], now)[0]:
                        raise WorkflowError("После предпросмотра появился свежий прайс. Пересоздайте запрос.")
                    if not self.check_inventory(state, delivery["supplier_id"], item["brand"], now):
                        raise WorkflowError("Проверка почты и Диска устарела. Обновите её перед отправкой.")
                    duplicate = any(r["id"] != request_id and not d["excluded"] and d["supplier_id"] == delivery["supplier_id"] and d["sent_at"]
                                    and d["status"] in {"waiting", "question", "partial_delivery", "sending"}
                                    and any(item_key(i) == item_key(item) for i in pending_items(d))
                                    for r in state["requests"] for d in r["deliveries"])
                    if duplicate:
                        raise WorkflowError("Другой запрос уже ожидает тот же прайс. Дублирование остановлено.")
            if any(m.get("delivery_id") == delivery_id and m.get("classification") == "needs_review" for m in state["inbox"]):
                raise WorkflowError("Сначала разберите новое письмо поставщика.")
            outgoing = {"id": "out_" + uuid4().hex[:12], "request_id": request_id, "delivery_id": delivery_id,
                        "message_id": f"<purchases.{uuid4().hex}@{request['sender'].split('@')[-1]}>",
                        "recipients": list(delivery["recipients"]), "subject": text["subject"], "body": text["body"],
                        "items": copy.deepcopy(items), "reminder": attempt, "prepared_at": iso(now), "status": "sending",
                        "simulated": self.environment == "test"}
            outgoing["environment"] = self.environment
            if request.get("purpose") == "mail_connection_check":
                outgoing["purpose"] = "mail_connection_check"
            originals = [o for o in state["outbox"] if o["delivery_id"] == delivery_id and o["reminder"] == 0]
            if originals:
                outgoing["in_reply_to"] = originals[0]["message_id"]
            state["outbox"].append(outgoing)
            delivery["status"] = "sending"
            event(state, "send_reserved", now, request_id=request_id, delivery_id=delivery_id, outgoing_id=outgoing["id"])
        eml_path = self.store.directory / "outbox" / (outgoing["id"] + ".eml")
        eml_path.parent.mkdir(parents=True, exist_ok=True)
        eml_path.write_bytes(make_eml(outgoing, request["sender"]).as_bytes())
        try:
            receipt = self.transport.send(outgoing, self.settings)
        except Exception as exc:
            with self.store.transaction() as state:
                request, delivery = self.delivery(state, request_id, delivery_id)
                saved = next(o for o in state["outbox"] if o["id"] == outgoing["id"])
                saved["status"] = "outcome_unknown"
                saved["error_type"] = type(exc).__name__
                delivery["status"] = "outcome_unknown"
                notify(state, outgoing["id"], "Ошибка отправки. Результат неизвестен; требуется сверка почты, автоматического повтора нет.", now, request_id, delivery_id)
                event(state, "send_outcome_unknown", now, request_id=request_id, delivery_id=delivery_id)
            raise WorkflowError("Результат отправки неизвестен. Проверьте историю и ящик перед повтором.") from None
        with self.store.transaction() as state:
            request, delivery = self.delivery(state, request_id, delivery_id)
            saved = next(o for o in state["outbox"] if o["id"] == outgoing["id"])
            saved.update(receipt)
            saved["status"] = "simulated" if receipt["simulated"] else "sent" if receipt["accepted"] else "failed"
            saved["sent_at"] = iso(now) if receipt["accepted"] else None
            if not receipt["accepted"]:
                delivery["status"] = "send_failed"
                notify(state, outgoing["id"], f"Не удалось отправить письмо поставщику {delivery['supplier_name']}.", now, request_id, delivery_id)
            else:
                if not delivery["sent_at"]:
                    delivery["sent_at"] = iso(now)
                delivery["last_sent_at"] = iso(now)
                delivery["reminder_count"] = attempt
                diagnostic = request.get("purpose") == "mail_connection_check"
                delivery["next_action_at"] = None if diagnostic else iso(reminder_due(now, self.settings))
                delivery["status"] = "waiting_test_reply" if diagnostic else "question" if delivery.get("has_open_question") else "waiting"
                if receipt["refused"]:
                    delivery["status"] = "partial_delivery"
                    notify(state, outgoing["id"], f"Часть адресов {delivery['supplier_name']} не приняла письмо; требуется решение пользователя.", now, request_id, delivery_id)
            request["status"] = "active"
            event(state, "mail_simulated" if receipt["simulated"] else "mail_sent", now, request_id=request_id, delivery_id=delivery_id,
                  outgoing_id=outgoing["id"], reminder=attempt)
            return copy.deepcopy(saved)

    def match_reply(self, state, message):
        references = set(message.get("references", []))
        if message.get("delivery_id"):
            candidates = [(r, d) for r in state["requests"] for d in r["deliveries"] if d["id"] == message["delivery_id"]]
        else:
            ids = {o["delivery_id"] for o in state["outbox"] if o["message_id"] in references}
            candidates = [(r, d) for r in state["requests"] for d in r["deliveries"] if d["id"] in ids]
        if not candidates:
            candidates = [(r, d) for r in state["requests"] for d in r["deliveries"]
                          if d["sent_at"] and d["status"] in {"waiting", "question", "partial_delivery"}
                          and message["from"].lower() in d["recipients"]]
        candidates = [(r, d) for r, d in candidates if message["from"].lower() in d["recipients"] and d["sent_at"]
                      and timestamp(message["received_at"]) >= timestamp(d["sent_at"])]
        return candidates[0] if len(candidates) == 1 else None

    def ingest(self, message, now=None):
        now = timestamp(now)
        if not message.get("id") or not valid_email(message.get("from", "")):
            raise WorkflowError("Входящее письмо должно иметь id и корректный адрес отправителя.")
        if not message.get("received_at"):
            raise WorkflowError("Укажите время получения письма.")
        timestamp(message.get("received_at"))
        with self.store.transaction() as state:
            if any(m["id"] == message["id"] for m in state["inbox"]):
                return {"duplicate": True, "id": message["id"]}
            matched = self.match_reply(state, message)
            stored = {k: copy.deepcopy(v) for k, v in message.items() if k != "attachments"}
            stored["classification"] = "needs_review"
            stored["attachments"] = []
            for index, attachment in enumerate(message.get("attachments", [])):
                name = safe_filename(attachment["name"])
                if Path(name).suffix.lower() not in self.settings["files"]["accepted_extensions"]:
                    stored["attachments"].append({"name": name, "accepted": False, "reason": "unsupported_extension"})
                    continue
                if "bytes" in attachment:
                    payload = attachment["bytes"]
                elif attachment.get("path"):
                    payload = within(self.root, self.root / attachment["path"]).read_bytes()
                else:
                    raise WorkflowError("У вложения нет содержимого или локального файла.")
                if len(payload) > self.settings["files"]["max_attachment_bytes"]:
                    raise WorkflowError("Размер вложения превышает лимит; требуется ручная обработка.")
                message_folder = hashlib.sha256(message["id"].encode()).hexdigest()[:16]
                target = within(self.store.directory, self.store.directory / "attachments" / message_folder / str(index) / name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                stored["attachments"].append({"name": name, "accepted": True, "path": target.relative_to(self.root).as_posix(),
                                              "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)})
            if matched:
                request, delivery = matched
                stored["request_id"], stored["delivery_id"] = request["id"], delivery["id"]
                delivery["reply_ids"].append(stored["id"])
                if request.get("purpose") == "mail_connection_check":
                    stored["classification"] = "diagnostic_reply"
                    delivery["status"] = "completed"
                    delivery["next_action_at"] = None
                    request["status"] = "completed"
                    event(state, "mail_round_trip_verified", now, request_id=request["id"], delivery_id=delivery["id"], message_id=stored["id"])
                    notify(state, "diagnostic_" + stored["id"], "Получен ответ Gmail на тестовое письмо: отправка Mail.ru и приём ответа проверены.", now, request["id"], delivery["id"])
            else:
                stored["request_id"], stored["delivery_id"] = None, None
                notify(state, "unmatched_" + stored["id"], "Получено письмо, которое нельзя однозначно связать с запросом. Требуется разбор в Codex.", now)
            state["inbox"].append(stored)
            event(state, "mail_received", now, request_id=stored["request_id"], delivery_id=stored["delivery_id"], message_id=stored["id"])
            return copy.deepcopy(stored)

    def classify(self, message_id, classification, now=None):
        now = timestamp(now)
        kind = classification.get("kind")
        if kind not in {"price", "question", "other"}:
            raise WorkflowError("Допускается классификация price, question или other.")
        with self.store.transaction() as state:
            message = next((m for m in state["inbox"] if m["id"] == message_id), None)
            if not message or not message.get("delivery_id"):
                raise WorkflowError("Сначала однозначно сопоставьте входящее письмо с запросом.")
            if message["classification"] != "needs_review":
                return {"duplicate": True, "id": message_id}
            request, delivery = self.delivery(state, message["request_id"], message["delivery_id"])
            if kind == "price":
                coverage = classification.get("coverage", [])
                known_items = {item_key(i) for i in delivery["items"]}
                if not coverage or not {item_key(i) for i in coverage} <= known_items:
                    raise WorkflowError("Нужно явно подтвердить бренды и виды прайса из этого запроса.")
                evidence = classification.get("evidence", {})
                assets = [a for a in message["attachments"] if a.get("accepted") and a["name"] in evidence.get("attachment_names", [])]
                links = evidence.get("links", [])
                if any(link not in message.get("links", []) or not re.match(r"https?://", link) for link in links):
                    raise WorkflowError("Ссылка должна присутствовать в полученном письме.")
                if not assets and not links:
                    raise WorkflowError("Нельзя считать ответ прайсом без подтверждённого файла или ссылки.")
                covered = {item_key(i) for i in coverage}
                for item in delivery["items"]:
                    if item_key(item) in covered:
                        item["status"] = "received"
                        item["message_id"] = message_id
                price = {"id": "price_" + uuid4().hex[:12], "supplier_id": delivery["supplier_id"],
                         "brands": list(dict.fromkeys(i["brand"] for i in coverage)), "kinds": list(dict.fromkeys(i["kind"] for i in coverage)),
                         "coverage": copy.deepcopy(coverage), "received_at": message["received_at"], "source": "email", "message_id": message_id,
                         "url": links[0] if links else None, "attachments": assets, "storage_status": "pending",
                         "classification_by": classification.get("actor", "codex")}
                state["prices"].append(price)
                storage_brands = evidence.get("file_brands", price["brands"])
                if not storage_brands or not set(storage_brands) <= {b["name"] for b in self.catalog["brands"]} or not set(price["brands"]) <= set(storage_brands):
                    raise WorkflowError("Укажите проверенные бренды всего файла, включая полученные позиции.")
                price["storage_brands"] = list(dict.fromkeys(storage_brands))
                mixed = len(storage_brands) > 1
                if self.environment == "mail_test":
                    price["storage_status"] = "stored_local"
                    if assets and not price["url"]:
                        price["url"] = assets[0]["path"]
                for asset in (assets if self.environment != "mail_test" else []):
                    state["jobs"].append({"id": "job_" + uuid4().hex[:12], "kind": "drive_upload", "status": "awaiting_route" if mixed else "pending",
                                          "price_id": price["id"], "supplier_id": price["supplier_id"], "brands": price["storage_brands"],
                                          "received_at": message["received_at"], "local_path": asset["path"], "original_name": asset["name"],
                                          "destination_folder_id": None, "simulated": self.environment == "test"})
                for link in (links if self.environment != "mail_test" else []):
                    state["jobs"].append({"id": "job_" + uuid4().hex[:12], "kind": "price_link", "status": "awaiting_route" if mixed else "pending",
                                          "price_id": price["id"], "url": link, "supplier_id": price["supplier_id"], "brands": price["storage_brands"],
                                          "destination_folder_id": None, "simulated": self.environment == "test"})
                delivery["status"] = "completed" if not pending_items(delivery) else "waiting"
                if delivery["status"] == "completed":
                    delivery["next_action_at"] = None
                notify(state, "price_" + message_id, f"Получен прайс от {delivery['supplier_name']}: {', '.join(price['brands'])}." +
                       (" Для файла с несколькими брендами выберите папку сохранения." if mixed and self.environment != "mail_test" else ""), now, request["id"], delivery["id"])
            elif kind == "question":
                delivery["has_open_question"] = True
                delivery["status"] = "question" if pending_items(delivery) else delivery["status"]
                notify(state, "question_" + message_id, f"Поставщик {delivery['supplier_name']} задаёт вопрос: {message['body']}", now, request["id"], delivery["id"])
            message["classification"] = kind
            message["review"] = classification
            if all(d["excluded"] or d["status"] == "completed" for d in request["deliveries"]):
                request["status"] = "completed"
            event(state, "reply_classified", now, request_id=request["id"], delivery_id=delivery["id"], message_id=message_id, classification=kind)
            return copy.deepcopy(message)

    def tick(self, now=None, simulate=False):
        now = timestamp(now)
        if not is_worktime(now, self.settings):
            return {"status": "outside_worktime", "actions": []}
        actions = []
        with self.store.transaction() as state:
            for request in state["requests"]:
                for delivery in request["deliveries"]:
                    if delivery["status"] not in {"waiting", "question"} or delivery["excluded"] or delivery["paused"] or not delivery["next_action_at"]:
                        continue
                    if now < timestamp(delivery["next_action_at"]):
                        continue
                    if any(m.get("delivery_id") == delivery["id"] and m["classification"] == "needs_review" for m in state["inbox"]):
                        actions.append({"kind": "review_reply", "request_id": request["id"], "delivery_id": delivery["id"]})
                        continue
                    if delivery["reminder_count"] >= self.settings["reminders"]["max_count"]:
                        delivery["status"] = "exhausted"
                        delivery["next_action_at"] = None
                        notify(state, "exhausted_" + delivery["id"],
                               f"Поставщик: {delivery['supplier_name']}\nПочта: {', '.join(delivery['recipients'])}\nСтатус: после первого письма и пяти напоминаний (6 писем) прайс не получен.",
                               now, request["id"], delivery["id"])
                        event(state, "reminders_exhausted", now, request_id=request["id"], delivery_id=delivery["id"])
                        actions.append({"kind": "exhausted", "request_id": request["id"], "delivery_id": delivery["id"]})
                    else:
                        actions.append({"kind": "reminder_due", "request_id": request["id"], "delivery_id": delivery["id"],
                                        "reminder": delivery["reminder_count"] + 1, "items": copy.deepcopy(pending_items(delivery)), "language": delivery["language"]})
        if simulate:
            if self.environment != "test":
                raise WorkflowError("Симуляция напоминаний допускается только в тестовой истории.")
            for action in actions:
                if action["kind"] == "reminder_due":
                    action["outgoing"] = self.dispatch(action["request_id"], action["delivery_id"], now, reminder=True)
        return {"status": "checked", "actions": actions}

    def inventory_import(self, payload, now=None):
        now = timestamp(now)
        known = {s["id"]: s for s in self.catalog["suppliers"]}
        with self.store.transaction() as state:
            for price in payload.get("prices", []):
                if price["supplier_id"] not in known or not price.get("brands") or not set(price["brands"]) <= set(known[price["supplier_id"]]["brands"]):
                    raise WorkflowError("Прайс в индексе имеет неизвестного поставщика или бренд.")
                if not price.get("kinds") or not set(price["kinds"]) <= {"stock", "preorder"}:
                    raise WorkflowError("Укажите виды прайса в индексе.")
                if not any(p["id"] == price["id"] for p in state["prices"]):
                    state["prices"].append(copy.deepcopy(price))
            for check in payload.get("checks", []):
                if check["supplier_id"] not in known or check["brand"] not in known[check["supplier_id"]]["brands"]:
                    raise WorkflowError("Неизвестная пара поставщик — бренд в проверке.")
                timestamp(check["checked_at"])
                state["inventory_checks"].append(copy.deepcopy(check))
            for request in state["requests"]:
                for delivery in request["deliveries"]:
                    if not delivery["sent_at"] or delivery["status"] not in {"waiting", "question"}:
                        continue
                    refreshed = []
                    for item in pending_items(delivery):
                        price, _ = self.fresh_price(state, delivery["supplier_id"], item["brand"], item["kind"], now)
                        if price:
                            item.update(status="received", price_id=price["id"])
                            refreshed.append(item["brand"] + " / " + item["kind"])
                    if refreshed:
                        if not pending_items(delivery):
                            delivery["status"] = "completed"
                            delivery["next_action_at"] = None
                        event(state, "inventory_satisfied_request", now, request_id=request["id"], delivery_id=delivery["id"], items=refreshed)
                        notify(state, "inventory_" + delivery["id"] + "_" + digest(refreshed), "При проверке найден свежий прайс: " + delivery["supplier_name"] + ": " + ", ".join(refreshed), now, request["id"], delivery["id"])
                if request["deliveries"] and all(d["excluded"] or d["status"] == "completed" for d in request["deliveries"]):
                    request["status"] = "completed"
            event(state, "inventory_refreshed", now)

    def route_job(self, job_id, folder_id, now=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", folder_id):
            raise WorkflowError("Нужен ID выбранной папки Google Диска.")
        with self.store.transaction() as state:
            job = next((j for j in state["jobs"] if j["id"] == job_id), None)
            if not job or job["status"] not in {"pending", "awaiting_route"}:
                raise WorkflowError("Задание не найдено или уже выполнено.")
            job["destination_folder_id"] = folder_id
            job["status"] = "pending"
            event(state, "storage_route_selected", now, job_id=job_id, folder_id=folder_id)

    def complete_job(self, job_id, result, now=None):
        with self.store.transaction() as state:
            job = next((j for j in state["jobs"] if j["id"] == job_id), None)
            if not job:
                raise WorkflowError("Задание не найдено.")
            if job["status"] in {"completed", "completed_simulated"}:
                return {"duplicate": True, "id": job_id}
            if job["status"] != "pending" or not job.get("destination_folder_id"):
                raise WorkflowError("Сначала выберите папку сохранения.")
            if self.environment != "test":
                network_gate(self.settings, "drive")
                if not result.get("verified") or not re.match(r"https://(?:drive|docs)\.google\.com/", result.get("url", "")):
                    raise WorkflowError("Нужно подтверждённое чтением результата сохранение на Google Диск.")
            else:
                local_url = result.get("url", "")
                if not result.get("simulated") or not re.match(r"(?:data/test/|fixtures/)", local_url) or not within(self.root, self.root / local_url).is_file():
                    raise WorkflowError("Тестовый результат должен быть локальным и явно помечен simulated.")
            job["status"] = "completed_simulated" if self.environment == "test" else "completed"
            job["result"] = result
            price = next(p for p in state["prices"] if p["id"] == job["price_id"])
            price["url"] = result["url"]
            price["storage_status"] = job["status"]
            notify(state, "saved_" + job_id, f"Прайс сохранён{' в тестовую папку' if self.environment == 'test' else ' на Google Диск'}: {job.get('original_name', '')}\n{result['url']}",
                   now, price.get("request_id"))
            event(state, "price_storage_simulated" if self.environment == "test" else "price_stored", now, job_id=job_id)
            return copy.deepcopy(job)

    def pause(self, request_id, delivery_id, paused=True, now=None):
        with self.store.transaction() as state:
            _, delivery = self.delivery(state, request_id, delivery_id)
            delivery["paused"] = bool(paused)
            event(state, "reminders_paused" if paused else "reminders_resumed", now, request_id=request_id, delivery_id=delivery_id)

    def notifications(self, acknowledge=False):
        with self.store.transaction() as state:
            notes = copy.deepcopy([n for n in state["notifications"] if not n["acknowledged"]])
            if acknowledge:
                for note in state["notifications"]:
                    note["acknowledged"] = True
            return notes

    def associate_reply(self, message_id, request_id, delivery_id, actor, now=None):
        if not actor:
            raise WorkflowError("Ручное сопоставление должно иметь автора решения.")
        with self.store.transaction() as state:
            message = next((m for m in state["inbox"] if m["id"] == message_id), None)
            request, delivery = self.delivery(state, request_id, delivery_id)
            if not message or message["classification"] != "needs_review" or message.get("delivery_id"):
                raise WorkflowError("Сопоставлять можно только неразобранное и ещё не привязанное письмо.")
            if not delivery["sent_at"] or timestamp(message["received_at"]) < timestamp(delivery["sent_at"]):
                raise WorkflowError("Ответ предшествует запросу.")
            message["request_id"], message["delivery_id"] = request_id, delivery_id
            message["manual_association"] = {"actor": actor, "at": iso(now)}
            delivery["reply_ids"].append(message_id)
            event(state, "reply_associated_manually", now, request_id=request_id, delivery_id=delivery_id, message_id=message_id, actor=actor)
