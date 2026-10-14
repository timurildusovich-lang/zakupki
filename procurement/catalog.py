from __future__ import annotations

import re

from .common import WorkflowError, normalize, valid_email


def normalize_sheet(rows, resolutions=None):
    resolutions = resolutions or {}
    if not rows or len(rows[0]) < 7 or rows[0][1:7] != ["Язык общения", "Мы называем", "Английское название", "Nickname", "E-Mail 1 - Value", "Brand"]:
        raise WorkflowError("Заголовки справочника изменились. Сопоставьте поля до импорта.")
    common_brands = [b.strip() for b in str(rows[1][6]).splitlines() if b.strip()]
    stock_marker = resolutions.get("stock_is_marker")
    brands = [b for b in common_brands if not (stock_marker is True and normalize(b) == "stock")]
    suppliers, contacts, previous = [], [], None
    for number, row in enumerate(rows[1:], 2):
        row = list(row) + [None] * (7 - len(row))
        if row[0] and row[2]:
            sid = f"supplier_{int(row[0]):02}"
            language = {"англ": "en", "русс": "ru"}.get(normalize(str(row[1] or "")))
            if sid == "supplier_01":
                language = resolutions.get("alexey_language", language)
            row_brands = brands if 2 <= number <= 20 else [b.strip() for b in str(row[6] or "").splitlines() if b.strip()]
            supplier = {"id": sid, "name": str(row[2]).strip(), "english_name": str(row[3] or "").strip(),
                        "language": language, "enabled": resolutions.get("include_alexey") if sid == "supplier_01" else True,
                        "emails": [{"address": str(row[5]).strip(), "contact": str(row[4] or "").strip(), "source_row": number}],
                        "brands": row_brands, "source_rows": [number], "channels": ["email"]}
            suppliers.append(supplier)
            previous = supplier
        elif row[5]:
            contact = {"address": str(row[5]).strip(), "contact": str(row[4] or "").strip(), "source_row": number}
            if resolutions.get("additional_contacts_belong_to_previous") is True and previous:
                previous["emails"].append(contact)
                previous["source_rows"].append(number)
            else:
                contacts.append({**contact, "candidate_supplier_id": previous["id"] if previous else None})
    if stock_marker is not True:
        brands = [b for b in brands if normalize(b) != "stock"]
        for supplier in suppliers:
            supplier["brands"] = [b for b in supplier["brands"] if normalize(b) != "stock"]
    for supplier in suppliers:
        if any(not valid_email(e["address"]) for e in supplier["emails"]):
            raise WorkflowError(f"Некорректный email в {supplier['name']}.")
    warnings = []
    if any(s["enabled"] is None or (s["enabled"] is not False and not s["language"]) for s in suppliers):
        warnings.append("Уточнить участие и язык Алексея.")
    if contacts:
        warnings.append("Уточнить принадлежность дополнительных контактов.")
    if stock_marker is None:
        warnings.append("Уточнить смысл значения Stock в Brand.")
    return {"schema_version": 1, "brands": [{"name": b, "aliases": []} for b in brands],
            "suppliers": suppliers, "unresolved_contacts": contacts, "warnings": warnings,
            "mapping_rule": "G2:G20 — общие бренды; G21 и G22 — индивидуальные, подтверждено пользователем"}


def find_brands(text, catalog):
    normalized_text = normalize(text)
    patterns = []
    for brand in catalog["brands"]:
        for term in [brand["name"], *brand.get("aliases", [])]:
            patterns.append((normalize(term), brand["name"]))
    spans, result = [], []
    for term, canonical in sorted(patterns, key=lambda p: -len(p[0])):
        pattern = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
        for match in re.finditer(pattern, normalized_text):
            if any(match.start() < end and match.end() > start for start, end in spans):
                continue
            spans.append(match.span())
            if canonical not in result:
                result.append(canonical)
    return result


def parse_intent(text, catalog):
    normalized_text = normalize(text)
    brands = find_brands(text, catalog)
    stock = bool(re.search(r"\b(?:сток\w*|stock)\b", normalized_text))
    preorder = bool(re.search(r"\b(?:предзаказ\w*|pre[- ]?order|заказ\w*)\b", normalized_text))
    kinds = (["stock"] if stock else []) + (["preorder"] if preorder else [])
    if stock and re.search(r"\b(?:оба|both)\b", normalized_text) and not preorder:
        kinds.append("preorder")
    all_brands = bool(re.search(r"(?:все|всех|all)\s+(?:бренд\w*|brands)", normalized_text))
    questions = []
    if not brands and not all_brands:
        questions.append("Какие бренды нужно запросить?")
    if not kinds:
        default = catalog.get("generic_price_kind")
        if default and re.search(r"\b(?:прайс\w*|price\w*)\b", normalized_text):
            kinds = [default]
        else:
            questions.append("Какой прайс нужен: сток, предзаказ или оба?")
    return {"brands": brands, "kinds": kinds, "all_brands": all_brands, "source_text": text, "questions": questions}


def validate_intent(intent, catalog):
    known = {b["name"] for b in catalog["brands"]}
    if intent.get("all_brands"):
        brands = [b["name"] for b in catalog["brands"]]
    else:
        brands = list(dict.fromkeys(intent.get("brands", [])))
    kinds = list(dict.fromkeys(intent.get("kinds", [])))
    if not brands or not set(brands) <= known:
        raise WorkflowError("Нужно указать существующие бренды из справочника.")
    if not kinds or not set(kinds) <= {"stock", "preorder"}:
        raise WorkflowError("Нужно указать stock, preorder или оба вида.")
    selected = intent.get("supplier_ids")
    if selected is not None and not set(selected) <= {s["id"] for s in catalog["suppliers"]}:
        raise WorkflowError("Указан неизвестный поставщик.")
    return {**intent, "brands": brands, "kinds": kinds}
