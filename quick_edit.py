"""Разбор частых сообщений без Gemini: разовая смена аудитории/времени, отмена пар на дату.

Если сообщение не похоже ни на один шаблон или неоднозначно — возвращаем None, и его разберёт Gemini.
"""
import copy
import re
from datetime import date, timedelta

from storage import lessons_on, make_lesson, normalize_time, start_minutes

MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
MONTH_RE = r"(январ[яь]|феврал[яь]|марта?|апрел[яь]|ма[яй]|июн[яь]|июл[яь]|августа?|сентябр[яь]|октябр[яь]|ноябр[яь]|декабр[яь])"
WEEKDAYS = [
    (r"понедельник", 0), (r"вторник", 1), (r"сред[уаые]", 2), (r"четверг", 3),
    (r"пятниц[уаые]", 4), (r"суббот[уаые]", 5),
]
DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
CYR2LAT = str.maketrans({"А": "A", "В": "B", "С": "C", "Е": "E", "К": "K", "М": "M", "Н": "H",
                         "О": "O", "Р": "P", "Т": "T", "Х": "X", "а": "a", "в": "b", "с": "c", "е": "e"})

CANCEL_RE = re.compile(r"отмен|не будет|не состоит|не проводит|переносится на другой день", re.I)
ROOM_RE = re.compile(r"аудитори|кабинет|ауд\.|\bкаб\.|актов|онлайн|online|zoom|зум", re.I)
MOVE_RE = re.compile(r"перенес|перенос|перенёс|сдвиг|начн[её]тся в|будет в \d", re.I)
ROOM_CODE_RE = re.compile(r"\b([ABАВCС]\s?-?\s?\d{3}[a-zа-я]?)\b")


def _year_for(day: int, month: int, today: date) -> date | None:
    try:
        d = date(today.year, month, day)
    except ValueError:
        return None
    if d < today - timedelta(days=7):
        try:
            d = date(today.year + 1, month, day)
        except ValueError:
            return None
    if d > today + timedelta(days=120):
        return None  # слишком далеко — скорее всего это не дата (или ошибка), пусть разбирает Gemini
    return d


