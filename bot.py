"""
Личный бот расписания IUCA.
- своё расписание из пар разных курсов;
- добавление пар прямо из общего расписания IUCA (поиск по предмету);
- редактирование по фото или текстовой командой (через Gemini);
- ручное редактирование кнопками;
- напоминания перед парой.
"""
import asyncio
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import render
import storage
from ai_editor import AIBusyError, ai_enabled, edit_schedule
from quick_edit import quick_edit
from storage import TIME_SLOTS, lessons_on, make_lesson, next_date_for_weekday, normalize_extras, start_minutes

# =====================================================================
# НАСТРОЙКИ
# =====================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Если задан — ботом может пользоваться только этот Telegram ID (личный бот).
OWNER_ID = int(os.environ["OWNER_ID"]) if os.environ.get("OWNER_ID") else None

KG_TZ = timezone(timedelta(hours=6))
DAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]

# Общее расписание IUCA (тот же формат, что в tgbotiuca)
IUCA_FILE = Path(__file__).parent / "iuca_schedule.json"
with open(IUCA_FILE, "r", encoding="utf-8") as f:
    IUCA = json.load(f)

# Секции = один предмет у одного набора групп (все его пары за неделю)
_sections: dict[tuple, list[dict]] = {}
for _it in IUCA:
    _sections.setdefault((_it["s"], tuple(sorted(_it["g"]))), []).append(_it)
SECTIONS = [(k[0], list(k[1]), v) for k, v in sorted(_sections.items())]

# =====================================================================
# ФОРМАТИРОВАНИЕ
# =====================================================================
def now_kg() -> datetime:
    return datetime.now(KG_TZ)


async def lessons_of(user_id: int) -> list[dict]:
    return (await storage.load(user_id))["lessons"]


def ddmm(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}"


def fmt_once(o: dict) -> str:
    if o.get("cancel"):
        txt = "отменена"
    else:
        parts = []
        if o.get("t"):
            parts.append(f"время {o['t']}")
        if o.get("r"):
            parts.append(f"ауд. {escape(o['r'])}")
        if o.get("note"):
            parts.append(escape(o["note"]))
        txt = ", ".join(parts)
    return f"{ddmm(o['date'])}: {txt}"


def fmt_lesson(x: dict, idx: int | None = None, base_view: bool = False) -> str:
    """base_view=True — показываем недельную пару и список её разовых изменений;
    иначе — пару на конкретную дату (с уже применёнными изменениями)."""
    num = f"{idx}. " if idx else ""
    if x.get("_cancel"):
        return f"<b>{num}❌ <s>{x['t']}</s></b>\n📚 <s>{escape(x['s'])}</s>\n⚠️ <b>Пара отменена в этот день</b>"
    mark = " 🔄" if x.get("_changed") else ""
    lines = [f"<b>{num}⏰ {x['t']}</b>{mark}", f"📚 {escape(x['s'])}"]
    if x.get("r"):
        lines.append(f"🚪 Ауд.: <code>{escape(x['r'])}</code>")
    if x.get("e"):
        lines.append(f"👤 {escape(x['e'])}")
    if x.get("note"):
        lines.append(f"📝 <i>{escape(x['note'])}</i>")
    if x.get("mute"):
        lines.append("🔕 <i>без напоминания</i>")
    if x.get("_changed"):
        lines.append("🔄 <i>изменено только на этот день</i>")
    if base_view:
        if x.get("date"):
            lines.append(f"📅 <i>разовая пара: {ddmm(x['date'])}</i>")
        for o in x.get("once", []):
            lines.append(f"↳ <i>{fmt_once(o)}</i>")
    return "\n".join(lines)


MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]


def fmt_card(x: dict, base_view: bool = False, status: str = "") -> str:
    """Одна пара — компактная карточка в цитате."""
    start, _, end = x["t"].partition(" - ")
    t = f"{start.zfill(5)} – {end.zfill(5)}"
    if x.get("_cancel"):
        return f"<blockquote>❌ <s>{t}  {escape(x['s'])}</s>\n<b>Отменена в этот день</b></blockquote>"
    head = f"{status}<b>{t}</b>  {escape(x['s'])}"
    info = []
    if x.get("r"):
        room = escape(x["r"])
        info.append(f"📍 <b>{room}</b>" + (" 🔄" if x.get("_changed") else ""))
    if x.get("e"):
        info.append(escape(x["e"]))
    lines = [head]
    if info:
        lines.append(" · ".join(info))
    if x.get("_changed") and not x.get("r"):
        lines.append("🔄 <i>изменено на этот день</i>")
    if x.get("note"):
        lines.append(f"📝 <i>{escape(x['note'])}</i>")
    if x.get("mute"):
        lines.append("🔕 <i>без напоминания</i>")
    if base_view:
        if x.get("date"):
            lines.append(f"📅 <i>разовая пара: {ddmm(x['date'])}</i>")
        for o in x.get("once", []):
            lines.append(f"↳ <i>{fmt_once(o)}</i>")
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def _dur(mins: int) -> str:
    h, m = divmod(mins, 60)
    return (f"{h} ч " if h else "") + (f"{m} мин" if m else "").strip() or "0 мин"


