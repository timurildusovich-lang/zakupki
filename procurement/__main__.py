from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import check_mail, parse_eml, poll_imap
from .catalog import normalize_sheet, parse_intent
from .common import WorkflowError, atomic_json, iso, read_json, within
from .demo import run_demo
from .engine import Engine
from .report import render


def parser():
    result = argparse.ArgumentParser(description="Запросить прайс-листы — локальный помощник Codex")
    result.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    result.add_argument("--env", choices=["test", "mail_test", "live"], default="test")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ["demo", "report", "doctor", "poll", "mail-check"]:
        commands.add_parser(name)
    catalog = commands.add_parser("import-catalog")
    catalog.add_argument("--file", required=True, type=Path)
    prepare = commands.add_parser("prepare")
    source = prepare.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--intent", type=Path)
    prepare.add_argument("--actor", default="Пользователь Codex")
    prepare.add_argument("--at")
    diagnostic = commands.add_parser("prepare-mail-check")
    diagnostic.add_argument("--actor", default="Пользователь Codex")
    diagnostic.add_argument("--at")
    for name in ["preview", "approve", "send", "revise", "pause", "remind", "resume"]:
        command = commands.add_parser(name)
        command.add_argument("--request", required=True)
        command.add_argument("--at")
        if name in {"send", "pause", "resume", "remind"}:
            command.add_argument("--delivery", required=name in {"pause", "resume", "remind"})
        if name == "approve":
            command.add_argument("--digest", required=True)
            command.add_argument("--actor", required=True)
        if name in {"revise", "remind"}:
            command.add_argument("--file", required=True, type=Path)
    for name in ["ingest", "inventory"]:
        command = commands.add_parser(name)
        command.add_argument("--file", required=True, type=Path)
        command.add_argument("--at")
    eml = commands.add_parser("ingest-eml")
    eml.add_argument("--file", type=Path, required=True)
    eml.add_argument("--received-at", required=True)
    classify = commands.add_parser("classify")
    classify.add_argument("--message", required=True)
    classify.add_argument("--file", required=True, type=Path)
    classify.add_argument("--at")
    associate = commands.add_parser("associate")
    for field in ["message", "request", "delivery", "actor"]:
        associate.add_argument("--" + field, required=True)
    tick = commands.add_parser("tick")
    tick.add_argument("--at")
    tick.add_argument("--simulate", action="store_true")
    notes = commands.add_parser("notifications")
    notes.add_argument("--acknowledge", action="store_true")
    route = commands.add_parser("route")
    route.add_argument("--job", required=True)
    route.add_argument("--folder", required=True)
    complete = commands.add_parser("complete-job")
    complete.add_argument("--job", required=True)
    complete.add_argument("--file", required=True, type=Path)
    return result