def parse_date(text: str, today: date) -> tuple[date | None, tuple[int, int] | None]:
    """Дата из текста и позиция найденного фрагмента (чтобы не спутать «13.10» с временем)."""
    t = text.lower()
    m = re.search(r"\b(\d{1,2})\s+" + MONTH_RE, t)
    if m:
        word = m.group(2)
        month = next(v for k, v in MONTHS.items() if word.startswith(k))
        return _year_for(int(m.group(1)), month, today), m.span()
    for m in re.finditer(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b", t):
        dd, mm = int(m.group(1)), int(m.group(2))
        before = t[max(0, m.start() - 3):m.start()]
        if m.group(3):
            y = int(m.group(3))
            y = y + 2000 if y < 100 else y
            try:
                return date(y, mm, dd), m.span()
            except ValueError:
                continue
        # «в 13.10» — это время, а «13.10 пары не будет» — дата
        if 1 <= mm <= 12 and 1 <= dd <= 31 and not re.search(r"\b(в|с|до|и)\s$", before):
            return _year_for(dd, mm, today), m.span()
    if "послезавтра" in t:
        return today + timedelta(days=2), None
    if "завтра" in t:
        return today + timedelta(days=1), None
    if "сегодня" in t:
        return today, None
    for pat, wd in WEEKDAYS:
        m = re.search(r"\b" + pat, t)
        if m:
            delta = (wd - today.weekday()) % 7
            if re.search(r"следующ\w*\s+" + pat, t) and delta == 0:
                delta = 7
            return today + timedelta(days=delta), None
    return None, None


def parse_times(text: str) -> list[int]:
    """Времена начала пар, упомянутые в тексте (минуты от полуночи)."""
    out = []
    for m in re.finditer(r"\b(\d{1,2})[.:](\d{2})\b", text):
        h, mi = int(m.group(1)), int(m.group(2))
        if 7 <= h <= 21 and mi < 60:
            # пропускаем то, что похоже на дату «27.10» (день > 21 или после него идёт месяц/год)
            out.append(h * 60 + mi)
    return out


def _base(s: str) -> str:
    return re.sub(r"\(.*?\)", "", s).strip().lower()


def match_subjects(text: str, lessons: list[dict]) -> set[str]:
    t = text.lower()
    names = {_base(x["s"]) for x in lessons}
    found = {n for n in names if n and n in t}
    # в кавычках
    for q in re.findall(r"[«\"“']([^»\"”']{3,})[»\"”']", text):
        qn = q.strip().lower()
        found |= {n for n in names if qn in n or n in qn}
    return found


def parse_room(text: str) -> str | None:
    t = text.strip().rstrip(".!")
    low = t.lower()
    if re.search(r"онлайн|online|zoom|зум", low) and not re.search(r"\bна\s+(?!онлайн|online|zoom|зум)\S", low.split("аудитори")[-1]):
        return "Online"
    # «… с B106 на актовый зал» / «… перенесена в B201»
    m = None
    for m in re.finditer(r"\bна\s+([^,;:!?\n]+)", t):
        pass
    if m:
        cand = m.group(1).strip().rstrip(".")
        if not re.match(r"^\d{1,2}[.:]\d{2}", cand) and not re.match(r"^(другой|другую|следующ)", cand.lower()):
            return _clean_room(cand)
    codes = ROOM_CODE_RE.findall(t)
    if codes:
        return _clean_room(codes[-1])
    return None


def _clean_room(r: str) -> str:
    r = r.strip().strip("«»\"' ")
    r = re.sub(r"\s+", " ", r)
    if ROOM_CODE_RE.fullmatch(r) or re.fullmatch(r"[ABАВCС]\s?-?\s?\d{3}[a-zа-я]?", r):
        r = r.translate(CYR2LAT).replace(" ", "").replace("-", "").upper()
    else:
        r = r[0].upper() + r[1:]
    return r[:40]


def _fmt_d(d: date) -> str:
    return d.strftime("%d.%m")


def quick_edit(text: str, current: list[dict], today: date) -> tuple[list[dict], str] | None:
    """Возвращает (новое расписание, описание) или None, если не уверены."""
    if not text or len(text) > 400:
        return None
    is_cancel = bool(CANCEL_RE.search(text))
    is_room = bool(ROOM_RE.search(text) or ROOM_CODE_RE.search(text)) and not is_cancel
    if MOVE_RE.search(text) and not is_room:
        return None  # переносы на другое время/день — пусть разбирает Gemini
    if not (is_cancel or is_room):
        return None

    day, span = parse_date(text, today)
    if day is None or day < today or day.weekday() == 6:
        return None

    on_day = [x for x in lessons_on(current, day) if not x.get("_cancel")]
    if not on_day:
        return None

    subjects = match_subjects(text, current)
    times = parse_times(text[:span[0]] + " " + text[span[1]:] if span else text)

    targets = on_day
    if subjects:
        targets = [x for x in targets if _base(x["s"]) in subjects]
    if times:
        by_time = [x for x in targets if start_minutes(x["t"]) in times]
        if by_time:
            targets = by_time
        elif subjects:
            pass  # время не совпало, но предмет назван — меняем этот предмет в этот день
        else:
            return None
    if not subjects and not times and not (is_cancel and re.search(r"пар\w* не будет|занятий не будет|все пары|выходн", text, re.I)):
        return None
    if not targets:
        return None

    new_room = None
    if is_room:
        new_room = parse_room(text)
        if not new_room:
            return None

    new = copy.deepcopy(current)
    by_id = {x["id"]: x for x in new}
    iso = day.isoformat()
    changed = []
    for t in targets:
        x = by_id.get(t["id"])
        if x is None:
            continue
        if x.get("date"):  # разовая пара — меняем её саму
            if is_cancel:
                new.remove(x)
            else:
                x["r"] = new_room
        else:
            once = [o for o in x.get("once", []) if o["date"] != iso]
            prev = next((o for o in x.get("once", []) if o["date"] == iso), {})
            entry = {k: v for k, v in prev.items() if k not in ("cancel",)}
            entry["date"] = iso
            if is_cancel:
                entry = {"date": iso, "cancel": True}
            else:
                entry["r"] = new_room
            once.append(entry)
            x["once"] = sorted(once, key=lambda o: o["date"])
        changed.append(t)

    if not changed:
        return None
    what = ", ".join(f"{x['t'].split(' - ')[0]} {x['s']}" for x in changed)
    when = f"{_fmt_d(day)} ({DAYS_SHORT[day.weekday()]})"
    summary = f"{when}: {what} — " + ("отмена" if is_cancel else f"аудитория «{new_room}»")
    missing = [m for m in times if m not in {start_minutes(x['t']) for x in changed}]
    if missing:
        summary += ". В " + ", ".join(f"{m // 60}:{m % 60:02d}" for m in missing) + " у тебя в этот день такой пары нет — не трогаю"
    return new, summary
