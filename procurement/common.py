from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


class WorkflowError(ValueError):
    pass


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")).strip()


def timestamp(value: str | datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise WorkflowError("Дата должна содержать часовой пояс.")
    return value.astimezone(timezone.utc)


def iso(value: str | datetime | None = None) -> str:
    return timestamp(value).isoformat(timespec="seconds")


def valid_email(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[^\s@<>,;\r\n]+@[^\s@<>,;\r\n]+\.[^\s@<>,;\r\n]+", value))


def within(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise WorkflowError("Путь должен оставаться внутри проекта.")
    return resolved


def safe_filename(name: str) -> str:
    if not isinstance(name, str) or not name or name in {".", ".."}:
        raise WorkflowError("Пустое или недопустимое имя вложения.")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name[-1] in {".", " "}:
        raise WorkflowError("Имя вложения небезопасно для Windows; требуется ручное сохранение с учётом исходного имени.")
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name):
        raise WorkflowError("Имя вложения зарезервировано Windows.")
    return name
