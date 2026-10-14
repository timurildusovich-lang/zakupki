from __future__ import annotations

from .common import WorkflowError

# Addresses explicitly authorized by the user for the current email test.
AUTHORIZED_SENDER = "timur-ildusovich@mail.ru"
AUTHORIZED_RECIPIENT = "timurildusovich@gmail.com"


def routing(settings, recipients=None):
    if settings.get("mode") != "mail_test":
        raise WorkflowError("Для почтового теста выберите режим mail_test.")
    if settings["mail"]["sender"].lower() != AUTHORIZED_SENDER:
        raise WorkflowError("Отправитель почтового теста изменился; требуется новая настройка.")
    cfg = settings.get("mail_test", {})
    if cfg.get("recipient", "").lower() != AUTHORIZED_RECIPIENT:
        raise WorkflowError("Получатель почтового теста изменился; требуется новая настройка.")
    if recipients is not None and (len(recipients) != 1 or recipients[0].lower() != AUTHORIZED_RECIPIENT):
        raise WorkflowError("Почтовый тест разрешает только одного получателя: " + AUTHORIZED_RECIPIENT)
    return {"mode": "mail_test", "sender": AUTHORIZED_SENDER, "recipient": AUTHORIZED_RECIPIENT}
