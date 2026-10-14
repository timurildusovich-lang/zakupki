from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from procurement.adapters import SMTPTransport, check_mail, network_gate, poll_imap
from procurement.common import WorkflowError, atomic_json, read_json
from procurement.credentials import credentials, read_dotenv
from procurement.engine import Engine, approval_digest
from procurement.mail_test import AUTHORIZED_RECIPIENT, AUTHORIZED_SENDER
from procurement.report import render
from procurement.store import Store

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-10-14T15:00:00+03:00"
FAKE_PASSWORD = "not-a-real-password#with$literal"


class MailTestTests(unittest.TestCase):
    def setUp(self):
        stage = ROOT / ".staging/tests"
        stage.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="mail-test-", dir=stage)
        self.root = Path(self.temporary.name)
        for folder in ["config", "fixtures", "web"]:
            shutil.copytree(ROOT / folder, self.root / folder)
        (self.root / "data").mkdir()
        shutil.copy(ROOT / "data/catalog.test.json", self.root / "data/catalog.json")
        shutil.copy(ROOT / "data/catalog.test.json", self.root / "data/catalog.test.json")
        (self.root / ".env").write_text(f'EMAIL_LOGIN="{AUTHORIZED_SENDER}"\nEMAIL_PASSWORD="{FAKE_PASSWORD}"\n', encoding="utf-8")
        self.settings = read_json(self.root / "config/settings.json")
        self.settings.update(mode="mail_test", external_side_effects_enabled=True)
        self.settings["mail"].update(sender=AUTHORIZED_SENDER, read_enabled=True, send_enabled=True, env_file=".env", login_env="EMAIL_LOGIN", password_env="EMAIL_PASSWORD")
        self.settings["mail_test"] = {"recipient": AUTHORIZED_RECIPIENT}
        self.engine = Engine(self.root, "mail_test", settings=self.settings, catalog=read_json(self.root / "data/catalog.test.json"))
        atomic_json(self.engine.store.directory / "connection.json", {"status":"connected", "sender":AUTHORIZED_SENDER, "test_fixture":True})

    def tearDown(self):
        self.temporary.cleanup()

    def plan(self):
        return self.engine.prepare({"brands":["Davines"], "kinds":["stock"], "supplier_ids":["test_eu","test_ru"]}, "Test reviewer", NOW)

    def test_dotenv_is_literal_and_login_must_match_sender(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(credentials(self.engine.settings), (AUTHORIZED_SENDER, FAKE_PASSWORD))
        self.assertEqual(read_dotenv(self.root / ".env")["EMAIL_PASSWORD"], FAKE_PASSWORD)
        (self.root / ".env").write_text("EMAIL_LOGIN=wrong@example.invalid\nEMAIL_PASSWORD=secret\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(WorkflowError):
            credentials(self.engine.settings)

    def test_routing_isolated_and_supplier_addresses_remain_reference_only(self):
        request = self.plan()
        self.assertFalse(request["blockers"])
        self.assertEqual(len(request["deliveries"]), 2)
        self.assertTrue(all(d["recipients"] == [AUTHORIZED_RECIPIENT] for d in request["deliveries"]))
        self.assertEqual(request["deliveries"][0]["intended_recipients"], ["prices@example.invalid", "stock@example.invalid"])
        self.assertEqual(Store(self.root, "test").read()["requests"], [])
        self.assertEqual(Store(self.root, "live").read()["requests"], [])
        with self.assertRaises(WorkflowError):
            self.engine.revise(request["id"], [{"id":request["deliveries"][0]["id"], "recipients":["supplier@example.invalid"]}], NOW)

    def test_both_initial_and_reminder_only_reach_authorized_recipient(self):
        request = self.plan()
        delivery = request["deliveries"][0]
        with patch("smtplib.SMTP_SSL") as smtp:
            connection = smtp.return_value
            connection.send_message.return_value = {}
            with self.assertRaises(WorkflowError):
                self.engine.dispatch(request["id"], delivery["id"], NOW)
            smtp.assert_not_called()
            self.engine.approve(request["id"], request["preview_digest"], "Test approval", NOW)
            first = self.engine.dispatch(request["id"], delivery["id"], NOW)
            reminder = self.engine.dispatch(request["id"], delivery["id"], "2026-10-15T15:00:00+03:00", reminder=True,
                                           draft={"subject":"stock Davines — reminder", "body":"Hello! Could you share the current stock list for Davines?"})
            self.assertEqual([first["status"],reminder["status"]], ["sent","sent"])
            for call in connection.send_message.call_args_list:
                self.assertEqual(call.kwargs["to_addrs"], [AUTHORIZED_RECIPIENT])
                self.assertEqual(call.kwargs["from_addr"], AUTHORIZED_SENDER)
                mime = call.args[0]
                self.assertEqual(str(mime["To"]), AUTHORIZED_RECIPIENT)
                self.assertIsNone(mime["Cc"])
                self.assertIsNone(mime["Bcc"])
                self.assertEqual(str(mime["X-Procurement-Environment"]), "mail_test")
                self.assertNotIn("prices@example.invalid", mime.as_string())
            connection.login.assert_called_with(AUTHORIZED_SENDER, FAKE_PASSWORD)

    def test_tampered_history_or_direct_transport_cannot_bypass_address_guard(self):
        request = self.plan()
        delivery = request["deliveries"][0]
        self.engine.approve(request["id"], request["preview_digest"], "Test", NOW)
        with self.engine.store.transaction() as state:
            saved = state["requests"][0]
            saved["deliveries"][0]["recipients"] = ["supplier@example.invalid"]
            saved["approval"]["digest"] = approval_digest(saved)
        with patch("smtplib.SMTP_SSL") as smtp:
            with self.assertRaises(WorkflowError):
                self.engine.dispatch(request["id"], delivery["id"], NOW)
            with self.assertRaises(WorkflowError):
                SMTPTransport().send({"recipients":["supplier@example.invalid"]}, self.engine.settings)
            smtp.assert_not_called()
        self.assertEqual(self.engine.store.read()["outbox"], [])
        with self.assertRaises(WorkflowError):
            network_gate(self.engine.settings, "drive")

    def test_connection_probe_does_not_send_or_read_messages(self):
        with patch("smtplib.SMTP_SSL") as smtp, patch("imaplib.IMAP4_SSL") as imap:
            smtp_conn = smtp.return_value
            smtp_conn.noop.return_value = (250,b"OK")
            imap_conn = imap.return_value.__enter__.return_value
            imap_conn.select.return_value = ("OK",[b"5"])
            imap_conn.response.side_effect = lambda key: (key,[b"123" if key=="UIDVALIDITY" else b"6"])
            result, cursor = check_mail(self.engine.settings)
            self.assertEqual(result["status"], "connected")
            self.assertEqual(cursor["last_uid"], 5)
            smtp_conn.send_message.assert_not_called()
            smtp_conn.sendmail.assert_not_called()
            imap_conn.uid.assert_not_called()
            imap_conn.select.assert_called_with("INBOX", readonly=True)
            self.assertNotIn(FAKE_PASSWORD, json.dumps(result))

    def test_poll_baselines_old_mail_and_filters_gmail(self):
        with patch("imaplib.IMAP4_SSL") as imap:
            connection = imap.return_value.__enter__.return_value
            connection.select.return_value = ("OK",[b"5"])
            connection.response.side_effect = lambda key: (key,[b"123" if key=="UIDVALIDITY" else b"6"])
            messages, baseline = poll_imap(self.engine.settings)
            self.assertEqual(messages, [])
            connection.uid.assert_not_called()
            connection.uid.return_value = ("OK",[b""])
            poll_imap(self.engine.settings, baseline)
            connection.uid.assert_called_with("search", None, "UID", "6:*", "FROM", '"'+AUTHORIZED_RECIPIENT+'"')

    def test_imap_internaldate_after_body_literal_is_used(self):
        raw = ("From: " + AUTHORIZED_RECIPIENT + "\r\nSubject: Test reply\r\n\r\nTest received").encode()
        with patch("imaplib.IMAP4_SSL") as imap:
            connection = imap.return_value.__enter__.return_value
            connection.select.return_value = ("OK",[b"1"])
            connection.response.return_value = ("UIDVALIDITY",[b"123"])
            connection.uid.side_effect = [("OK",[b"6"]), ("OK",[(b"1 (UID 6 BODY[] {100}",raw),b' INTERNALDATE "14-Oct-2026 15:30:00 +0300")'])]
            messages,cursor = poll_imap(self.engine.settings,{"uidvalidity":"123","last_uid":5})
            self.assertEqual(messages[0]["received_at"],"2026-10-14T12:30:00+00:00")
            self.assertEqual(cursor["last_uid"],6)

    def test_live_uses_production_account_and_original_supplier_recipients(self):
        settings=copy.deepcopy(self.settings)
        settings["mode"]="live"
        settings["mail"].update(sender="tam_sabis4@mail.ru",login_env="PRODUCTION_EMAIL_LOGIN",password_env="PRODUCTION_EMAIL_PASSWORD")
        with (self.root/".env").open("a",encoding="utf-8") as stream:
            stream.write("PRODUCTION_EMAIL_LOGIN=tam_sabis4@mail.ru\nPRODUCTION_EMAIL_PASSWORD="+FAKE_PASSWORD+"\n")
        catalog=copy.deepcopy(self.engine.catalog)
        catalog["synced_at"]=NOW
        live=Engine(self.root,"live",settings=settings,catalog=catalog)
        live.inventory_import({"checks":[{"supplier_id":s["id"],"brand":"Davines","checked_at":NOW,"complete":True,"sources":["test_fixture"]}
                                         for s in catalog["suppliers"] if "Davines" in s["brands"]]},NOW)
        request=live.prepare({"brands":["Davines"],"kinds":["stock"],"supplier_ids":["test_eu","test_ru"]},"Test",NOW)
        self.assertEqual(request["deliveries"][0]["recipients"],["prices@example.invalid","stock@example.invalid"])
        self.assertEqual(request["deliveries"][1]["recipients"],["supplier@example.invalid"])
        self.assertNotIn("routing_policy",request)
        live.approve(request["id"],request["preview_digest"],"Test approval",NOW)
        with patch("smtplib.SMTP_SSL") as smtp:
            with self.assertRaises(WorkflowError):
                live.dispatch(request["id"],request["deliveries"][0]["id"],NOW)
            smtp.assert_not_called()
            atomic_json(live.store.directory/"connection.json",{"status":"connected","sender":"tam_sabis4@mail.ru","test_fixture":True})
            smtp.return_value.send_message.return_value={}
            live.dispatch(request["id"],request["deliveries"][0]["id"],NOW)
            smtp.return_value.login.assert_called_with("tam_sabis4@mail.ru",FAKE_PASSWORD)
            self.assertEqual(smtp.return_value.send_message.call_args.kwargs["to_addrs"],["prices@example.invalid","stock@example.invalid"])

    def test_offline_simulation_stays_offline_when_mail_test_is_enabled(self):
        offline = Engine(self.root, "test", settings=self.settings)
        with patch("smtplib.SMTP_SSL", side_effect=AssertionError("No network")):
            for capability in ["smtp","imap","drive"]:
                with self.assertRaises(WorkflowError):
                    network_gate(offline.settings, capability)
            render(self.root)
        html = (self.root / "index.html").read_text(encoding="utf-8")
        self.assertNotIn(FAKE_PASSWORD, html)
        self.assertIn('"mail_test"', html)

    def test_failed_connection_blocks_sending_before_transport(self):
        request = self.plan()
        self.engine.approve(request["id"], request["preview_digest"], "Test", NOW)
        atomic_json(self.engine.store.directory / "connection.json", {"status":"incomplete"})
        with patch("smtplib.SMTP_SSL") as smtp:
            with self.assertRaises(WorkflowError):
                self.engine.dispatch(request["id"], request["deliveries"][0]["id"], NOW)
            smtp.assert_not_called()
        self.assertEqual(self.engine.store.read()["outbox"], [])

    def test_gmail_reply_matches_thread_and_saves_price_locally(self):
        request = self.plan()
        delivery = request["deliveries"][0]
        self.engine.approve(request["id"], request["preview_digest"], "Test", NOW)
        with patch("smtplib.SMTP_SSL") as smtp:
            smtp.return_value.send_message.return_value = {}
            outgoing = self.engine.dispatch(request["id"], delivery["id"], NOW)
        reply = {"id":"gmail-reply", "from":AUTHORIZED_RECIPIENT, "subject":"Re: stock Davines", "body":"Attached is the test stock list.",
                 "received_at":"2026-10-14T16:00:00+03:00", "references":[outgoing["message_id"]], "links":[],
                 "attachments":[{"name":"Test stock.csv", "bytes":b"Brand,Name,Stock\nDavines,Test item,10\n"}]}
        incoming = self.engine.ingest(reply, "2026-10-14T16:01:00+03:00")
        self.assertEqual(incoming["delivery_id"], delivery["id"])
        self.engine.classify(reply["id"], {"kind":"price", "coverage":[{"brand":"Davines","kind":"stock"}], "evidence":{"attachment_names":["Test stock.csv"], "file_brands":["Davines"]}}, "2026-10-14T16:01:00+03:00")
        state = self.engine.store.read()
        self.assertEqual(state["requests"][0]["deliveries"][0]["status"], "completed")
        self.assertTrue(state["prices"][0]["url"].startswith("data/mail_test/attachments/"))
        self.assertEqual(state["prices"][0]["storage_status"], "stored_local")
        self.assertEqual(state["jobs"], [])
        self.assertEqual((self.root / state["prices"][0]["url"]).name, "Test stock.csv")

    def test_diagnostic_is_one_email_without_reminders_and_reply_verifies_round_trip(self):
        request = self.engine.prepare_mail_check("Explicit test request", NOW)
        self.assertEqual(self.engine.prepare_mail_check("Repeat prepare", NOW)["id"], request["id"])
        self.engine.approve(request["id"], request["preview_digest"], "Explicit user authorization", NOW)
        delivery = request["deliveries"][0]
        with patch("smtplib.SMTP_SSL") as smtp:
            smtp.return_value.send_message.return_value = {}
            outgoing = self.engine.dispatch(request["id"], delivery["id"], NOW)
        saved = self.engine.store.read()["requests"][0]["deliveries"][0]
        self.assertIsNone(saved["next_action_at"])
        self.assertEqual(self.engine.tick("2026-10-15T15:00:00+03:00")["actions"], [])
        reply = self.engine.ingest({"id":"diagnostic-answer", "from":AUTHORIZED_RECIPIENT, "received_at":"2026-10-14T16:00:00+03:00",
                                   "references":[outgoing["message_id"]], "body":"Test received", "attachments":[], "links":[]}, "2026-10-14T16:01:00+03:00")
        self.assertEqual(reply["classification"], "diagnostic_reply")
        state = self.engine.store.read()
        self.assertEqual(state["requests"][0]["status"], "completed")
        self.assertEqual(state["prices"], [])
        self.assertEqual(state["events"][-2]["kind"], "mail_round_trip_verified")


if __name__ == "__main__":
    unittest.main()
