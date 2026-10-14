from __future__ import annotations

import email
import imaplib
import os
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.policy import default
from email.utils import format_datetime, getaddresses
from pathlib import Path

from .common import WorkflowError, iso, timestamp
from .credentials import credentials
from .mail_test import routing


def network_gate(settings, capability):
    if settings.get("mode") not in {"live", "mail_test"} or settings.get("external_side_effects_enabled") is not True:
        raise WorkflowError("Рабочие подключения заблокированы: проект находится в тестовом режиме.")
    if settings["mode"] == "mail_test":
        routing(settings)
        if capability == "drive":
            raise WorkflowError("Почтовый тест сохраняет прайсы только локально; запись на Google Диск выключена.")
    if capability == "smtp" and not settings["mail"]["send_enabled"]:
        raise WorkflowError("Отправка почты отключена в настройках.")
    if capability == "imap" and not settings["mail"]["read_enabled"]:
        raise WorkflowError("Чтение рабочего ящика отключено в настройках.")
    if capability == "drive" and not settings["drive"]["write_enabled"]:
        raise WorkflowError("Запись на Google Диск отключена в настройках.")


def password(settings):
    return credentials(settings)[1]


def make_eml(outgoing, sender):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(outgoing["recipients"])
    message["Subject"] = outgoing["subject"]
    message["Message-ID"] = outgoing["message_id"]
    message["Date"] = format_datetime(timestamp(outgoing["prepared_at"]))
    if outgoing.get("environment") == "mail_test":
        message["X-Procurement-Environment"] = "mail_test"
        message["X-Procurement-Delivery"] = outgoing["delivery_id"]
    if outgoing.get("in_reply_to"):
        message["In-Reply-To"] = outgoing["in_reply_to"]
        message["References"] = outgoing["in_reply_to"]
    message.set_content(outgoing["body"])
    return message


class SimulatedTransport:
    name = "simulation"

    def send(self, outgoing, settings):
        return {"accepted": outgoing["recipients"], "refused": {}, "simulated": True}


class SMTPTransport:
    name = "smtp"

    def send(self, outgoing, settings):
        network_gate(settings, "smtp")
        if settings["mode"] == "mail_test":
            routing(settings, outgoing["recipients"])
        cfg = settings["mail"]
        login, secret = credentials(settings)
        message = make_eml(outgoing, cfg["sender"])
        context = ssl.create_default_context()
        if cfg["smtp_security"] == "ssl":
            connection = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], local_hostname="localhost", timeout=30, context=context)
        elif cfg["smtp_security"] == "starttls":
            connection = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], local_hostname="localhost", timeout=30)
        else:
            raise WorkflowError("Допускается только SMTP с SSL или STARTTLS.")
        with connection:
            if cfg["smtp_security"] == "starttls":
                connection.ehlo()
                connection.starttls(context=context)
                connection.ehlo()
            connection.login(login, secret)
            refused = connection.send_message(message, from_addr=cfg["sender"], to_addrs=outgoing["recipients"])
        return {"accepted": [a for a in outgoing["recipients"] if a not in refused],
                "refused": {a: {"code": value[0], "reason": value[1].decode(errors="replace")} for a, value in refused.items()},
                "simulated": False}


def parse_eml(raw: bytes, received_at, account, source_uid=None):
    import hashlib
    import re
    message = email.message_from_bytes(raw, policy=default)
    _, from_address = getaddresses([str(message.get("From", ""))])[0] if message.get("From") else ("", "")
    body = message.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    links = list(dict.fromkeys(re.findall(r'https?://[^\s<>"\']+', text)))
    attachments = []
    for part in message.iter_attachments():
        name = part.get_filename()
        if name:
            attachments.append({"name": name, "bytes": part.get_payload(decode=True) or b""})
    return {"id": "mail_" + hashlib.sha256((account + ":" + str(source_uid or hashlib.sha256(raw).hexdigest())).encode()).hexdigest()[:20],
            "message_id": str(message.get("Message-ID", "")), "from": from_address.lower(),
            "subject": str(message.get("Subject", "")), "body": text, "received_at": iso(received_at),
            "references": re.findall(r"<[^>]+>", str(message.get("References", "")) + " " + str(message.get("In-Reply-To", ""))),
            "links": links, "attachments": attachments, "account": account, "source_uid": source_uid}