async def fmt_day(user_id: int, d: int, on_date=None) -> str:
    """on_date — конкретная дата (с разовыми изменениями); None — недельный вид для редактирования."""
    if d > 5:
        return "🎉 <b>Воскресенье</b> — выходной!"
    lessons = await lessons_of(user_id)
    if on_date is None:
        items = [x for x in lessons if x["d"] == d]
        header = f"📅 <b>{DAYS_RU[d]}</b>"
        if not items:
            return header + "\n\n😴 Пар нет"
        return header + "\n\n" + "\n".join(fmt_card(x, base_view=True) for x in items)

    items = lessons_on(lessons, on_date)
    header = f"📅 <b>{DAYS_RU[d]}, {on_date.day} {MONTHS_GEN[on_date.month - 1]}</b>"
    real = [x for x in items if not x.get("_cancel")]
    if not real:
        body = "\n\n😴 Пар нет — отдыхай!"
        if items:
            body += "\n\n" + "\n".join(fmt_card(x) for x in items)
        return header + body
    first = real[0]["t"].split(" - ")[0].zfill(5)
    last = real[-1]["t"].split(" - ")[1].zfill(5)
    header += f"\n<i>{render.plural_pairs(len(real))} · {first} – {last}</i>"

    now = now_kg()
    is_today = on_date == now.date()
    cur = now.hour * 60 + now.minute
    next_marked = False
    out = []
    prev_end = None
    for x in items:
        st = start_minutes(x["t"])
        en = start_minutes(x["t"].split(" - ")[1] + " - 0:00")
        if prev_end is not None and not x.get("_cancel") and st - prev_end >= 30:
            out.append(f"☕ <i>окно {_dur(st - prev_end)}</i>")
        status = ""
        if is_today and not x.get("_cancel"):
            if st <= cur < en:
                status = "▶️ <b>Сейчас</b> · "
            elif cur < st and not next_marked:
                status = f"⏭ <b>Через {_dur(st - cur)}</b> · "
                next_marked = True
            elif cur >= en:
                status = "✔️ "
        out.append(fmt_card(x, status=status))
        if not x.get("_cancel"):
            prev_end = en
    return header + "\n\n" + "\n".join(out)


def fmt_week(lessons: list[dict], title: str = "🗓 <b>Моё расписание</b>") -> str:
    blocks = []
    for d in range(6):
        items = [x for x in lessons if x["d"] == d and not x.get("date")]
        if not items:
            continue
        rows = [f"📌 <b>{DAYS_RU[d].upper()}</b>"]
        for x in items:
            room = f" (<b>{escape(x['r'])}</b>)" if x.get("r") else ""
            rows.append(f"• <code>{x['t']}</code> — {escape(x['s'])}{room}")
            for o in x.get("once", []):
                rows.append(f"    ↳ <i>{fmt_once(o)}</i>")
        blocks.append("\n".join(rows))
    one_off = sorted((x for x in lessons if x.get("date")), key=lambda x: (x["date"], start_minutes(x["t"])))
    if one_off:
        rows = ["📌 <b>РАЗОВЫЕ ПАРЫ</b>"]
        for x in one_off:
            room = f" (<b>{escape(x['r'])}</b>)" if x.get("r") else ""
            rows.append(f"• {ddmm(x['date'])} <code>{x['t']}</code> — {escape(x['s'])}{room}")
        blocks.append("\n".join(rows))
    if not blocks:
        return title + "\n\nПока пусто. Отправь фото расписания или добавь пары через «📚 Добавить из IUCA»."
    return title + "\n\n" + "\n\n".join(blocks)


def fmt_diff(old: list[dict], new: list[dict]) -> str:
    old_by_id = {x["id"]: x for x in old}
    new_ids = {x["id"] for x in new}
    lines = []
    for x in new:
        o = old_by_id.get(x["id"])
        if o is None:
            when = f"{ddmm(x['date'])} (разово)" if x.get("date") else DAYS_SHORT[x["d"]]
            lines.append(f"➕ {when} {x['t']} — {escape(x['s'])}")
            for oc in x.get("once", []):
                lines.append(f"    📌 {fmt_once(oc)}")
            continue
        old_once = {json.dumps(e, sort_keys=True) for e in o.get("once", [])}
        for oc in x.get("once", []):
            if json.dumps(oc, sort_keys=True) not in old_once:
                lines.append(f"📌 {escape(x['s'])} ({DAYS_SHORT[x['d']]} {x['t']}) — {fmt_once(oc)}")
        new_once = {json.dumps(e, sort_keys=True) for e in x.get("once", [])}
        for oc in o.get("once", []):
            if json.dumps(oc, sort_keys=True) not in new_once:
                lines.append(f"🗑 Убрано разовое: {escape(o['s'])} — {fmt_once(oc)}")
        if any(o.get(k) != x.get(k) for k in ("d", "t", "s", "r", "e", "note")):
            lines.append(
                f"✏️ {DAYS_SHORT[o['d']]} {o['t']} {escape(o['s'])}"
                f"\n    → {DAYS_SHORT[x['d']]} {x['t']} {escape(x['s'])}"
                + (f" ({escape(x['r'])})" if x.get("r") else "")
            )
    for x in old:
        if x["id"] not in new_ids:
            lines.append(f"➖ {DAYS_SHORT[x['d']]} {x['t']} — {escape(x['s'])}")
    return "\n".join(lines) if lines else "Изменений нет."


