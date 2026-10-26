"""Домашние задания: логика без Telegram (разбор, сроки, форматирование) — всё локально, без Gemini."""
import re
import uuid
from datetime import date, timedelta
from html import escape

from quick_edit import MONTH_RE, parse_date
from storage import lessons_on

DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
PREFIX_RE = re.compile(r"^\s*(?:дз|д/з|д\.з\.|домашка|домашнее задание|домашнее|задали|задание|hw)\b[\s:,\-—]*", re.I)
NEXT_LESSON_RE = re.compile(r"\b(?:к|на|до)\s+(?:следующ\w*|след\.?)\s+(?:пар\w*|заняти\w*|урок\w*)", re.I)
DUE_PHRASE_RE = re.compile(
    r"\b(?:к|до|на|сдать|сдача|дедлайн|срок)\s+(?:(?:следующ\w*|след\.?)\s+)?"
    r"(?:\d{1,2}(?:\.\d{1,2}(?:\.\d{2,4})?|\s+" + MONTH_RE + r")|понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|суббот\w*|"
    r"завтра\w*|послезавтра|сегодня)",
    re.I,
)


def base(s: str) -> str:
    return re.sub(r"\(.*?\)", "", s).strip()


def subjects(lessons: list[dict]) -> list[str]:
    seen, out = set(), []
    for x in sorted(lessons, key=lambda x: base(x["s"]).lower()):
        b = base(x["s"])
        if b and b.lower() not in seen:
            seen.add(b.lower())
            out.append(b)
    return out


def _aliases(name: str) -> list[str]:
    low = name.lower()
    words = re.findall(r"[a-zа-яё0-9]+", low)
    al = {low}
    if len(words) >= 2:
        al.add("".join(w[0] for w in words))           # machine learning -> ml
    if words and len(words[0]) >= 4:
        al.add(words[0])                               # machine, critical, software…
    if "/" in low:
        al.add(low.split()[0])                         # ui/ux
    return sorted(al, key=len, reverse=True)


def find_subject(text: str, lessons: list[dict]) -> tuple[str | None, str]:
    """Ищет предмет в тексте. Возвращает (предмет, текст без упоминания предмета)."""
    best = None
    for name in subjects(lessons):
        for a in _aliases(name):
            m = re.search(r"(?<![a-zа-яё0-9])" + re.escape(a) + r"(?![a-zа-яё0-9])", text, re.I)
            if m and (best is None or len(a) > best[1]):
                best = (name, len(a), m.span())
            if m:
                break
    if not best:
        return None, text
    s, e = best[2]
    rest = (text[:s] + " " + text[e:])
    rest = re.sub(r"^\s*(?:по|на|для)\s+", "", rest.strip(), flags=re.I)
    return best[0], rest


def next_lesson_date(lessons: list[dict], subject: str, today: date) -> date | None:
    for i in range(1, 22):
        d = today + timedelta(days=i)
        if any(base(x["s"]).lower() == subject.lower() and not x.get("_cancel") for x in lessons_on(lessons, d)):
            return d
    return None


def parse_quick(text: str, lessons: list[dict], today: date) -> dict | None:
    """«дз ML практическая 4 к четвергу» -> {s, text, due}. None — если это не домашка."""
    m = PREFIX_RE.match(text)
    if not m:
        return None
    rest = text[m.end():].strip()
    subj, rest = find_subject(rest, lessons)

    due = None
    if NEXT_LESSON_RE.search(rest):
        rest = NEXT_LESSON_RE.sub(" ", rest)
        due = "next"
    else:
        found = False
        for dm in DUE_PHRASE_RE.finditer(rest):
            d, _ = parse_date(dm.group(0), today)
            if d and d >= today:
                due = d.isoformat()
                rest = rest[:dm.start()] + " " + rest[dm.end():]
                found = True
                break
        if not found:
            d, _ = parse_date(rest, today)
            if d and d >= today:
                due = d.isoformat()
    if due in (None, "next") and subj:
        nd = next_lesson_date(lessons, subj, today)
        due = nd.isoformat() if nd else None
    elif due == "next":
        due = None

    task = re.sub(r"\s{2,}", " ", rest).strip(" ,.;:-—")
    if task:
        task = task[0].upper() + task[1:]
    return {"s": subj, "text": task, "due": due}


def make(s: str | None, text: str, due: str | None, today: date) -> dict:
    return {"id": uuid.uuid4().hex[:8], "s": s or "", "text": text.strip(), "due": due,
            "done": False, "created": today.isoformat()}


def prune(hw: list[dict], today: date) -> list[dict]:
    """Выполненные задания старше 14 дней удаляем."""
    cutoff = (today - timedelta(days=14)).isoformat()
    return [h for h in hw if not (h.get("done") and (h.get("done_at") or h.get("due") or "9999") < cutoff)]


def sort_key(h: dict):
    return (h.get("due") or "9999-99-99", h.get("s", "").lower())


def due_label(due: str | None, today: date) -> tuple[str, str]:
    """(эмодзи-маркер, подпись срока)."""
    if not due:
        return "⚪", "без срока"
    d = date.fromisoformat(due)
    delta = (d - today).days
    tail = f"{d.strftime('%d.%m')} ({DAYS_SHORT[d.weekday()]})"
    if delta < 0:
        return "⚠️", f"просрочено · {tail}"
    if delta == 0:
        return "🔴", f"сегодня · {tail}"
    if delta == 1:
        return "🟠", f"завтра · {tail}"
    if delta <= 3:
        return "🟡", f"через {delta} дн. · {tail}"
    return "🟢", tail


def fmt_item(h: dict, today: date) -> str:
    mark, lbl = due_label(h.get("due"), today)
    subj = f"<b>{escape(h['s'])}</b>" if h.get("s") else "<b>Без предмета</b>"
    if h.get("done"):
        return f"✅ <s>{subj} — {escape(h['text'])}</s>"
    return f"{mark} {subj} · <i>{lbl}</i>\n      {escape(h['text']) or '—'}"


def fmt_list(hw: list[dict], today: date, show_done: bool = False) -> str:
    items = [h for h in hw if bool(h.get("done")) == show_done]
    items.sort(key=sort_key, reverse=show_done)
    if show_done:
        if not items:
            return "📋 <b>Выполненные</b>\n\nПока пусто."
        return "📋 <b>Выполненные</b> (последние 14 дней)\n\n" + "\n".join(fmt_item(h, today) for h in items[:30])
    if not items:
        return ("📝 <b>Домашка</b>\n\nВсё сделано 🎉\n\n"
                "Чтобы записать задание, нажми «➕ Добавить» или просто напиши боту:\n"
                "<code>дз ML практическая 4 к четвергу</code>")
    n = len(items)
    word = "задание" if n % 10 == 1 and n % 100 != 11 else ("задания" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "заданий")
    return f"📝 <b>Домашка</b> — {n} {word}\n\n" + "\n\n".join(fmt_item(h, today) for h in items)


def due_on(hw: list[dict], day: date) -> list[dict]:
    iso = day.isoformat()
    return [h for h in hw if not h.get("done") and h.get("due") == iso]
