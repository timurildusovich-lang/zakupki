from __future__ import annotations

from .common import WorkflowError


RU_OPENINGS = ["Добрый день!", "Здравствуйте!", "Добрый день.", "Здравствуйте.", "Доброго дня!", "Добрый день!"]
EN_OPENINGS = ["Hi!", "Hello!", "Good day!", "Hello.", "Hi.", "Good day."]
RU_REQUESTS = ["Отправьте, пожалуйста,", "Просим прислать", "Поделитесь, пожалуйста,", "Будем признательны за", "Пришлите, пожалуйста,", "Просим направить"]
EN_REQUESTS = ["Please send", "Could you share", "Please provide", "Could you send", "Please forward", "Would you share"]


def compose(language, items, reminder=0, signature=""):
    if language not in {"ru", "en"}:
        raise WorkflowError("Язык поставщика не указан.")
    if not 0 <= reminder <= 5:
        raise WorkflowError("Допускается пять напоминаний.")
    brands = list(dict.fromkeys(i["brand"] for i in items))
    kinds = {i["kind"] for i in items}
    base = "stock" if kinds == {"stock"} else "price and stock" if kinds == {"stock", "preorder"} else "price"
    subject = base + (" " + brands[0] if len(brands) == 1 else "")
    if reminder:
        suffixes = ["", " — update request", " — current availability", " — price list follow-up", " — latest list requested", " — final follow-up"]
        subject += suffixes[reminder]
    names = ", ".join(brands)
    by_kind = {kind: list(dict.fromkeys(i["brand"] for i in items if i["kind"] == kind)) for kind in kinds}
    asymmetric = len(kinds) == 2 and by_kind["stock"] != by_kind["preorder"]
    if language == "ru":
        phrase = "актуальный сток" if kinds == {"stock"} else "актуальные прайсы предзаказа и стока" if len(kinds) == 2 else "актуальный прайс предзаказа"
        opening = RU_OPENINGS[reminder]
        if asymmetric:
            phrase = "актуальный прайс предзаказа на " + ", ".join(by_kind["preorder"]) + " и актуальный сток на " + ", ".join(by_kind["stock"])
            sentence = f"{RU_REQUESTS[reminder]} {phrase}."
        else:
            sentence = f"{RU_REQUESTS[reminder]} {phrase} на {names}."
        body = opening + "\n\n" + sentence
    else:
        phrase = "the current stock list" if kinds == {"stock"} else "the current pre-order and stock price lists" if len(kinds) == 2 else "the current pre-order price list"
        if asymmetric:
            phrase = "the current pre-order price list for " + ", ".join(by_kind["preorder"]) + " and the current stock list for " + ", ".join(by_kind["stock"])
            sentence = f"{EN_REQUESTS[reminder]} {phrase}."
        else:
            sentence = f"{EN_REQUESTS[reminder]} {phrase} for {names}."
        body = EN_OPENINGS[reminder] + "\n\n" + sentence
    if signature:
        body += "\n\n" + signature
    return {"subject": subject, "body": body, "generator": "local_example_variants", "reminder": reminder}


def validate_draft(draft):
    subject, body = draft.get("subject"), draft.get("body")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 250 or "\n" in subject or "\r" in subject:
        raise WorkflowError("Тема письма пустая, слишком длинная или содержит перевод строки.")
    if not isinstance(body, str) or not body.strip() or len(body) > 10000:
        raise WorkflowError("Текст письма пустой или слишком длинный.")
    return {"subject": subject.strip(), "body": body.strip(), "generator": draft.get("generator", "codex"), "reminder": draft.get("reminder", 0)}