def chunks(text: str, limit: int = 4000) -> list[str]:
    out, cur = [], ""
    for part in text.split("\n\n"):
        if len(cur) + len(part) + 2 > limit:
            out.append(cur)
            cur = part
        else:
            cur = f"{cur}\n\n{part}" if cur else part
    if cur:
        out.append(cur)
    return out

# =====================================================================
# КЛАВИАТУРЫ
# =====================================================================
BTN_TODAY, BTN_TOMORROW = "📅 Сегодня", "📆 Завтра"
BTN_WEEK, BTN_EDIT = "🗓 Неделя", "✏️ Редактировать"
BTN_IUCA, BTN_REMIND = "📚 Добавить из IUCA", "🔔 Напоминания"
MENU_BUTTONS = {BTN_TODAY, BTN_TOMORROW, BTN_WEEK, BTN_EDIT, BTN_IUCA, BTN_REMIND}


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
            [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_EDIT)],
            [KeyboardButton(text=BTN_IUCA), KeyboardButton(text=BTN_REMIND)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="📸 Фото или «перенеси ML на чт»",
    )


def days_kb(active: int, prefix: str = "day") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, name in enumerate(DAYS_SHORT):
        b.button(text=f"• {name} •" if i == active else name, callback_data=f"{prefix}:{i}")
    b.adjust(6)
    return b.as_markup()


async def edit_day_kb(user_id: int, d: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for x in [x for x in await lessons_of(user_id) if x["d"] == d]:
        b.row(InlineKeyboardButton(text=f"🗑 {x['t']} {x['s'][:28]}", callback_data=f"del:{x['id']}"))
    b.row(InlineKeyboardButton(text="➕ Добавить пару в этот день", callback_data=f"add:{d}"))
    day_row = [
        InlineKeyboardButton(text=f"• {n} •" if i == d else n, callback_data=f"ed:{i}")
        for i, n in enumerate(DAYS_SHORT)
    ]
    b.row(*day_row)
    b.row(InlineKeyboardButton(text="🧹 Очистить всё расписание", callback_data="clear_ask"))
    return b.as_markup()


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Применить", callback_data="ai_ok"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="ai_no"),
    ]])


# =====================================================================
# СОСТОЯНИЯ
# =====================================================================
class AddLesson(StatesGroup):
    custom_time = State()
    subject = State()
    room = State()
    teacher = State()


class IucaSearch(StatesGroup):
    query = State()


class AiConfirm(StatesGroup):
    waiting = State()


router = Router()


@router.message.outer_middleware()
async def owner_only_msg(handler, event, data):
    if OWNER_ID and event.from_user and event.from_user.id != OWNER_ID:
        return
    return await handler(event, data)


@router.callback_query.outer_middleware()
async def owner_only_cb(handler, event, data):
    if OWNER_ID and event.from_user.id != OWNER_ID:
        return await event.answer("Это личный бот 🙂")
    return await handler(event, data)


