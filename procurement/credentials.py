from __future__ import annotations

import os
import re
from pathlib import Path

from .common import WorkflowError, within


def read_dotenv(path):
    """Read scalar KEY=VALUE secrets without interpolation, execution, or logging."""
    values = {}
    if not path.is_file():
        return values
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise WorkflowError(f"Некорректная строка .env: {number}. Ожидается KEY=VALUE.")
        if value.startswith(("'", '"')):
            quote = value[0]
            end = value.rfind(quote)
            if end == 0 or (value[end + 1:].strip() and not value[end + 1:].lstrip().startswith("#")):
                raise WorkflowError(f"Некорректные кавычки в .env, строка {number}.")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if key in values:
            raise WorkflowError(f"Повтор имени переменной .env, строка {number}.")
        values[key] = value
    return values


def credentials(settings):
    cfg = settings["mail"]
    root = Path(settings.get("_project_root", Path(__file__).resolve().parents[1])).resolve()
    values = read_dotenv(within(root, root / cfg.get("env_file", ".env")))
    login_key, password_key = cfg.get("login_env"), cfg["password_env"]
    login = (values.get(login_key) or os.environ.get(login_key)) if login_key else cfg["sender"]
    secret = values.get(password_key) or os.environ.get(password_key)
    if not login or login.lower() != cfg["sender"].lower():
        raise WorkflowError((login_key or "Логин почты") + " должен совпадать с настроенным отправителем.")
    if not secret:
        raise WorkflowError("Пароль почтового приложения не настроен в .env или окружении.")
    return login, secret
