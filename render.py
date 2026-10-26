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


# ---------------------------------------------------------------------
# Картинка одного дня — крупно, со статусами
# ---------------------------------------------------------------------
GREEN = "#4FD18B"


def _mins(hhmm: str) -> int:
    h, m = hhmm.strip().split(":")
    return int(h) * 60 + int(m)


def _dur(m: int) -> str:
    h, mm = divmod(m, 60)
    if h and mm:
        return f"{h} ч {mm} мин"
    return f"{h} ч" if h else f"{mm} мин"


def render_day(all_lessons: list[dict], day: date, now_minutes: int | None) -> bytes:
    """now_minutes — текущее время в минутах, если day == сегодня, иначе None."""
    colors = color_map(all_lessons)
    items = lessons_on(all_lessons, day)
    real = [x for x in items if not x.get("_cancel")]

    CARD_H = 236
    GAP = 22
    WIN_H = 70
    PADX = 40

    # --- раскладка (считаем высоту)
    blocks = []  # (kind, payload)
    prev_end = None
    for x in items:
        st, en = (_mins(p) for p in x["t"].split(" - "))
        if prev_end is not None and not x.get("_cancel") and st - prev_end >= 20:
            blocks.append(("win", st - prev_end))
        blocks.append(("card", x))
        if not x.get("_cancel"):
            prev_end = en

    NOW_EXTRA = 44

    def is_now(x):
        if now_minutes is None or x.get("_cancel"):
            return False
        a, b = (_mins(p) for p in x["t"].split(" - "))
        return a <= now_minutes < b

    h = 60 + 84 + 56 + 50
    for kind, p in blocks:
        h += (CARD_H + GAP + (NOW_EXTRA if is_now(p) else 0)) if kind == "card" else WIN_H
    if not real:
        h += 260
    h += 40

    img = Image.new("RGB", (W, h), BG)
    d = ImageDraw.Draw(img)

    # --- заголовок
    y = 60
    title = DAYS[day.weekday()]
    d.text((PADX, y), title, font=font("Bold", 72), fill=TEXT)
    if now_minutes is not None:
        tx = PADX + d.textlength(title, font=font("Bold", 72)) + 22
        chip = "СЕГОДНЯ"
        cw = d.textlength(chip, font=font("Bold", 28)) + 36
        d.rounded_rectangle((tx, y + 20, tx + cw, y + 68), radius=24, fill=ACCENT)
        d.text((tx + 18, y + 27), chip, font=font("Bold", 28), fill=BG)
    y += 84
    sub = f"{day.day} {MONTHS[day.month - 1]}"
    if real:
        sub += f"  ·  {plural_pairs(len(real))}  ·  {real[0]['t'].split(' - ')[0].zfill(5)} – {real[-1]['t'].split(' - ')[1].zfill(5)}"
    d.text((PADX, y), sub, font=font("Medium", 36), fill=MUTED)
    y += 56 + 50

    if not real:
        d.rounded_rectangle((PADX, y, W - PADX, y + 230), radius=32, fill=CARD)
        msg = "Пар нет"
        d.text(((W - d.textlength(msg, font=font("Bold", 60))) / 2, y + 50), msg, font=font("Bold", 60), fill=TEXT)
        msg2 = "можно отдыхать"
        d.text(((W - d.textlength(msg2, font=font("Medium", 36))) / 2, y + 135), msg2, font=font("Medium", 36), fill=MUTED)
        y += 260

    next_marked = False
    for kind, payload in blocks:
        if kind == "win":
            label = f"окно {_dur(payload)}"
            lf = font("Medium", 32)
            lw = d.textlength(label, font=lf)
            cy = y + WIN_H // 2 - 6
            d.line((PADX + 30, cy, (W - lw) / 2 - 24, cy), fill="#2A303C", width=3)
            d.line(((W + lw) / 2 + 24, cy, W - PADX - 30, cy), fill="#2A303C", width=3)
            d.text(((W - lw) / 2, cy - 21), label, font=lf, fill=DIM)
            y += WIN_H
            continue

        x = payload
        cancel = bool(x.get("_cancel"))
        changed = bool(x.get("_changed"))
        st, en = (_mins(p) for p in x["t"].split(" - "))
        state = ""
        if now_minutes is not None and not cancel:
            if st <= now_minutes < en:
                state = "now"
            elif now_minutes >= en:
                state = "past"
            elif not next_marked:
                state = "next"
                next_marked = True
        color = colors.get(_base(x["s"]), ACCENT)
        faded = cancel or state == "past"

        top, bottom = y, y + CARD_H + (NOW_EXTRA if state == "now" else 0)
        fill = "#1B2A22" if state == "now" else ("#141821" if faded else CARD)
        d.rounded_rectangle((PADX, top, W - PADX, bottom), radius=32, fill=fill,
                            outline=GREEN if state == "now" else None, width=4)
        # цветная полоса слева
        d.rounded_rectangle((PADX + 26, top + 30, PADX + 36, bottom - 30), radius=5,
                            fill=DIM if faded else color)
        lx = PADX + 66
        right = W - PADX - 34

        # строка 1: время + статус
        tcol = DIM if faded else TEXT
        tstr = f"{x['t'].split(' - ')[0].zfill(5)} – {x['t'].split(' - ')[1].zfill(5)}"
        d.text((lx, top + 30), tstr, font=font("Bold", 44), fill=tcol)
        chip, cfill, ctext = "", "", BG
        if cancel:
            chip, cfill = "ОТМЕНА", RED
        elif state == "now":
            chip, cfill = "ИДЁТ СЕЙЧАС", GREEN
        elif state == "next":
            chip, cfill = f"ЧЕРЕЗ {_dur(st - now_minutes).upper()}", ACCENT
        elif state == "past":
            chip, cfill, ctext = "ПРОШЛА", "#232834", DIM
        if chip:
            cf = font("Bold", 28)
            cw = d.textlength(chip, font=cf) + 40
            d.rounded_rectangle((right - cw, top + 32, right, top + 80), radius=24, fill=cfill)
            d.text((right - cw + 20, top + 39), chip, font=cf, fill=ctext)

        # строка 2: предмет
        sf = font("SemiBold", 50)
        subj = fit(d, x["s"], sf, right - lx)
        d.text((lx, top + 92), subj, font=sf, fill=DIM if faded else TEXT)
        if cancel:
            sw = d.textlength(subj, font=sf)
            d.line((lx, top + 124, lx + sw, top + 124), fill=DIM, width=4)

        # строка 3: аудитория + преподаватель / пометка
        y3 = top + 166
        cx = lx
        if x.get("r") and not cancel:
            rf = font("Bold", 32)
            room = fit(d, x["r"], rf, 360)
            rw = d.textlength(room, font=rf) + 36
            rfill = ORANGE if changed else ("#232834" if faded else "#2A3140")
            d.rounded_rectangle((cx, y3 - 6, cx + rw, y3 + 42), radius=22, fill=rfill)
            d.text((cx + 18, y3), room, font=rf, fill=BG if changed else (DIM if faded else TEXT))
            cx += rw + 20
        extra = ""
        ecol = DIM if faded else MUTED
        if cancel:
            extra = "пара отменена в этот день"
            ecol = RED
        elif changed:
            extra = "изменено на этот день"
            ecol = ORANGE
        elif x.get("e"):
            extra = x["e"]
        if extra:
            ef = font("Medium", 32)
            d.text((cx, y3 + 1), fit(d, extra, ef, right - cx), font=ef, fill=ecol)

        # прогресс текущей пары
        if state == "now":
            frac = (now_minutes - st) / max(1, en - st)
            bx0, bx1, by = lx, right, bottom - 26
            d.rounded_rectangle((bx0, by - 8, bx1, by), radius=4, fill="#2A3A30")
            d.rounded_rectangle((bx0, by - 8, bx0 + max(16, (bx1 - bx0) * frac), by), radius=4, fill=GREEN)
            left = f"осталось {_dur(en - now_minutes)}"
            lf = font("SemiBold", 26)
            d.text((right - d.textlength(left, font=lf), by - 46), left, font=lf, fill=GREEN)

        y = bottom + GAP

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