# =====================================================================
# СТАРТ / ПРОСМОТР
# =====================================================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await storage.save(message.from_user.id, await storage.load(message.from_user.id))
    ai_line = (
        "📸 <b>Отправь фото</b> расписания — я сам всё внесу.\n"
        "💬 Или напиши словами: <i>«перенеси Machine Learning на четверг 14:45»</i>, "
        "<i>«в пятницу пары в B201 отменили»</i>.\n\n"
        if ai_enabled()
        else "⚠️ Редактирование по фото выключено: не задан GEMINI_API_KEY.\n\n"
    )
    await message.answer(
        "👋 <b>Личное расписание IUCA</b>\n\n"
        + ai_line
        + "📚 «Добавить из IUCA» — найти предмет в общем расписании (любой курс) и добавить его пары.\n"
        "✏️ «Редактировать» — удалить/добавить пары вручную.\n\n"
        "/export — скачать расписание JSON, /help — подсказка",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await cmd_start(message, state)


@router.message(F.text.in_({BTN_TODAY, BTN_TOMORROW}))
async def show_today(message: Message, state: FSMContext):
    await state.clear()
    day = now_kg().date()
    if message.text == BTN_TOMORROW:
        day += timedelta(days=1)
    await message.answer_photo(await day_photo(message.from_user.id, day), reply_markup=day_nav_kb(day))


def day_nav_kb(day) -> InlineKeyboardMarkup:
    today = now_kg().date()
    prev_d, next_d = day - timedelta(days=1), day + timedelta(days=1)
    if prev_d.weekday() == 6:
        prev_d -= timedelta(days=1)
    if next_d.weekday() == 6:
        next_d += timedelta(days=1)
    monday = render.week_start(day)
    week_row = []
    for i, name in enumerate(DAYS_SHORT):
        dd = monday + timedelta(days=i)
        week_row.append(InlineKeyboardButton(text=f"• {name} •" if dd == day else name, callback_data=f"dd:{dd.isoformat()}"))
    mid = "Сегодня" if day != today else "🔄 Обновить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"◀️ {prev_d.strftime('%d.%m')}", callback_data=f"dd:{prev_d.isoformat()}"),
            InlineKeyboardButton(text=mid, callback_data=f"dd:{today.isoformat()}"),
            InlineKeyboardButton(text=f"{next_d.strftime('%d.%m')} ▶️", callback_data=f"dd:{next_d.isoformat()}"),
        ],
        week_row,
        [InlineKeyboardButton(text="📝 Текстом", callback_data=f"ddt:{day.isoformat()}")],
    ])


async def day_photo(uid: int, day) -> BufferedInputFile:
    now = now_kg()
    now_min = now.hour * 60 + now.minute if day == now.date() else None
    png = await asyncio.to_thread(render.render_day, await lessons_of(uid), day, now_min)
    return BufferedInputFile(png, f"day_{day.isoformat()}.png")


@router.callback_query(F.data.startswith("dd:"))
async def cb_day_photo(call: CallbackQuery):
    await call.answer()
    from datetime import date as _date
    day = _date.fromisoformat(call.data.split(":", 1)[1])
    try:
        await call.message.edit_media(InputMediaPhoto(media=await day_photo(call.from_user.id, day)),
                                      reply_markup=day_nav_kb(day))
    except Exception:
        logging.exception("day edit failed")


@router.callback_query(F.data.startswith("ddt:"))
async def cb_day_text(call: CallbackQuery):
    from datetime import date as _date
    day = _date.fromisoformat(call.data.split(":", 1)[1])
    await call.message.answer(await fmt_day(call.from_user.id, day.weekday(), day), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("day:"))
async def cb_day(call: CallbackQuery):
    d = int(call.data.split(":")[1])
    try:
        day = next_date_for_weekday(d, now_kg().date())
        await call.message.edit_text(await fmt_day(call.from_user.id, d, day), reply_markup=days_kb(d), parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


def week_kb(offset: int) -> InlineKeyboardMarkup:
    mid = "Эта неделя" if offset == 0 else ("Следующая" if offset == 1 else ("Прошлая" if offset == -1 else f"{offset:+d} нед."))
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️", callback_data=f"wk:{offset - 1}"),
            InlineKeyboardButton(text=mid, callback_data="wk:0"),
            InlineKeyboardButton(text="▶️", callback_data=f"wk:{offset + 1}"),
        ],
        [InlineKeyboardButton(text="📝 Текстом", callback_data="wktxt")],
    ])


async def week_photo(uid: int, offset: int) -> BufferedInputFile:
    today = now_kg().date()
    monday = render.week_start(today) + timedelta(weeks=offset)
    png = await asyncio.to_thread(render.render_week, await lessons_of(uid), monday, today)
    return BufferedInputFile(png, f"week_{monday.isoformat()}.png")


@router.message(F.text == BTN_WEEK)
async def show_week(message: Message, state: FSMContext):
    await state.clear()
    if not await lessons_of(message.from_user.id):
        return await message.answer(fmt_week([]), parse_mode="HTML")
    await message.answer_photo(await week_photo(message.from_user.id, 0), reply_markup=week_kb(0))


@router.callback_query(F.data.startswith("wk:"))
async def cb_week(call: CallbackQuery):
    await call.answer()
    offset = max(-8, min(26, int(call.data.split(":")[1])))
    try:
        await call.message.edit_media(
            InputMediaPhoto(media=await week_photo(call.from_user.id, offset)),
            reply_markup=week_kb(offset),
        )
    except Exception:
        logging.exception("week edit failed")


@router.callback_query(F.data == "wktxt")
async def cb_week_text(call: CallbackQuery):
    for part in chunks(fmt_week(await lessons_of(call.from_user.id))):
        await call.message.answer(part, parse_mode="HTML")
    await call.answer()