def execute(args):
    root = args.root.resolve()
    if args.command == "report":
        return {"history_html": render(root)}
    if args.command == "import-catalog":
        source = read_json(within(root, root / args.file))
        rows = source.get("values", source.get("structuredContent", {}).get("values"))
        catalog = normalize_sheet(rows, read_json(root / "config" / "catalog-resolutions.json"))
        catalog.update({"source": "https://docs.google.com/spreadsheets/d/1-yQBQXjbKm8dXp_9Et4_GP2SQnS35DA4pOvx2U4nBKM/edit",
                        "source_sheet": "Лист1", "source_range": source.get("range", "A1:Z130"), "synced_at": source.get("fetched_at", iso()),
                        "generic_price_kind": None})
        atomic_json(root / "data" / "catalog.json", catalog)
        return {"suppliers": len(catalog["suppliers"]), "active_suppliers": sum(s["enabled"] is True for s in catalog["suppliers"]),
                "emails": sum(len(s["emails"]) for s in catalog["suppliers"]), "brands": len(catalog["brands"]), "warnings": catalog["warnings"]}
    if args.command == "demo":
        if args.env != "test":
            raise WorkflowError("Демонстрация работает только в тестовой среде.")
        return run_demo(root)
    engine = Engine(root, args.env)
    now = getattr(args, "at", None)
    load = lambda p: read_json(within(root, root / p))
    if args.command == "doctor":
        connection_path = engine.store.directory / "connection.json"
        connection = read_json(connection_path) if connection_path.is_file() else {}
        return {"environment": args.env, "mode": engine.settings["mode"], "external_side_effects_enabled": engine.settings["external_side_effects_enabled"],
                "sender": engine.settings["mail"]["sender"], "smtp_enabled": engine.settings["mail"]["send_enabled"],
                "imap_enabled": engine.settings["mail"]["read_enabled"], "calendar": engine.settings["calendar"],
                "directory_warnings": engine.catalog.get("warnings", []), "state_file": str(engine.store.path),
                "mail_test_recipient": engine.settings.get("mail_test", {}).get("recipient"),
                "connection_status": connection.get("status", "not_checked"),
                "live_ready": engine.settings["mode"] == "live" and engine.settings["external_side_effects_enabled"] is True and
                              connection.get("status") == "connected" and connection.get("sender") == engine.settings["mail"]["sender"]}
    if args.command == "mail-check":
        if args.env == "test" or args.env != engine.settings["mode"]:
            raise WorkflowError("Проверка подключения требует выбранной сетевой среды mail_test или live.")
        result, baseline = check_mail(engine.settings)
        result["checked_at"] = iso()
        atomic_json(engine.store.directory / "connection.json", result)
        transition=engine.settings.get("production_transition", {})
        if args.env == "live" and result["status"] == "connected" and transition.get("authorized") and transition.get("mail_test_completed") and transition.get("sender") == engine.settings["mail"]["sender"]:
            settings_path=root / "config/settings.json"
            settings=read_json(settings_path)
            settings["production_transition"]["status"]="completed"
            settings["production_transition"]["completed_at"]=iso()
            atomic_json(settings_path,settings)
        if args.env == "mail_test" and baseline:
            cursor_path = engine.store.directory / "imap-cursor.json"
            if not cursor_path.exists():
                atomic_json(cursor_path, baseline)
        return result
    if args.command == "prepare":
        intent = load(args.intent) if args.intent else parse_intent(args.text, engine.catalog)
        if intent.get("questions"):
            return {"status": "needs_clarification", "questions": intent["questions"], "intent": intent}
        return engine.prepare(intent, args.actor, now)
    if args.command == "prepare-mail-check":
        return engine.prepare_mail_check(args.actor, now)
    if args.command == "preview":
        return engine.request(engine.store.read(), args.request)
    if args.command == "revise":
        changes = load(args.file)
        return engine.revise(args.request, changes.get("deliveries", changes) if isinstance(changes, dict) else changes, now)
    if args.command == "approve":
        return engine.approve(args.request, args.digest, args.actor, now)
    if args.command == "send":
        request = engine.request(engine.store.read(), args.request)
        eligible = [d for d in request["deliveries"] if not d["excluded"] and d["status"] == "approved" and (not args.delivery or d["id"] == args.delivery)]
        if not eligible:
            raise WorkflowError("Нет подтверждённых писем для отправки.")
        return [engine.dispatch(args.request, d["id"], now) for d in eligible]
    if args.command == "remind":
        return engine.dispatch(args.request, args.delivery, now, reminder=True, draft=load(args.file))
    if args.command in {"pause", "resume"}:
        engine.pause(args.request, args.delivery, args.command == "pause", now)
        return {"status": args.command}
    if args.command == "ingest":
        return engine.ingest(load(args.file), now)
    if args.command == "ingest-eml":
        path = within(root, root / args.file)
        mail = parse_eml(path.read_bytes(), args.received_at, engine.settings["mail"]["sender"])
        return engine.ingest(mail)
    if args.command == "classify":
        return engine.classify(args.message, load(args.file), now)
    if args.command == "associate":
        engine.associate_reply(args.message, args.request, args.delivery, args.actor)
        return {"status": "associated", "message": args.message}
    if args.command == "inventory":
        engine.inventory_import(load(args.file), now)
        return {"status": "inventory_refreshed"}
    if args.command == "tick":
        return engine.tick(now, args.simulate)
    if args.command == "notifications":
        return engine.notifications(args.acknowledge)
    if args.command == "route":
        engine.route_job(args.job, args.folder)
        return {"status": "route_selected"}
    if args.command == "complete-job":
        return engine.complete_job(args.job, load(args.file))
    if args.command == "poll":
        if args.env not in {"mail_test", "live"} or args.env != engine.settings["mode"]:
            raise WorkflowError("В тестовом режиме входящие письма берутся из локальных фикстур через ingest.")
        cursor_path = engine.store.directory / "imap-cursor.json"
        cursor = read_json(cursor_path) if cursor_path.exists() else None
        messages, new_cursor = poll_imap(engine.settings, cursor)
        saved = [engine.ingest(m) for m in messages]
        atomic_json(cursor_path, new_cursor)
        return {"received": len(saved), "message_ids": [m["id"] for m in saved]}
    raise WorkflowError("Команда не реализована.")


def main():
    args = parser().parse_args()
    try:
        result = execute(args)
        if args.command != "report":
            render(args.root.resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (WorkflowError, OSError, KeyError, ValueError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
