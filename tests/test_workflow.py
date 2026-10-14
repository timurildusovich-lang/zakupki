from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from procurement.adapters import SMTPTransport, SimulatedTransport, network_gate, parse_eml
from procurement.calendar import add_month, folder_date, freshness, is_worktime, reminder_due
from procurement.catalog import find_brands, normalize_sheet, parse_intent
from procurement.common import WorkflowError, atomic_json, digest, read_json, timestamp
from procurement.demo import run_demo
from procurement.engine import Engine, approval_digest, pending_items
from procurement.report import render
from procurement.store import Store


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-10-01T15:00:00+03:00"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        stage = ROOT / ".staging" / "tests"
        stage.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="workflow-", dir=stage)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(stage.resolve()))
        for folder in ["config", "fixtures", "web"]:
            shutil.copytree(ROOT / folder, self.root / folder)
        (self.root / "data").mkdir()
        shutil.copy(ROOT / "data" / "catalog.test.json", self.root / "data" / "catalog.test.json")
        shutil.copy(ROOT / "data" / "catalog.test.json", self.root / "data" / "catalog.json")
        self.engine = Engine(self.root)
        self.scan(NOW)

    def tearDown(self):
        self.temporary.cleanup()

    def scan(self, at, engine=None):
        engine = engine or self.engine
        engine.inventory_import({"checks": [{"supplier_id": s["id"], "brand": b, "checked_at": at,
                                              "complete": True, "sources": ["fixture_mail", "fixture_drive"]}
                                             for s in engine.catalog["suppliers"] for b in s["brands"]]}, at)

    def plan(self, brands=None, kinds=None, suppliers=None, at=NOW):
        return self.engine.prepare({"brands": brands or ["Davines"], "kinds": kinds or ["stock"],
                                    "supplier_ids": suppliers or ["test_eu"], "source_text": "Test request"}, "Test actor", at)

    def sent(self, brands=None, kinds=None, suppliers=None, at=NOW):
        request = self.plan(brands, kinds, suppliers, at)
        self.engine.approve(request["id"], request["preview_digest"], "Test approval", at)
        for delivery in request["deliveries"]:
            self.engine.dispatch(request["id"], delivery["id"], at)
        return request

    def reply(self, request, kind="price", coverage=None, mid="test-reply", from_address="prices@example.invalid", evidence=None, **extras):
        delivery = request["deliveries"][0]
        message = {"id": mid, "from": from_address, "delivery_id": delivery["id"], "subject": "Different subject",
                   "body": "Current list", "received_at": "2026-10-01T16:00:00+03:00", "links": [],
                   "attachments": [{"name": "Davines stock.csv", "path": "fixtures/prices/Davines stock.csv"}], **extras}
        self.engine.ingest(message, NOW)
        classification = {"kind": kind, "actor": "Test reviewer"}
        if kind == "price":
            classification.update({"coverage": coverage or [{"brand": "Davines", "kind": "stock"}], "evidence": evidence or {"attachment_names": ["Davines stock.csv"]}})
        self.engine.classify(mid, classification, NOW)
        return message

    def test_source_catalog_and_merged_rows(self):
        catalog = normalize_sheet(read_json(ROOT / "fixtures" / "supplier-sheet.test.json")["values"], read_json(ROOT / "config" / "catalog-resolutions.json"))
        self.assertEqual(sum(s["enabled"] is True for s in catalog["suppliers"]), 16)
        self.assertEqual(len(catalog["suppliers"][1]["emails"]), 2)
        self.assertEqual(len(catalog["suppliers"][4]["emails"]), 3)
        self.assertIn("contact14@example.invalid", [e["address"] for e in catalog["suppliers"][8]["emails"]])
        self.assertEqual(catalog["suppliers"][15]["brands"], ["Kydra", "Gehwol"])
        self.assertFalse(catalog["suppliers"][0]["enabled"])
        self.assertNotIn("Stock", [b["name"] for b in catalog["brands"]])
        self.assertEqual(catalog["warnings"], [])

    def test_intent_requires_type_and_matches_longest_brand(self):
        self.assertEqual(find_brands("Прайс Wella Color и Тиги", self.engine.catalog), ["Wella Color", "Tigi"])
        self.assertTrue(parse_intent("Запросить прайс Davines", self.engine.catalog)["questions"])
        self.assertEqual(parse_intent("Сток и предзаказ Davines", self.engine.catalog)["kinds"], ["stock", "preorder"])
        self.assertTrue(parse_intent("Сток всех брендов", self.engine.catalog)["all_brands"])

    def test_calendar_weekend_hours_and_new_year(self):
        settings = self.engine.settings
        self.assertEqual(reminder_due("2026-10-02T15:00:00+03:00", settings), timestamp("2026-10-05T15:00:00+03:00"))
        self.assertEqual(reminder_due("2026-12-30T15:00:00+03:00", settings), timestamp("2027-01-08T15:00:00+03:00"))
        self.assertFalse(is_worktime("2026-10-03T12:00:00+03:00", settings))
        self.assertFalse(is_worktime("2026-10-01T19:00:00+03:00", settings))
        self.assertTrue(is_worktime("2026-10-01T09:00:00+03:00", settings))

    def test_freshness_calendar_month_and_uncertain_folder_date(self):
        self.assertEqual(add_month("2027-01-31T15:00:00+03:00"), timestamp("2027-02-28T15:00:00+03:00"))
        price = {"received_at": "2026-09-24T15:00:00+03:00"}
        self.assertEqual(freshness(price, "stock", NOW, self.engine.settings), "expired")
        interval = folder_date("Сентябрь 2026")
        self.assertEqual(freshness(interval, "stock", "2026-10-01T00:00:00+03:00", self.engine.settings), "uncertain")
        self.assertEqual(freshness(interval, "stock", "2026-10-08T00:00:00+03:00", self.engine.settings), "expired")

    def test_grouping_languages_and_fresh_price_exclusion(self):
        self.engine.inventory_import({"prices": [{"id": "fresh", "supplier_id": "test_eu", "brands": ["Davines"], "kinds": ["stock"], "received_at": "2026-09-30T15:00:00+03:00"}]}, NOW)
        request = self.plan(["Davines", "Tigi"], ["stock", "preorder"], ["test_eu", "test_ru"])
        self.assertEqual(len(request["deliveries"]), 2)
        self.assertEqual([d["language"] for d in request["deliveries"]], ["en", "ru"])
        self.assertEqual(len(request["deliveries"][0]["items"]), 3)
        self.assertEqual(len(request["skipped"]), 1)
        self.assertIn("pre-order price list for Davines, Tigi and the current stock list for Tigi", request["deliveries"][0]["body"])
        self.assertEqual(request["deliveries"][0]["recipients"], ["prices@example.invalid", "stock@example.invalid"])

    def test_inventory_refresh_stops_reminders_and_mixed_file_contains_unrequested_brands(self):
        request = self.sent()
        self.engine.inventory_import({"prices": [{"id": "new-list", "supplier_id": "test_eu", "brands": ["Davines"], "kinds": ["stock"], "received_at": "2026-10-01T16:00:00+03:00"}]}, "2026-10-02T15:00:00+03:00")
        self.assertEqual(self.engine.store.read()["requests"][0]["deliveries"][0]["status"], "completed")
        self.assertEqual(self.engine.tick("2026-10-02T15:00:00+03:00", simulate=True)["actions"], [])
        second = self.sent(["Tigi"])
        self.reply(second, coverage=[{"brand": "Tigi", "kind": "stock"}], mid="mixed-extra", body="Davines and Tigi list", evidence={"attachment_names":["Davines stock.csv"], "file_brands":["Davines","Tigi"]})
        self.assertEqual(self.engine.store.read()["jobs"][-1]["status"], "awaiting_route")

    def test_approval_required_and_revision_invalidates(self):
        request = self.plan()
        with self.assertRaises(WorkflowError):
            self.engine.dispatch(request["id"], request["deliveries"][0]["id"], NOW)
        self.assertEqual(self.engine.store.read()["outbox"], [])
        self.engine.approve(request["id"], request["preview_digest"], "Test", NOW)
        changed = self.engine.revise(request["id"], [{"id": request["deliveries"][0]["id"], "recipients": ["changed@example.invalid"], "body": "Please share the current stock list for Davines."}], NOW)
        self.assertIsNone(changed["approval"])
        with self.assertRaises(WorkflowError):
            self.engine.approve(request["id"], request["preview_digest"], "Test", NOW)
        with self.assertRaises(WorkflowError):
            self.engine.dispatch(request["id"], request["deliveries"][0]["id"], NOW)

    def test_dispatch_checks_worktime_and_new_inventory(self):
        request = self.plan()
        self.engine.approve(request["id"], request["preview_digest"], "Test", NOW)
        with self.assertRaises(WorkflowError):
            self.engine.dispatch(request["id"], request["deliveries"][0]["id"], "2026-10-03T15:00:00+03:00")
        self.engine.inventory_import({"prices": [{"id": "new", "supplier_id": "test_eu", "brands": ["Davines"], "kinds": ["stock"], "received_at": NOW}]}, NOW)
        with self.assertRaises(WorkflowError):
            self.engine.dispatch(request["id"], request["deliveries"][0]["id"], NOW)

    def test_six_unique_messages_and_single_exhaustion_notification(self):
        request = self.sent()
        for at in ["2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]:
            self.engine.tick(at + "T15:00:00+03:00", simulate=True)
        state = self.engine.store.read()
        self.assertEqual(len(state["outbox"]), 6)
        self.assertEqual(len({o["subject"] for o in state["outbox"]}), 6)
        self.assertEqual(len({o["body"] for o in state["outbox"]}), 6)
        self.assertEqual(self.engine.tick("2026-10-08T15:00:00+03:00", simulate=True)["actions"], [])
        self.engine.tick("2026-10-09T15:00:00+03:00", simulate=True)
        self.engine.tick("2026-10-12T15:00:00+03:00", simulate=True)
        state = self.engine.store.read()
        self.assertEqual(len(state["outbox"]), 6)
        self.assertEqual(state["requests"][0]["deliveries"][0]["status"], "exhausted")
        self.assertEqual(len([n for n in state["notifications"] if n["key"].startswith("exhausted_")]), 1)

    def test_delayed_worker_sends_one_reminder(self):
        self.sent()
        self.engine.tick("2026-10-08T15:00:00+03:00", simulate=True)
        self.assertEqual(len(self.engine.store.read()["outbox"]), 2)
        self.assertEqual(self.engine.store.read()["requests"][0]["deliveries"][0]["next_action_at"], "2026-10-09T12:00:00+00:00")

    def test_price_reply_closes_and_deduplicates(self):
        request = self.sent()
        message = self.reply(request)
        self.assertTrue(self.engine.ingest(message, NOW)["duplicate"])
        self.assertTrue(self.engine.classify(message["id"], {"kind": "price"}, NOW)["duplicate"])
        self.assertEqual(self.engine.tick("2026-10-02T15:00:00+03:00", simulate=True)["actions"], [])
        state = self.engine.store.read()
        self.assertEqual(len(state["prices"]), 1)
        self.assertEqual(len(state["inbox"]), 1)
        self.assertEqual(state["requests"][0]["status"], "completed")

    def test_partial_reply_keeps_approval_and_reminds_only_missing(self):
        request = self.sent(["Davines", "Tigi"], ["stock", "preorder"])
        self.reply(request, coverage=[{"brand": "Davines", "kind": "stock"}])
        before = self.engine.store.read()["requests"][0]
        self.assertEqual(before["approval"]["digest"], approval_digest(before))
        self.engine.tick("2026-10-02T15:00:00+03:00", simulate=True)
        remaining = self.engine.store.read()["outbox"][-1]["items"]
        self.assertNotIn(("Davines", "stock"), [(i["brand"], i["kind"]) for i in remaining])
        self.assertEqual(len(remaining), 3)

    def test_question_not_a_price_and_notification(self):
        request = self.sent()
        self.reply(request, kind="question", attachments=[], body="Which order volume and target prices?")
        state = self.engine.store.read()
        self.assertEqual(state["requests"][0]["deliveries"][0]["status"], "question")
        self.assertEqual(state["prices"], [])
        self.assertIn("Which order volume", state["notifications"][0]["text"])
        self.engine.tick("2026-10-02T15:00:00+03:00", simulate=True)
        self.assertEqual(len(self.engine.store.read()["outbox"]), 2)

    def test_unreviewed_attachment_or_generic_link_does_not_close(self):
        request = self.sent()
        self.engine.ingest({"id": "candidate", "from": "prices@example.invalid", "delivery_id": request["deliveries"][0]["id"],
                            "received_at": "2026-10-01T16:00:00+03:00", "body": "Our homepage", "links": ["https://example.invalid"], "attachments": []}, NOW)
        self.assertEqual(self.engine.store.read()["requests"][0]["deliveries"][0]["status"], "waiting")
        self.assertEqual(self.engine.tick("2026-10-02T15:00:00+03:00", simulate=True)["actions"][0]["kind"], "review_reply")
        with self.assertRaises(WorkflowError):
            self.engine.classify("candidate", {"kind": "price", "coverage": [{"brand": "Davines", "kind": "stock"}], "evidence": {"links": ["https://other.invalid"]}}, NOW)
        self.engine.classify("candidate", {"kind": "price", "coverage": [{"brand": "Davines", "kind": "stock"}], "evidence": {"links": ["https://example.invalid"]}}, NOW)
        self.assertEqual(self.engine.store.read()["requests"][0]["deliveries"][0]["status"], "completed")

    def test_changed_subject_references_match_and_spoof_does_not(self):
        request = self.sent()
        reference = self.engine.store.read()["outbox"][0]["message_id"]
        good = self.engine.ingest({"id": "good", "from": "prices@example.invalid", "subject": "Brand new subject", "body": "Text",
                                   "received_at": "2026-10-01T16:00:00+03:00", "references": [reference]}, NOW)
        self.assertEqual(good["delivery_id"], request["deliveries"][0]["id"])
        bad = self.engine.ingest({"id": "spoof", "from": "stranger@example.invalid", "subject": "Price",
                                  "received_at": "2026-10-01T16:00:00+03:00", "references": [reference]}, NOW)
        self.assertIsNone(bad["delivery_id"])

    def test_cross_product_price_coverage_is_not_invented(self):
        self.engine.inventory_import({"prices": [{"id": "mixed-types", "supplier_id": "test_eu", "brands": ["Davines", "Tigi"], "kinds": ["stock", "preorder"],
            "coverage": [{"brand": "Davines", "kind": "stock"}, {"brand": "Tigi", "kind": "preorder"}], "received_at": NOW}]}, NOW)
        request = self.plan(["Davines", "Tigi"], ["stock", "preorder"])
        self.assertEqual({(i["brand"], i["kind"]) for i in request["deliveries"][0]["items"]}, {("Tigi", "stock"), ("Davines", "preorder")})

    def test_mixed_file_requires_route_and_original_name_is_saved(self):
        request = self.sent(["Davines", "Tigi"])
        self.reply(request, coverage=[{"brand": "Davines", "kind": "stock"}, {"brand": "Tigi", "kind": "stock"}])
        job = self.engine.store.read()["jobs"][0]
        self.assertEqual(job["status"], "awaiting_route")
        self.assertEqual(Path(job["local_path"]).name, "Davines stock.csv")
        with self.assertRaises(WorkflowError):
            self.engine.complete_job(job["id"], {"simulated": True, "url": job["local_path"]}, NOW)
        self.engine.route_job(job["id"], "chosen_test_folder", NOW)
        result = self.engine.complete_job(job["id"], {"simulated": True, "url": job["local_path"]}, NOW)
        self.assertEqual(result["status"], "completed_simulated")
        self.assertTrue(self.engine.complete_job(job["id"], {"simulated": True, "url": job["local_path"]}, NOW)["duplicate"])

    def test_unsafe_attachment_path_and_unsupported_extension(self):
        request = self.sent()
        base = {"id": "unsafe", "from": "prices@example.invalid", "delivery_id": request["deliveries"][0]["id"], "received_at": "2026-10-01T16:00:00+03:00"}
        with self.assertRaises(WorkflowError):
            self.engine.ingest({**base, "attachments": [{"name": "../../evil.csv", "bytes": b"test"}]}, NOW)
        self.assertEqual(self.engine.store.read()["inbox"], [])
        saved = self.engine.ingest({**base, "attachments": [{"name": "file.exe", "bytes": b"test"}]}, NOW)
        self.assertFalse(saved["attachments"][0]["accepted"])

    def test_test_mode_cannot_use_working_transports(self):
        with self.assertRaises(WorkflowError):
            Engine(self.root, "test", transport=SMTPTransport())
        for capability in ["smtp", "imap", "drive"]:
            with self.assertRaises(WorkflowError):
                network_gate(self.engine.settings, capability)
        settings = read_json(self.root / "config" / "settings.json")
        settings.update({"mode": "live", "external_side_effects_enabled": True})
        atomic_json(self.root / "config" / "settings.json", settings)
        self.assertEqual(Engine(self.root, "test").settings["mode"], "test")
        self.assertFalse(Engine(self.root, "test").settings["external_side_effects_enabled"])

    def test_unknown_transport_outcome_is_not_retried(self):
        class FailingSimulation(SimulatedTransport):
            def send(self, outgoing, settings):
                raise TimeoutError("Outcome unknown")
        self.engine.transport = FailingSimulation()
        request = self.plan()
        self.engine.approve(request["id"], request["preview_digest"], "Test", NOW)
        with self.assertRaises(WorkflowError):
            self.engine.dispatch(request["id"], request["deliveries"][0]["id"], NOW)
        state = self.engine.store.read()
        self.assertEqual(state["outbox"][0]["status"], "outcome_unknown")
        self.assertEqual(self.engine.tick("2026-10-02T15:00:00+03:00", simulate=True)["actions"], [])
        self.assertEqual(len(self.engine.store.read()["outbox"]), 1)

    def test_duplicate_racing_plans_stop_at_dispatch(self):
        first, second = self.plan(), self.plan()
        for request in [first, second]:
            self.engine.approve(request["id"], request["preview_digest"], "Test", NOW)
        self.engine.dispatch(first["id"], first["deliveries"][0]["id"], NOW)
        with self.assertRaises(WorkflowError):
            self.engine.dispatch(second["id"], second["deliveries"][0]["id"], NOW)
        self.assertEqual(len(self.engine.store.read()["outbox"]), 1)

    def test_store_rolls_back_and_does_not_share_environments(self):
        before = self.engine.store.read()
        with self.assertRaises(RuntimeError):
            with self.engine.store.transaction() as state:
                state["requests"].append({"invalid": True})
                raise RuntimeError("Rollback")
        self.assertEqual(self.engine.store.read(), before)
        self.assertFalse((self.engine.store.directory / ".state.lock").exists())
        self.assertEqual(Store(self.root, "live").read()["requests"], [])

    def test_arrival_date_uses_supplied_internaldate_not_email_header(self):
        raw = b"From: Supplier <prices@example.invalid>\r\nSubject: stock\r\nDate: Thu, 1 Jan 1970 00:00:00 +0000\r\nMessage-ID: <id@sender>\r\n\r\nPlease send prices"
        parsed = parse_eml(raw, NOW, "test@example.invalid", "123:42")
        self.assertEqual(parsed["received_at"], timestamp(NOW).isoformat(timespec="seconds"))

    def test_demo_and_html_have_no_external_connections(self):
        with self.engine.store.transaction() as state:
            state["inventory_checks"] = []
        with patch("socket.create_connection", side_effect=AssertionError("Network forbidden")), \
             patch("smtplib.SMTP_SSL", side_effect=AssertionError("SMTP forbidden")), \
             patch("imaplib.IMAP4_SSL", side_effect=AssertionError("IMAP forbidden")):
            result = run_demo(self.root)
        self.assertEqual(result["external_messages_sent"], 0)
        self.assertEqual(result["messages_simulated"], 8)
        self.assertTrue(all(o["status"] == "simulated" for o in self.engine.store.read()["outbox"]))
        render(self.root)
        self.assertTrue((self.root / "index.html").exists())
        self.assertEqual(Store(self.root, "live").read()["requests"], [])


if __name__ == "__main__":
    unittest.main()