@router.message(Command("export"))
async def cmd_export(message: Message):
    data = await storage.load(message.from_user.id)
    raw = json.dumps(data["lessons"], ensure_ascii=False, indent=2).encode()
    await message.answer_document(BufferedInputFile(raw, "my_schedule.json"), caption="Твоё расписание")


# =====================================================================
# РУЧНОЕ РЕДАКТИРОВАНИЕ
# =====================================================================
@router.message(F.text == BTN_EDIT)
async def edit_menu(message: Message, state: FSMContext):
    await state.clear()
    d = min(now_kg().weekday(), 5)
    await message.answer(
        await fmt_day(message.from_user.id, d) + "\n\n<i>Нажми 🗑, чтобы удалить пару.</i>",
        reply_markup=await edit_day_kb(message.from_user.id, d),
        parse_mode="HTML",
    )


async def refresh_edit(call: CallbackQuery, d: int):
    try:
        await call.message.edit_text(
            await fmt_day(call.from_user.id, d) + "\n\n<i>Нажми 🗑, чтобы удалить пару.</i>",
            reply_markup=await edit_day_kb(call.from_user.id, d),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("ed:"))
async def cb_edit_day(call: CallbackQuery):
    await refresh_edit(call, int(call.data.split(":")[1]))
    await call.answer()


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(call: CallbackQuery):
    removed = await storage.delete_lesson(call.from_user.id, call.data.split(":")[1])
    if removed:
        await refresh_edit(call, removed["d"])
        await call.answer(f"Удалено: {removed['s'][:40]}")
    else:
        await call.answer("Уже удалено")


@router.callback_query(F.data == "clear_ask")
async def cb_clear_ask(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить всё", callback_data="clear_yes"),
        InlineKeyboardButton(text="Нет", callback_data="ed:0"),
    ]])
    await call.message.edit_reply_markup(reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "clear_yes")
async def cb_clear(call: CallbackQuery):
    await storage.replace_lessons(call.from_user.id, [])
    await call.message.edit_text("🧹 Расписание очищено.")
    await call.answer()


@router.callback_query(F.data.startswith("add:"))
async def cb_add(call: CallbackQuery, state: FSMContext):
    d = int(call.data.split(":")[1])
    await state.update_data(add_d=d)
    b = InlineKeyboardBuilder()
    for i, slot in enumerate(TIME_SLOTS):
        b.button(text=slot, callback_data=f"slot:{i}")
    b.button(text="🕒 Другое время", callback_data="slot:custom")
    b.adjust(2)
    await call.message.answer(f"➕ <b>{DAYS_RU[d]}</b>. Выбери время:", reply_markup=b.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("slot:"))
async def cb_slot(call: CallbackQuery, state: FSMContext):
    v = call.data.split(":")[1]
    if v == "custom":
        await state.set_state(AddLesson.custom_time)
        await call.message.edit_text("Напиши время в формате <code>9:30 - 10:50</code>:", parse_mode="HTML")
    else:
        await state.update_data(add_t=TIME_SLOTS[int(v)])
        await state.set_state(AddLesson.subject)
        await call.message.edit_text(f"⏰ {TIME_SLOTS[int(v)]}\nНапиши название предмета:")
    await call.answer()


@router.message(AddLesson.custom_time, F.text)
async def add_custom_time(message: Message, state: FSMContext):
    t = storage.normalize_time(message.text)
    if start_minutes(t) == 0 or "-" not in t:
        return await message.answer("Не понял время. Пример: <code>13:10 - 14:30</code>", parse_mode="HTML")
    await state.update_data(add_t=t)
    await state.set_state(AddLesson.subject)
    await message.answer("Название предмета:")


@router.message(AddLesson.subject, F.text)
async def add_subject(message: Message, state: FSMContext):
    await state.update_data(add_s=message.text.strip())
    await state.set_state(AddLesson.room)
    await message.answer("Аудитория? (или <code>-</code>, чтобы пропустить)", parse_mode="HTML")


@router.message(AddLesson.room, F.text)
async def add_room(message: Message, state: FSMContext):
    await state.update_data(add_r="" if message.text.strip() == "-" else message.text.strip())
    await state.set_state(AddLesson.teacher)
    await message.answer("Преподаватель? (или <code>-</code>)", parse_mode="HTML")


@router.message(AddLesson.teacher, F.text)
async def add_teacher(message: Message, state: FSMContext):
    data = await state.get_data()
    teacher = "" if message.text.strip() == "-" else message.text.strip()
    lesson = make_lesson(data["add_d"], data["add_t"], data["add_s"], data["add_r"], teacher)
    await storage.add_lessons(message.from_user.id, [lesson])
    await state.clear()
    await message.answer("✅ Добавлено!\n\n" + await fmt_day(message.from_user.id, lesson["d"]), parse_mode="HTML")


