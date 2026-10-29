"""How niya perceives when something happened. The database always keeps exact times; what niya sees
gets coarser as the memory fades, and recent things are said relative to now ("昨天下午")."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

WEEKDAYS = "一二三四五六日"
# Finest to coarsest. "minute" is stored but never shown finer than the hour.
PRECISIONS = ("minute", "hour", "day", "week", "xun", "month", "season", "year", "unknown")


def precision_for_r(r: float, age_days: float) -> str:
    if r >= 0.85:
        return "hour"
    if r >= 0.6:
        return "day"
    if r >= 0.4:
        return "xun"
    return "season" if age_days > 180 else "month"


def coarser(a: str, b: str) -> str:
    return max(a, b, key=PRECISIONS.index)


def part_of_day(hour: int) -> str:
    if hour < 5:
        return "凌晨"
    if hour < 12:
        return "上午" if hour >= 9 else "早上"
    if hour < 13:
        return "中午"
    if hour < 18:
        return "下午"
    return "晚上"


def _day_phrase(d: date, today: date) -> str | None:
    ago = (today - d).days
    if ago == 0:
        return "今天"
    if ago == 1:
        return "昨天"
    if ago == 2:
        return "前天"
    if ago < 0:
        ahead = -ago
        return "明天" if ahead == 1 else "後天" if ahead == 2 else None
    week_start = today.toordinal() - today.weekday()
    if d.toordinal() >= week_start:
        return f"這週{WEEKDAYS[d.weekday()]}"
    if d.toordinal() >= week_start - 7:
        return f"上週{WEEKDAYS[d.weekday()]}"
    return None


def _absolute_day(d: date, today: date) -> str:
    base = f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"
    return base if d.year == today.year else f"{d.year}/{base}"


def _year_prefix(d: date, today: date) -> str:
    return "" if d.year == today.year else f"{d.year} 年 "


def time_label(start: float | None, stored_precision: str, r: float, now: float, tz: ZoneInfo) -> str:
    """A blurred time label for a memory: never finer than stored, coarser as R falls."""
    if start is None or stored_precision == "unknown":
        return "不記得什麼時候"
    when = datetime.fromtimestamp(start, tz)
    today = datetime.fromtimestamp(now, tz).date()
    precision = coarser(coarser(stored_precision, "hour"), precision_for_r(r, (now - start) / 86400))
    d = when.date()
    if precision == "hour":
        phrase = _day_phrase(d, today) or _absolute_day(d, today)
        hour = when.hour % 12 or 12
        return f"{phrase}{part_of_day(when.hour)} {hour} 點"
    if precision == "day":
        return _day_phrase(d, today) or _absolute_day(d, today)
    if precision == "week":
        monday = d.toordinal() - d.weekday()
        this_monday = today.toordinal() - today.weekday()
        if monday == this_monday:
            return "這週"
        if monday == this_monday - 7:
            return "上週"
        start = date.fromordinal(monday)
        return f"{_year_prefix(start, today)}{start.month}/{start.day} 那週"
    if precision == "xun":
        xun = "上旬" if d.day <= 10 else "中旬" if d.day <= 20 else "下旬"
        return f"{_year_prefix(d, today)}{d.month} 月{xun}"
    if precision == "month":
        months_ago = (today.year - d.year) * 12 + today.month - d.month
        if months_ago == 0:
            return "這個月"
        if months_ago == 1:
            return "上個月"
        return f"{_year_prefix(d, today)}{d.month} 月"
    if precision == "season":
        season = "冬天" if d.month in (12, 1, 2) else "春天" if d.month <= 5 else "夏天" if d.month <= 8 else "秋天"
        year = d.year - 1 if d.month in (1, 2) else d.year
        return f"{year} 年{season}"
    years_ago = today.year - d.year
    return "今年" if years_ago == 0 else "去年" if years_ago == 1 else f"{d.year} 年"


def exact_label(start: float | None, end: float | None, tz: ZoneInfo) -> str:
    """Exact time, for deliberate recall."""
    if start is None:
        return "時間不明"
    a = datetime.fromtimestamp(start, tz)
    text = f"{a:%Y-%m-%d} ({WEEKDAYS[a.weekday()]}) {a:%H:%M}"
    if end is None or end - start < 60:
        return text
    b = datetime.fromtimestamp(end, tz)
    return f"{text}–{b:%H:%M}" if b.date() == a.date() else f"{text} – {b:%Y-%m-%d} ({WEEKDAYS[b.weekday()]}) {b:%H:%M}"
