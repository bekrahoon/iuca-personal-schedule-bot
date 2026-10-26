"""Картинка недельного расписания (вертикальная, удобно смотреть с телефона)."""
import hashlib
import io
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from storage import lessons_on, start_minutes

FONT_DIR = Path(__file__).parent / "fonts"
_fonts: dict = {}


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    key = (weight, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(str(FONT_DIR / f"Inter-{weight}.otf"), size)
    return _fonts[key]


DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

BG = "#0F1115"
CARD = "#1A1E26"
CARD_TODAY = "#1E2533"
TEXT = "#ECEEF3"
MUTED = "#8C94A8"
DIM = "#5C6375"
ACCENT = "#6EA8FF"
ORANGE = "#FFB454"
RED = "#FF6B6B"
PALETTE = ["#7C9CFF", "#4FD1A5", "#FFB86B", "#FF7AA2", "#B48CFF",
           "#4FC3F7", "#E6D35A", "#FF8A65", "#8BD17C", "#F49AC1"]

W = 1080
PAD = 44
ROW_H = 132
ROW_GAP = 14


def _base(name: str) -> str:
    return name.split("(")[0].strip().lower()


def color_map(lessons: list[dict]) -> dict[str, str]:
    """Каждому предмету — свой цвет (по алфавиту, чтобы цвета не прыгали)."""
    names = sorted({_base(x["s"]) for x in lessons})
    if len(names) <= len(PALETTE):
        return {n: PALETTE[i] for i, n in enumerate(names)}
    return {n: PALETTE[int(hashlib.md5(n.encode()).hexdigest(), 16) % len(PALETTE)] for n in names}


def plural_pairs(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} пара"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} пары"
    return f"{n} пар"


def fit(draw: ImageDraw.ImageDraw, text: str, f, max_w: int) -> str:
    if draw.textlength(text, font=f) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def week_start(day: date) -> date:
    """Понедельник недели; в воскресенье показываем следующую неделю."""
    if day.weekday() == 6:
        day += timedelta(days=1)
    return day - timedelta(days=day.weekday())


def fmt_range(a: date, b: date) -> str:
    if a.month == b.month:
        return f"{a.day} – {b.day} {MONTHS[b.month - 1]}"
    return f"{a.day} {MONTHS[a.month - 1]} – {b.day} {MONTHS[b.month - 1]}"


def render_week(lessons: list[dict], monday: date, today: date) -> bytes:
    colors = color_map(lessons)
    days = []
    for i in range(6):
        day = monday + timedelta(days=i)
        items = lessons_on(lessons, day)
        if items or day == today:
            days.append((day, items))

    # --- высота
    h = PAD + 64 + 44 + 30
    for _, items in days:
        h += 64 + max(1, len(items)) * (ROW_H + ROW_GAP) + 22
    h += PAD - 10

    img = Image.new("RGB", (W, h), BG)
    d = ImageDraw.Draw(img)

    # --- заголовок
    y = PAD
    d.text((PAD, y), "Моё расписание", font=font("Bold", 52), fill=TEXT)
    y += 66
    d.text((PAD, y), fmt_range(monday, monday + timedelta(days=5)), font=font("Medium", 30), fill=MUTED)
    total = sum(1 for _, it in days for x in it if not x.get("_cancel"))
    cnt = plural_pairs(total)
    d.text((W - PAD - d.textlength(cnt, font=font("Medium", 30)), y), cnt, font=font("Medium", 30), fill=MUTED)
    y += 44 + 30

    for day, items in days:
        is_today = day == today
        # --- заголовок дня
        label = f"{DAYS[day.weekday()].upper()} · {day.strftime('%d.%m')}"
        d.text((PAD, y + 14), label, font=font("SemiBold", 28), fill=ACCENT if is_today else MUTED)
        if is_today:
            lx = PAD + d.textlength(label, font=font("SemiBold", 28)) + 16
            chip = "СЕГОДНЯ"
            cw = d.textlength(chip, font=font("Bold", 22)) + 28
            d.rounded_rectangle((lx, y + 10, lx + cw, y + 48), radius=19, fill=ACCENT)
            d.text((lx + 14, y + 16), chip, font=font("Bold", 22), fill=BG)
        y += 64

        if not items:
            d.rounded_rectangle((PAD, y, W - PAD, y + ROW_H), radius=22, fill=CARD_TODAY)
            d.text((PAD + 36, y + 46), "Пар нет — отдыхай", font=font("Medium", 32), fill=MUTED)
            y += ROW_H + ROW_GAP + 22
            continue

        for x in items:
            cancel = bool(x.get("_cancel"))
            changed = bool(x.get("_changed"))
            color = colors.get(_base(x["s"]), ACCENT)
            top, bottom = y, y + ROW_H
            d.rounded_rectangle((PAD, top, W - PAD, bottom), radius=22, fill=CARD_TODAY if is_today else CARD)

            # время
            start, _, end = x["t"].partition(" - ")
            tcol = DIM if cancel else TEXT
            d.text((PAD + 28, top + 26), start.zfill(5), font=font("Bold", 38), fill=tcol)
            d.text((PAD + 30, top + 76), end.zfill(5), font=font("Medium", 28), fill=DIM if cancel else MUTED)

            # цветная полоска
            bx = PAD + 158
            d.rounded_rectangle((bx, top + 22, bx + 7, bottom - 22), radius=4, fill=DIM if cancel else color)

            # аудитория (чип справа)
            room = "ОТМЕНА" if cancel else (x.get("r") or "")
            right = W - PAD - 24
            if room:
                rf = font("Bold", 28)
                room = fit(d, room, rf, 260)
                rw = d.textlength(room, font=rf) + 36
                chip_fill = RED if cancel else (ORANGE if changed and x.get("r") else "#262B36")
                chip_text = BG if (cancel or (changed and x.get("r"))) else TEXT
                d.rounded_rectangle((right - rw, top + 24, right, top + 72), radius=24, fill=chip_fill)
                d.text((right - rw + 18, top + 31), room, font=rf, fill=chip_text)
                right -= rw + 16

            # предмет + преподаватель
            tx = bx + 30
            sf = font("SemiBold", 36)
            subj = fit(d, x["s"], sf, right - tx)
            d.text((tx, top + 24), subj, font=sf, fill=DIM if cancel else TEXT)
            if cancel:
                sw = d.textlength(subj, font=sf)
                d.line((tx, top + 48, tx + sw, top + 48), fill=DIM, width=3)
            sub = []
            if changed and not cancel:
                sub.append("изменено на этот день")
            elif x.get("e"):
                sub.append(x["e"])
            if x.get("note") and not changed:
                sub.append(x["note"])
            if sub:
                mf = font("Regular", 27)
                d.text((tx, top + 78), fit(d, " · ".join(sub), mf, W - PAD - 24 - tx),
                       font=mf, fill=ORANGE if changed and not cancel else MUTED)
            y = bottom + ROW_GAP
        y += 22

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def lesson_sort_key(x: dict) -> int:
    return start_minutes(x["t"])