# =====================================================================
# ДОБАВЛЕНИЕ ИЗ ОБЩЕГО РАСПИСАНИЯ IUCA
# =====================================================================
@router.message(F.text == BTN_IUCA)
async def iuca_start(message: Message, state: FSMContext):
    await state.set_state(IucaSearch.query)
    await message.answer(
        "🔍 Напиши часть названия предмета или фамилию преподавателя.\n"
        "Например: <code>Machine</code>, <code>Testing</code>, <code>Veis</code>",
        parse_mode="HTML",
    )


@router.message(IucaSearch.query, F.text, ~F.text.in_(MENU_BUTTONS))
async def iuca_search(message: Message, state: FSMContext):
    q = message.text.strip().lower()
    found = [
        i for i, (s, groups, items) in enumerate(SECTIONS)
        if q in s.lower() or any(q in e.lower() for it in items for e in it["e"])
    ]
    if not found:
        return await message.answer("❌ Ничего не нашёл. Попробуй другое слово (на английском, как в расписании).")
    await state.clear()
    b = InlineKeyboardBuilder()
    for i in found[:25]:
        s, groups, items = SECTIONS[i]
        days = ",".join(sorted({DAYS_SHORT[it["d"]] for it in items}, key=DAYS_SHORT.index))
        b.row(InlineKeyboardButton(text=f"{s[:30]} · {'/'.join(groups)[:18]} · {days}", callback_data=f"sec:{i}"))
    more = f"\n(показаны первые 25 из {len(found)}, уточни запрос)" if len(found) > 25 else ""
    await message.answer(f"Нашёл {len(found)}. Выбери нужную секцию:{more}", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("sec:"))
async def cb_section(call: CallbackQuery):
    i = int(call.data.split(":")[1])
    s, groups, items = SECTIONS[i]
    items = sorted(items, key=lambda it: (it["d"], start_minutes(it["t"])))
    rows = [
        f"• {DAYS_SHORT[it['d']]} <code>{it['t']}</code> — {', '.join(it['r']) or '—'} ({', '.join(it['e'])})"
        for it in items
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"➕ Добавить все {len(items)} пар", callback_data=f"secadd:{i}"),
    ]])
    await call.message.answer(
        f"📚 <b>{escape(s)}</b>\nГруппы: {', '.join(groups)}\n\n" + "\n".join(rows),
        reply_markup=kb,
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("secadd:"))
async def cb_section_add(call: CallbackQuery):
    s, groups, items = SECTIONS[int(call.data.split(":")[1])]
    new = [make_lesson(it["d"], it["t"], it["s"], ", ".join(it["r"]), ", ".join(it["e"])) for it in items]
    added = await storage.add_lessons(call.from_user.id, new)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(f"✅ Добавлено пар: {added}" + (" (остальные уже были)" if added < len(new) else ""))
    await call.answer()


# =====================================================================
# НАПОМИНАНИЯ
# =====================================================================
@router.message(F.text == BTN_REMIND)
async def remind_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(**await remind_view(message.from_user.id))


async def remind_view(uid: int) -> dict:
    data = await storage.load(uid)
    cur = data["remind"]
    total = len(data["lessons"])
    on = sum(1 for x in data["lessons"] if not x.get("mute"))
    b = InlineKeyboardBuilder()
    for m in (0, 5, 10, 15, 30):
        label = "Выкл" if m == 0 else f"{m} мин"
        b.button(text=("✅ " if m == cur else "") + label, callback_data=f"rem:{m}")
    b.adjust(5)
    b.row(InlineKeyboardButton(text=f"🎯 Выбрать пары ({on}/{total})", callback_data=f"rsd:{min(now_kg().weekday(), 5)}"))
    text = "🔔 <b>Напоминания</b>\n\n"
    text += "Сейчас: выключены" if not cur else f"Сейчас: за <b>{cur} мин</b> до пары"
    text += f"\nНапоминаю о <b>{on}</b> из {total} пар. Нажми «🎯 Выбрать пары», чтобы включить/выключить конкретные."
    return {"text": text, "reply_markup": b.as_markup(), "parse_mode": "HTML"}


@router.callback_query(F.data.startswith("rem:"))
async def cb_remind(call: CallbackQuery):
    m = int(call.data.split(":")[1])
    await storage.set_remind(call.from_user.id, m)
    try:
        await call.message.edit_text(**await remind_view(call.from_user.id))
    except Exception:
        pass
    await call.answer("Выключено" if m == 0 else f"За {m} мин")