def poll_imap(settings, cursor=None, limit=50):
    """Read-only UID/INTERNALDATE polling; returns bytes for local staging, never marks mail read."""
    network_gate(settings, "imap")
    cfg = settings["mail"]
    login, secret = credentials(settings)
    with imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], timeout=30) as connection:
        connection.login(login, secret)
        status, _ = connection.select(cfg["inbox_folder"], readonly=True)
        if status != "OK":
            raise WorkflowError("Не удалось открыть папку почты.")
        uidvalidity = connection.response("UIDVALIDITY")[1][0].decode()
        if settings["mode"] == "mail_test" and (not cursor or cursor.get("uidvalidity") != uidvalidity):
            # Establish a baseline without fetching old private mailbox contents.
            uidnext = int(connection.response("UIDNEXT")[1][0])
            return [], {"uidvalidity": uidvalidity, "last_uid": uidnext - 1, "baseline": True}
        previous = int(cursor.get("last_uid", 0)) if cursor and cursor.get("uidvalidity") == uidvalidity else 0
        criteria = ["UID", f"{previous + 1}:*"]
        if settings["mode"] == "mail_test":
            criteria += ["FROM", '"' + routing(settings)["recipient"] + '"']
        status, identifiers = connection.uid("search", None, *criteria)
        if status != "OK":
            raise WorkflowError("Ошибка поиска входящих писем.")
        uids = [int(uid) for uid in identifiers[0].split() if int(uid) > previous][:limit]
        result = []
        last_uid = previous
        for uid in uids:
            status, response = connection.uid("fetch", str(uid), "(BODY.PEEK[] INTERNALDATE)")
            if status != "OK":
                raise WorkflowError("Ошибка чтения входящего письма.")
            pair = next((part for part in response if isinstance(part, tuple)), None)
            if not pair:
                raise WorkflowError("IMAP не вернул письмо; курсор не будет продвинут.")
            import re
            metadata = b" ".join(part[0] if isinstance(part, tuple) else part for part in response if isinstance(part, (tuple, bytes)))
            arrival = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
            if not arrival:
                raise WorkflowError("IMAP не вернул дату получения.")
            received = datetime.strptime(arrival[1].decode(), "%d-%b-%Y %H:%M:%S %z")
            parsed = parse_eml(pair[1], received, cfg["sender"], f"{uidvalidity}:{uid}")
            if settings["mode"] != "mail_test" or parsed["from"].lower() == routing(settings)["recipient"]:
                result.append(parsed)
            last_uid = uid
        return result, {"uidvalidity": uidvalidity, "last_uid": last_uid}


def check_mail(settings):
    """Authenticate both protocols; never send mail or fetch message bodies."""
    network_gate(settings, "smtp")
    network_gate(settings, "imap")
    cfg = settings["mail"]
    login, secret = credentials(settings)
    result = {"sender": cfg["sender"], "smtp": {}, "imap": {}, "messages_sent": 0, "message_bodies_read": 0}
    context = ssl.create_default_context()
    try:
        if cfg["smtp_security"] == "ssl":
            connection = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], local_hostname="localhost", timeout=15, context=context)
        elif cfg["smtp_security"] == "starttls":
            connection = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], local_hostname="localhost", timeout=15)
        else:
            raise WorkflowError("Допускается только SMTP с SSL или STARTTLS.")
        with connection:
            if cfg["smtp_security"] == "starttls":
                connection.ehlo()
                connection.starttls(context=context)
                connection.ehlo()
            connection.login(login, secret)
            code, _ = connection.noop()
            result["smtp"] = {"status": "connected" if code == 250 else "error", "code": code}
    except smtplib.SMTPAuthenticationError as error:
        result["smtp"] = {"status": "authentication_failed", "code": error.smtp_code}
    except smtplib.SMTPHeloError as error:
        result["smtp"] = {"status": "greeting_failed", "code": error.smtp_code}
    except Exception as error:
        result["smtp"] = {"status": "connection_failed", "error_type": type(error).__name__}
    cursor = None
    phase = "authentication"
    try:
        with imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], timeout=15) as connection:
            connection.login(login, secret)
            phase = "mailbox_access"
            status, _ = connection.select(cfg["inbox_folder"], readonly=True)
            if status != "OK":
                raise WorkflowError("Не удалось открыть папку почты.")
            uidvalidity = connection.response("UIDVALIDITY")[1][0].decode()
            uidnext = int(connection.response("UIDNEXT")[1][0])
            cursor = {"uidvalidity": uidvalidity, "last_uid": uidnext - 1, "baseline": True}
            result["imap"] = {"status": "connected", "readonly": True}
    except imaplib.IMAP4.error as error:
        result["imap"] = {"status": phase + "_failed"}
        message = str(error).lower()
        if "application" in message or "app password" in message or "пароль приложения" in message:
            result["imap"]["reason"] = "application_password_required"
    except Exception as error:
        result["imap"] = {"status": "connection_failed", "error_type": type(error).__name__}
    result["status"] = "connected" if all(result[p].get("status") == "connected" for p in ["smtp", "imap"]) else "incomplete"
    return result, cursor
