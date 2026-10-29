"""Time words in a message become a search range: 「昨天」「上週二」「三天前」「上個月」「10 月 28 號」「去年」.

Rule-based and conservative: anything it does not understand yields None, never a wrong guess."""

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_NUM = r"[0-9０-９零〇一二兩三四五六七八九十]+"
_WEEK = r"(?:週|周|星期|禮拜)"
_PARTS = [("凌晨", 0, 5), ("早上", 5, 12), ("上午", 5, 12), ("中午", 11, 14), ("下午", 12, 18), ("傍晚", 16, 19), ("晚上", 18, 24)]

Range = tuple[float, float]


def cn_int(text: str) -> int | None:
    text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if text.isdigit():
        return int(text)
    if "十" in text:
        tens, _, ones = text.partition("十")
        value = (_DIGITS.get(tens, 0) if tens else 1) * 10
        if ones:
            if ones not in _DIGITS:
                return None
            value += _DIGITS[ones]
        return value if not tens or tens in _DIGITS else None
    if len(text) == 1 and text in _DIGITS:
        return _DIGITS[text]
    return None


def _day_range(d: date, tz: ZoneInfo, days: int = 1) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(), tzinfo=tz)
    return start, start + timedelta(days=days)


def _month_start(year: int, month: int) -> date:
    year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
    return date(year, month, 1)


def _month_range(year: int, month: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    first = _month_start(year, month)
    nxt = _month_start(first.year, first.month + 1)
    return _day_range(first, tz, (nxt - first).days)


def _narrow(span: tuple[datetime, datetime], text: str) -> tuple[datetime, datetime]:
    start, end = span
    if end - start != timedelta(days=1):
        return span
    for word, a, b in _PARTS:
        if word in text:
            return start + timedelta(hours=a), start + timedelta(hours=b)
    return span


def _find(text: str, today: date, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    for word, ago in (("大前天", 3), ("前天", 2), ("昨天", 1), ("昨晚", 1), ("今天", 0), ("今早", 0), ("今晚", 0), ("大後天", -3), ("後天", -2), ("明天", -1), ("明早", -1)):
        if word in text:
            span = _day_range(today - timedelta(days=ago), tz)
            return _narrow(span, text.replace("昨晚", "晚上").replace("今晚", "晚上").replace("今早", "早上").replace("明早", "早上"))

    monday = today - timedelta(days=today.weekday())
    if m := re.search(rf"(上上|上|下下|下|這|本)?{_WEEK}([一二三四五六日天])", text):
        prefix, day = m.group(1), _WEEKDAY[m.group(2)]
        if prefix in ("這", "本"):
            target = monday + timedelta(days=day)
        elif prefix in ("下", "下下"):
            target = monday + timedelta(weeks=1 if prefix == "下" else 2, days=day)
        elif prefix in ("上", "上上"):
            target = monday - timedelta(weeks=1 if prefix == "上" else 2) + timedelta(days=day)
        else:
            target = monday + timedelta(days=day)
            if target > today:
                target -= timedelta(weeks=1)
        return _narrow(_day_range(target, tz), text)
    if m := re.search(rf"(上上|上|這|本)?(?:個)?{_WEEK}末", text):
        prefix = m.group(1)
        saturday = monday + timedelta(days=5)
        if prefix in ("上", "上上") or (prefix is None and saturday > today):
            saturday -= timedelta(weeks=2 if prefix == "上上" else 1)
        return _day_range(saturday, tz, 2)
    if m := re.search(rf"(上上|上|下|這|本)(?:個)?{_WEEK}", text):
        weeks = {"上上": 2, "上": 1, "下": -1}.get(m.group(1), 0)
        return _day_range(monday - timedelta(weeks=weeks), tz, 7)

    if m := re.search(rf"({_NUM})\s*(天|個?{_WEEK}|週|個月)前", text):
        n = cn_int(m.group(1))
        if n is None:
            return None
        unit = m.group(2)
        if unit == "天":
            return _narrow(_day_range(today - timedelta(days=n), tz), text)
        if unit == "個月":
            return _month_range(today.year, today.month - n, tz)
        return _day_range(monday - timedelta(weeks=n), tz, 7)

    if m := re.search(r"(上上|上|下|這|本)(?:個)?月", text):
        back = {"上上": 2, "上": 1, "下": -1}.get(m.group(1), 0)
        return _month_range(today.year, today.month - back, tz)

    if m := re.search(rf"({_NUM})\s*月\s*({_NUM})\s*[日號号]", text):
        month, day = cn_int(m.group(1)), cn_int(m.group(2))
        try:
            target = date(today.year, month or 0, day or 0)
        except ValueError:
            return None
        if target > today:
            target = target.replace(year=today.year - 1)
        return _narrow(_day_range(target, tz), text)
    if m := re.search(rf"({_NUM})\s*月(?!份?前)", text):
        month = cn_int(m.group(1))
        if month is None or not 1 <= month <= 12:
            return None
        year = today.year if month <= today.month else today.year - 1
        return _month_range(year, month, tz)
    if m := re.search(rf"(?<![月\d])({_NUM})\s*[號号]", text):
        day = cn_int(m.group(1))
        try:
            target = today.replace(day=day or 0)
        except ValueError:
            return None
        if target > today:
            first = _month_start(today.year, today.month - 1)
            try:
                target = first.replace(day=day)
            except ValueError:
                return None
        return _narrow(_day_range(target, tz), text)

    for word, back in (("前年", 2), ("去年", 1), ("今年", 0)):
        if word in text:
            first = date(today.year - back, 1, 1)
            return _day_range(first, tz, (date(first.year + 1, 1, 1) - first).days)
    return None


def parse_time_range(text: str, now: float, tz: ZoneInfo) -> Range | None:
    found = _find(text, datetime.fromtimestamp(now, tz).date(), tz)
    if found is None:
        return None
    start, end = found
    return start.timestamp(), end.timestamp()