async def remind_pick_view(uid: int, d: int) -> dict:
    lessons = [x for x in await lessons_of(uid) if x["d"] == d]
    b = InlineKeyboardBuilder()
    for x in lessons:
        icon = "🔕" if x.get("mute") else "🔔"
        b.row(InlineKeyboardButton(text=f"{icon} {x['t'].split(' ')[0]} {x['s'][:30]}", callback_data=f"rtog:{d}:{x['id']}"))
    if lessons:
        b.row(
            InlineKeyboardButton(text="🔔 Все в этот день", callback_data=f"rall:{d}:1"),
            InlineKeyboardButton(text="🔕 Ни одной", callback_data=f"rall:{d}:0"),
        )
    b.row(*[
        InlineKeyboardButton(text=f"• {n} •" if i == d else n, callback_data=f"rsd:{i}")
        for i, n in enumerate(DAYS_SHORT)
    ])
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="rback"))
    text = f"🎯 <b>{DAYS_RU[d]}</b> — нажми на пару, чтобы включить 🔔 или выключить 🔕 напоминание."
    if not lessons:
        text = f"🎯 <b>{DAYS_RU[d]}</b> — пар нет."
    return {"text": text, "reply_markup": b.as_markup(), "parse_mode": "HTML"}


async def _edit(call: CallbackQuery, view: dict):
    try:
        await call.message.edit_text(**view)
    except Exception:
        pass


@router.callback_query(F.data.startswith("rsd:"))
async def cb_remind_day(call: CallbackQuery):
    await _edit(call, await remind_pick_view(call.from_user.id, int(call.data.split(":")[1])))
    await call.answer()


@router.callback_query(F.data.startswith("rtog:"))
async def cb_remind_toggle(call: CallbackQuery):
    _, d, lid = call.data.split(":")
    x = await storage.toggle_mute(call.from_user.id, lid)
    await _edit(call, await remind_pick_view(call.from_user.id, int(d)))
    if x:
        await call.answer(("🔕 Не напоминать: " if x.get("mute") else "🔔 Напоминать: ") + x["s"][:40])
    else:
        await call.answer()


@router.callback_query(F.data.startswith("rall:"))
async def cb_remind_all(call: CallbackQuery):
    _, d, on = call.data.split(":")
    await storage.set_mute_day(call.from_user.id, int(d), mute=(on == "0"))
    await _edit(call, await remind_pick_view(call.from_user.id, int(d)))
    await call.answer()


@router.callback_query(F.data == "rback")
async def cb_remind_back(call: CallbackQuery):
    await _edit(call, await remind_view(call.from_user.id))
    await call.answer()


async def check_reminders(bot: Bot) -> int:
    """Отправляет напоминания о парах, которые начнутся в ближайшие `remind` минут.
    Можно вызывать с любой периодичностью: уже отправленные не дублируются."""
    now = now_kg()
    cur = now.hour * 60 + now.minute
    today = now.date().isoformat()
    sent_count = 0
    for uid in await storage.all_user_ids():
        data = await storage.load(uid)
        m = data.get("remind", 0)
        if not m or now.weekday() > 5:
            continue
        sent_today = set(data.get("sent", {}).get(today, []))
        changed = False
        for x in lessons_on(data["lessons"], now.date()):
            if x.get("_cancel"):
                continue
            left = start_minutes(x["t"]) - cur
            if x["d"] == now.weekday() and not x.get("mute") and 0 < left <= m and x["id"] not in sent_today:
                try:
                    await bot.send_message(uid, f"🔔 <b>Через {left} мин</b>\n" + fmt_card(x), parse_mode="HTML")
                    sent_count += 1
                except Exception:
                    logging.exception("reminder send failed")
                sent_today.add(x["id"])
                changed = True
        if changed:
            data["sent"] = {today: sorted(sent_today)}
            await storage.save(uid, data)
    return sent_count


async def reminder_loop(bot: Bot):
    while True:
        try:
            await check_reminders(bot)
        except Exception:
            logging.exception("reminder loop")
        await asyncio.sleep(30)


# =====================================================================
# РЕДАКТИРОВАНИЕ ЧЕРЕЗ GEMINI (ФОТО / ТЕКСТ)
# =====================================================================
async def run_ai_edit(message: Message, state: FSMContext, instruction: str, image: bytes | None, mime: str):
    # 1) Частые сообщения (смена аудитории / отмена на дату) разбираем сами — мгновенно и без Gemini
    if image is None and instruction:
        current = await lessons_of(message.from_user.id)
        quick = quick_edit(instruction, current, now_kg().date())
        if quick:
            new, summary = quick
            await state.set_state(AiConfirm.waiting)
            await state.update_data(pending=new)
            text = f"⚡ {escape(summary)}\n\n<b>Изменения:</b>\n" + fmt_diff(current, new)
            return await message.answer(text[:4000], reply_markup=confirm_kb(), parse_mode="HTML")
    # 2) Всё остальное (фото, сложные формулировки) — через Gemini
    if not ai_enabled():
        return await message.answer("⚠️ Для этого нужен GEMINI_API_KEY в переменных окружения.")
    current = await lessons_of(message.from_user.id)
    wait = await message.answer("🧠 Разбираю…")
    try:
        new, summary = await edit_schedule(current, instruction, image, mime)
    except AIBusyError:
        return await wait.edit_text(
            "⏳ Gemini сейчас перегружен (это на стороне Google). Попробуй ещё раз через минуту — "
            "просто отправь то же сообщение."
        )
    except Exception as e:
        logging.exception("AI edit failed")
        return await wait.edit_text(f"❌ Не получилось: {escape(str(e))[:300]}", parse_mode="HTML")

    await state.set_state(AiConfirm.waiting)
    await state.update_data(pending=new)
    text = (
        (f"💡 {escape(summary)}\n\n" if summary else "")
        + "<b>Изменения:</b>\n" + fmt_diff(current, new)
    )
    await wait.edit_text(text[:4000], reply_markup=confirm_kb(), parse_mode="HTML")


