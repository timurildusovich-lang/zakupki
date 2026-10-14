from __future__ import annotations

import calendar as month_calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .common import WorkflowError, timestamp


def zone(settings):
    return ZoneInfo(settings["calendar"]["timezone"])


def is_workday(moment, settings):
    local = timestamp(moment).astimezone(zone(settings))
    if local.isoweekday() not in settings["calendar"]["weekdays"]:
        return False
    marker = local.strftime("%m-%d")
    for interval in settings["calendar"]["new_year_break"]:
        start, end = interval["start"], interval["end"]
        if (start <= marker <= end) if start <= end else (marker >= start or marker <= end):
            return False
    return True


def work_hours(settings):
    cfg = settings["calendar"]
    if cfg.get("start") is None or cfg.get("end") is None or cfg.get("new_year_break") is None:
        raise WorkflowError("Уточните рабочие часы и новогодний перерыв в config/settings.json.")
    try:
        start_h, start_m = map(int, cfg["start"].split(":"))
        end_h, end_m = map(int, cfg["end"].split(":"))
        start, end = start_h * 60 + start_m, end_h * 60 + end_m
        if not (0 <= start < end <= 1440) or start_m > 59 or end_m > 59:
            raise ValueError
    except (ValueError, TypeError):
        raise WorkflowError("Рабочие часы должны иметь формат HH:MM и начало раньше окончания.") from None
    return start, end


def is_worktime(moment, settings):
    start, end = work_hours(settings)
    local = timestamp(moment).astimezone(zone(settings))
    minutes = local.hour * 60 + local.minute
    return is_workday(moment, settings) and start <= minutes < end


def next_worktime(moment, settings):
    start, end = work_hours(settings)
    local = timestamp(moment).astimezone(zone(settings))
    for _ in range(370):
        minutes = local.hour * 60 + local.minute
        if is_workday(local, settings) and minutes < end:
            if minutes < start:
                local = local.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
            return timestamp(local)
        local = (local + timedelta(days=1)).replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    raise WorkflowError("В календаре не найден рабочий день.")


def reminder_due(sent_at, settings):
    """One eligible calendar day later at the same local clock time; weekends do not count."""
    work_hours(settings)
    local = timestamp(sent_at).astimezone(zone(settings))
    remaining = settings["reminders"]["interval_eligible_days"]
    for _ in range(370):
        local += timedelta(days=1)
        if is_workday(local, settings):
            remaining -= 1
        if remaining == 0:
            return next_worktime(local, settings)
    raise WorkflowError("Не удалось рассчитать дату напоминания.")


def add_month(moment):
    result = timestamp(moment)
    year, month = (result.year + 1, 1) if result.month == 12 else (result.year, result.month + 1)
    day = min(result.day, month_calendar.monthrange(year, month)[1])
    return result.replace(year=year, month=month, day=day)


def expiry(moment, kind, settings):
    if kind == "stock":
        return timestamp(moment) + timedelta(days=settings["freshness"]["stock_days"])
    if kind == "preorder":
        return add_month(moment) if settings["freshness"]["preorder_unit"] == "calendar_month" else timestamp(moment) + timedelta(days=30)
    raise WorkflowError("Неизвестный вид прайса.")


def freshness(price, kind, now, settings):
    earliest = price.get("date_from") or price.get("received_at")
    latest = price.get("date_to") or earliest
    if not earliest:
        return "unknown"
    current = timestamp(now)
    if timestamp(earliest) > current:
        return "unknown"
    if current < expiry(earliest, kind, settings):
        return "fresh"
    if current >= expiry(latest, kind, settings):
        return "expired"
    return "uncertain"


MONTHS = {name: index for index, name in enumerate(
    ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"], 1)}


def folder_date(name, year=None, timezone_name="Europe/Moscow"):
    """Preserve a month interval instead of inventing an exact day."""
    import re
    match = re.fullmatch(r"\s*(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\s*", name)
    tz = ZoneInfo(timezone_name)
    if match:
        day, month, actual_year = map(int, match.groups())
        first = datetime(actual_year, month, day, tzinfo=tz)
        return {"date_from": first.isoformat(), "date_to": (first + timedelta(days=1) - timedelta(seconds=1)).isoformat(), "granularity": "day"}
    match = re.fullmatch(r"\s*([А-Яа-яЁё]+)\s*(\d{4})?\s*", name)
    if match and match[1].casefold() in MONTHS and (match[2] or year):
        actual_year, month = int(match[2] or year), MONTHS[match[1].casefold()]
        first = datetime(actual_year, month, 1, tzinfo=tz)
        last = first.replace(day=month_calendar.monthrange(actual_year, month)[1], hour=23, minute=59, second=59)
        return {"date_from": first.isoformat(), "date_to": last.isoformat(), "granularity": "month"}
    return None
