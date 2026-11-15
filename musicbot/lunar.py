"""Vietnamese lunar calendar (âm lịch), Hồ Ngọc Đức's algorithm at UTC+7.

Vietnam computes new moons at UTC+7 and China at UTC+8, so a few months (e.g. Tết 1985, 2007)
start on different days: don't swap this for a Chinese-calendar package.
"""
from __future__ import annotations

from datetime import date, timedelta, timezone
from math import floor, pi, sin

TZ = 7.0
VN = timezone(timedelta(hours=7))
_DR = pi / 180

MONTHS = ["Giêng", "Hai", "Ba", "Tư", "Năm", "Sáu", "Bảy", "Tám", "Chín", "Mười", "Mười Một", "Chạp"]
HOLIDAYS = {(1, 1): "Tết Nguyên Đán", (15, 1): "Tết Nguyên Tiêu", (15, 7): "Lễ Vu Lan", (15, 8): "Tết Trung Thu"}


def _jd(d: date) -> int:
    return d.toordinal() + 1721425  # Julian day number


def _new_moon(k: int) -> float:
    t = k / 1236.85
    t2, t3 = t * t, t * t * t
    jd1 = 2415020.75933 + 29.53058868 * k + 0.0001178 * t2 - 0.000000155 * t3
    jd1 += 0.00033 * sin((166.56 + 132.87 * t - 0.009173 * t2) * _DR)
    m = 359.2242 + 29.10535608 * k - 0.0000333 * t2 - 0.00000347 * t3
    mpr = 306.0253 + 385.81691806 * k + 0.0107306 * t2 + 0.00001236 * t3
    f = 21.2964 + 390.67050646 * k - 0.0016528 * t2 - 0.00000239 * t3
    c1 = (0.1734 - 0.000393 * t) * sin(m * _DR) + 0.0021 * sin(2 * _DR * m)
    c1 += -0.4068 * sin(mpr * _DR) + 0.0161 * sin(_DR * 2 * mpr) - 0.0004 * sin(_DR * 3 * mpr)
    c1 += 0.0104 * sin(_DR * 2 * f) - 0.0051 * sin(_DR * (m + mpr))
    c1 += -0.0074 * sin(_DR * (m - mpr)) + 0.0004 * sin(_DR * (2 * f + m))
    c1 += -0.0004 * sin(_DR * (2 * f - m)) - 0.0006 * sin(_DR * (2 * f + mpr))
    c1 += 0.0010 * sin(_DR * (2 * f - mpr)) + 0.0005 * sin(_DR * (2 * mpr + m))
    if t < -11:
        dt = 0.001 + 0.000839 * t + 0.0002261 * t2 - 0.00000845 * t3 - 0.000000081 * t * t3
    else:
        dt = -0.000278 + 0.000265 * t + 0.000262 * t2
    return jd1 + c1 - dt


def _new_moon_day(k: int) -> int:
    return floor(_new_moon(k) + 0.5 + TZ / 24)


def _sun_longitude(jdn: float) -> float:
    t = (jdn - 2451545.0) / 36525
    t2 = t * t
    m = 357.52910 + 35999.05030 * t - 0.0001559 * t2 - 0.00000048 * t * t2
    l0 = 280.46645 + 36000.76983 * t + 0.0003032 * t2
    dl = (1.914600 - 0.004817 * t - 0.000014 * t2) * sin(_DR * m)
    dl += (0.019993 - 0.000101 * t) * sin(_DR * 2 * m) + 0.000290 * sin(_DR * 3 * m)
    lon = (l0 + dl) * _DR
    return lon - 2 * pi * floor(lon / (2 * pi))


def _sun_sector(day: int) -> int:
    return floor(_sun_longitude(day - 0.5 - TZ / 24) / pi * 6)


def _month11(year: int) -> int:
    off = _jd(date(year, 12, 31)) - 2415021
    k = floor(off / 29.530588853)
    nm = _new_moon_day(k)
    return _new_moon_day(k - 1) if _sun_sector(nm) >= 9 else nm


def _leap_offset(a11: int) -> int:
    k = floor((a11 - 2415021.076998695) / 29.530588853 + 0.5)
    i, arc = 1, _sun_sector(_new_moon_day(k + 1))
    while True:
        last = arc
        i += 1
        arc = _sun_sector(_new_moon_day(k + i))
        if arc == last or i >= 14:
            return i - 1


def to_lunar(d: date) -> tuple[int, int, int, bool]:
    """Solar date -> (day, month, year, is_leap_month)."""
    day_number = _jd(d)
    k = floor((day_number - 2415021.076998695) / 29.530588853)
    month_start = _new_moon_day(k + 1)
    if month_start > day_number:
        month_start = _new_moon_day(k)
    a11 = _month11(d.year)
    b11 = a11
    if a11 >= month_start:
        year, a11 = d.year, _month11(d.year - 1)
    else:
        year, b11 = d.year + 1, _month11(d.year + 1)
    lday = day_number - month_start + 1
    diff = floor((month_start - a11) / 29)
    leap = False
    month = diff + 11
    if b11 - a11 > 365:
        leap_diff = _leap_offset(a11)
        if diff >= leap_diff:
            month = diff + 10
            leap = diff == leap_diff
    if month > 12:
        month -= 12
    if month >= 11 and diff < 4:
        year -= 1
    return lday, month, year, leap


def describe(day: int, month: int, leap: bool) -> str:
    """'Rằm tháng Bảy' / 'mùng 1 tháng Sáu nhuận' (+ holiday name)."""
    name = ("Rằm" if day == 15 else f"mùng {day}" if day <= 10 else f"ngày {day}") + f" tháng {MONTHS[month - 1]}"
    if leap:
        name += " nhuận"
    holiday = None if leap else HOLIDAYS.get((day, month))
    return f"{name} ({holiday})" if holiday else name