@router.message(F.document.file_name.lower().endswith(".json"))
async def on_json_import(message: Message, state: FSMContext, bot: Bot):
    """Импорт расписания из JSON-файла (формат как у /export)."""
    buf = io.BytesIO()
    await bot.download(message.document, destination=buf)
    try:
        raw = json.loads(buf.getvalue().decode("utf-8-sig"))
        items = raw["lessons"] if isinstance(raw, dict) else raw
        new = [
            normalize_extras(make_lesson(x["d"], x["t"], x["s"], x.get("r", ""), x.get("e", ""), x.get("note", "")), x)
            for x in items
            if 0 <= int(x["d"]) <= 5
        ]
    except Exception as e:
        return await message.answer(f"❌ Не смог прочитать файл: {escape(str(e))[:200]}", parse_mode="HTML")
    current = await lessons_of(message.from_user.id)
    await state.set_state(AiConfirm.waiting)
    await state.update_data(pending=new)
    await message.answer(
        (f"📥 В файле {len(new)} пар. Заменить ими текущее расписание?\n\n<b>Изменения:</b>\n" + fmt_diff(current, new))[:4000],
        reply_markup=confirm_kb(),
        parse_mode="HTML",
    )


@router.message(F.photo)
async def on_photo(message: Message, state: FSMContext, bot: Bot):
    buf = io.BytesIO()
    await bot.download(message.photo[-1], destination=buf)
    await run_ai_edit(message, state, message.caption or "", buf.getvalue(), "image/jpeg")


@router.message(F.document.mime_type.startswith("image/"))
async def on_image_doc(message: Message, state: FSMContext, bot: Bot):
    buf = io.BytesIO()
    await bot.download(message.document, destination=buf)
    mime = message.document.mime_type
    if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        mime = "image/jpeg"
    await run_ai_edit(message, state, message.caption or "", buf.getvalue(), mime)


@router.callback_query(F.data == "ai_ok")
async def cb_ai_ok(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pending = data.get("pending")
    if pending is None:
        return await call.answer("Это предложение уже неактуально")
    await storage.replace_lessons(call.from_user.id, pending)
    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    for part in chunks(fmt_week(await lessons_of(call.from_user.id), "✅ <b>Сохранено!</b>")):
        await call.message.answer(part, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "ai_no")
async def cb_ai_no(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(call.message.html_text + "\n\n❌ <i>Отменено</i>", parse_mode="HTML")
    await call.answer()


# Любой свободный текст = команда на изменение расписания
@router.message(StateFilter(None, AiConfirm.waiting), F.text, ~F.text.in_(MENU_BUTTONS), ~F.text.startswith("/"))
async def on_text(message: Message, state: FSMContext):
    await state.clear()
    await run_ai_edit(message, state, message.text, None, "image/jpeg")


# =====================================================================
async def on_error(event) -> bool:
    """Любая ошибка — не молчим, а говорим пользователю."""
    logging.exception("handler error", exc_info=event.exception)
    upd = event.update
    err = f"{type(event.exception).__name__}: {event.exception}"[:300]
    try:
        if upd.callback_query:
            await upd.callback_query.answer("⚠️ Ошибка, попробуй ещё раз", show_alert=False)
            chat_id = upd.callback_query.from_user.id
        elif upd.message:
            chat_id = upd.message.chat.id
        else:
            return True
        await upd.bot.send_message(chat_id, f"⚠️ Что-то пошло не так, попробуй ещё раз.\n<code>{escape(err)}</code>",
                                   parse_mode="HTML")
    except Exception:
        logging.exception("error handler failed")
    return True


def build_dispatcher(fsm_storage=None) -> Dispatcher:
    dp = Dispatcher(storage=fsm_storage) if fsm_storage else Dispatcher()
    dp.include_router(router)
    dp.errors.register(on_error)
    return dp


async def main():
    """Локальный запуск (long polling)."""
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=BOT_TOKEN)
    await bot.delete_webhook()
    dp = build_dispatcher()
    asyncio.create_task(reminder_loop(bot))
    print("🚀 Личный бот расписания запущен (polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
