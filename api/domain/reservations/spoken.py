"""Uhrzeiten so, wie man sie am Telefon sagt: „halb sieben", „sechs Uhr", „viertel nach acht"."""

from datetime import date, datetime, timedelta

HOURS = {
    1: "ein",
    2: "zwei",
    3: "drei",
    4: "vier",
    5: "fünf",
    6: "sechs",
    7: "sieben",
    8: "acht",
    9: "neun",
    10: "zehn",
    11: "elf",
    12: "zwölf",
}


def _hour_word(hour24: int, standalone: bool = False) -> str:
    hour = hour24 % 12 or 12
    if hour == 1 and standalone:
        return "eins"
    return HOURS[hour]


WEEKDAYS = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)
MONTHS = (
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)
COUNTS = {
    1: "eine",
    2: "zwei",
    3: "drei",
    4: "vier",
    5: "fünf",
    6: "sechs",
    7: "sieben",
    8: "acht",
    9: "neun",
    10: "zehn",
    11: "elf",
    12: "zwölf",
}


def spoken_date(dt: datetime, today: date | None = None) -> str:
    """„heute", „morgen" oder „am Dienstag, den 15. September". dt und today in Ortszeit."""
    day = dt.date()
    if today is not None:
        if day == today:
            return "heute"
        if day == today + timedelta(days=1):
            return "morgen"
    return f"am {WEEKDAYS[day.weekday()]}, den {day.day}. {MONTHS[day.month - 1]}"


def spoken_party_size(n: int) -> str:
    if n == 1:
        return "eine Person"
    return f"{COUNTS.get(n, str(n))} Personen"


def spoken_time(dt: datetime) -> str:
    hour, minute = dt.hour, dt.minute
    if minute == 0:
        return f"{_hour_word(hour)} Uhr"
    if minute == 30:
        return f"halb {_hour_word(hour + 1, standalone=True)}"
    if minute == 15:
        return f"viertel nach {_hour_word(hour, standalone=True)}"
    if minute == 45:
        return f"viertel vor {_hour_word(hour + 1, standalone=True)}"
    return f"{hour} Uhr {minute}"
