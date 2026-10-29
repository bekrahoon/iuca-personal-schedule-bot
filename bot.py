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

import homework as hwlib
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
BTN_HW = "📝 Домашка"
MENU_BUTTONS = {BTN_TODAY, BTN_TOMORROW, BTN_WEEK, BTN_EDIT, BTN_IUCA, BTN_REMIND, BTN_HW}


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
            [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_HW)],
            [KeyboardButton(text=BTN_EDIT), KeyboardButton(text=BTN_REMIND)],
            [KeyboardButton(text=BTN_IUCA)],
        ],
        resize_keyboard=True,
        is_persistent=False,       # меню можно свернуть значком ⌨️ в поле ввода
        one_time_keyboard=True,    # и оно само прячется после нажатия
        input_field_placeholder="📸 Фото или «перенеси ML на чт»",
    )


BOT_COMMANDS = [
    ("today", "📅 Сегодня"),
    ("tomorrow", "📆 Завтра"),
    ("week", "🗓 Неделя"),
    ("hw", "📝 Домашка"),
    ("remind", "🔔 Напоминания"),
    ("edit", "✏️ Редактировать"),
    ("iuca", "📚 Добавить из IUCA"),
    ("app", "📱 Приложение (без Telegram)"),
    ("menu", "⌨️ Показать кнопки"),
    ("export", "💾 Скачать расписание"),
]
_commands_set = False


async def ensure_commands(bot: Bot) -> None:
    """Синяя кнопка «≡ Меню» слева от поля ввода — список разделов."""
    global _commands_set
    if _commands_set:
        return
    from aiogram.types import BotCommand
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
        _commands_set = True
    except Exception:
        logging.exception("set_my_commands failed")


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


class HwAdd(StatesGroup):
    text = State()
    due = State()


router = Router()


@router.message.outer_middleware()
async def owner_only_msg(handler, event, data):
    if OWNER_ID and event.from_user and event.from_user.id != OWNER_ID:
        return
    # Только личка: в группе расписание и домашку увидели бы все участники
    if event.chat.type != "private":
        if event.text and (event.text.startswith("/") or "@" in event.text):
            try:
                await event.reply("🔒 Я работаю только в личных сообщениях — там твоё расписание и домашка видны только тебе.")
            except Exception:
                pass
        return
    return await handler(event, data)


@router.callback_query.outer_middleware()
async def owner_only_cb(handler, event, data):
    if OWNER_ID and event.from_user.id != OWNER_ID:
        return await event.answer("Это личный бот 🙂")
    msg = event.message
    if msg is not None and getattr(msg, "chat", None) is not None and msg.chat.type != "private":
        return await event.answer("🔒 Только в личных сообщениях", show_alert=True)
    return await handler(event, data)


# =====================================================================
# СТАРТ / ПРОСМОТР
# =====================================================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await ensure_commands(message.bot)
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
        + "📝 «Домашка» — записать задание. Быстро: <i>«дз ML практическая 4 к четвергу»</i>\n"
        + "📚 «Добавить из IUCA» — найти предмет в общем расписании (любой курс) и добавить его пары.\n"
        "✏️ «Редактировать» — удалить/добавить пары вручную.\n\n"
        "/export — скачать расписание JSON, /help — подсказка",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await cmd_start(message, state)


@router.message(Command("app"))
async def cmd_app(message: Message, state: FSMContext):
    """Личная ссылка на PWA — расписание как приложение на главном экране."""
    import webapp
    host = (os.environ.get("APP_HOST") or os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")
            or "iuca-personal-schedule-bot.vercel.app")
    url = webapp.app_link(host, message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Открыть приложение", url=url)]])
    await message.answer(
        "📱 <b>Расписание как приложение</b>\n\n"
        "1. Открой ссылку в Chrome (⋮ → «Открыть в Chrome», если откроется внутри Telegram).\n"
        "2. Нажми «Установить» (или ⋮ → «Добавить на главный экран»).\n\n"
        "Дальше открываешь иконку на рабочем столе — без Telegram. "
        "Ссылка личная, никому не пересылай.",
        reply_markup=kb, parse_mode="HTML",
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("⌨️ Кнопки внизу. Свернуть — значок ⌨️ в поле ввода, а все разделы есть ещё в «≡ Меню» слева.",
                         reply_markup=main_kb())


@router.message(F.text.in_({BTN_TODAY, BTN_TOMORROW}))
@router.message(Command("today", "tomorrow"))
async def show_today(message: Message, state: FSMContext):
    await state.clear()
    day = now_kg().date()
    if message.text in (BTN_TOMORROW, "/tomorrow") or (message.text or "").startswith("/tomorrow"):
        day += timedelta(days=1)
    await message.answer_photo(await day_photo(message.from_user.id, day), reply_markup=day_nav_kb(day))


def day_nav_kb(day, expanded: bool = False) -> InlineKeyboardMarkup:
    """Одна строка кнопок; дни недели раскрываются по 🗓."""
    today = now_kg().date()
    prev_d, next_d = day - timedelta(days=1), day + timedelta(days=1)
    if prev_d.weekday() == 6:
        prev_d -= timedelta(days=1)
    if next_d.weekday() == 6:
        next_d += timedelta(days=1)
    mid = "🔄" if day == today else "Сегодня"
    rows = [[
        InlineKeyboardButton(text=f"◀️ {prev_d.strftime('%d.%m')}", callback_data=f"dd:{prev_d.isoformat()}"),
        InlineKeyboardButton(text=mid, callback_data=f"dd:{today.isoformat()}"),
        InlineKeyboardButton(text=f"{next_d.strftime('%d.%m')} ▶️", callback_data=f"dd:{next_d.isoformat()}"),
        InlineKeyboardButton(text="✖️" if expanded else "🗓", callback_data=f"ddx:{day.isoformat()}:{0 if expanded else 1}"),
    ]]
    if expanded:
        monday = render.week_start(day)
        rows.append([
            InlineKeyboardButton(text=f"• {n} •" if monday + timedelta(days=i) == day else n,
                                 callback_data=f"dd:{(monday + timedelta(days=i)).isoformat()}")
            for i, n in enumerate(DAYS_SHORT)
        ])
        rows.append([InlineKeyboardButton(text="📝 Текстом", callback_data=f"ddt:{day.isoformat()}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("ddx:"))
async def cb_day_expand(call: CallbackQuery):
    from datetime import date as _date
    _, iso, flag = call.data.split(":")
    try:
        await call.message.edit_reply_markup(reply_markup=day_nav_kb(_date.fromisoformat(iso), expanded=flag == "1"))
    except Exception:
        pass
    await call.answer()


async def day_photo(uid: int, day) -> BufferedInputFile:
    now = now_kg()
    now_min = now.hour * 60 + now.minute if day == now.date() else None
    data = await storage.load(uid)
    png = await asyncio.to_thread(render.render_day, data["lessons"], day, now_min, hwlib.due_on(data.get("hw", []), day))
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
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️", callback_data=f"wk:{offset - 1}"),
        InlineKeyboardButton(text=mid, callback_data="wk:0"),
        InlineKeyboardButton(text="▶️", callback_data=f"wk:{offset + 1}"),
        InlineKeyboardButton(text="📝", callback_data="wktxt"),
    ]])


async def week_photo(uid: int, offset: int) -> BufferedInputFile:
    today = now_kg().date()
    monday = render.week_start(today) + timedelta(weeks=offset)
    png = await asyncio.to_thread(render.render_week, await lessons_of(uid), monday, today)
    return BufferedInputFile(png, f"week_{monday.isoformat()}.png")


@router.message(F.text == BTN_WEEK)
@router.message(Command("week"))
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
@router.message(Command("edit"))
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
@router.message(Command("iuca"))
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
@router.message(Command("remind"))
async def remind_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(**await remind_view(message.from_user.id))


REMIND_OPTIONS = (0, 5, 10, 15, 30, 45, 60)


def mins_label(m: int) -> str:
    if m == 0:
        return "Выкл"
    return "1 час" if m == 60 else f"{m} мин"


def mins_text(m: int) -> str:
    if m == 0:
        return "выкл"
    return "за 1 час" if m == 60 else f"за {m} мин"


async def remind_view(uid: int) -> dict:
    data = await storage.load(uid)
    cur = data["remind"]
    lessons = [x for x in data["lessons"] if not x.get("date")]
    on = sum(1 for x in lessons if storage.remind_minutes(data, x))
    custom = [x for x in lessons if "rm" in x or x.get("mute")]
    b = InlineKeyboardBuilder()
    for m in REMIND_OPTIONS:
        b.button(text=("✅ " if m == cur else "") + mins_label(m), callback_data=f"rem:{m}")
    b.adjust(4, 3)
    b.row(InlineKeyboardButton(text="⏰ Настроить каждую пару", callback_data=f"rsd:{min(now_kg().weekday(), 5)}"))

    text = "🔔 <b>Напоминания</b>\n\n"
    text += f"По умолчанию: <b>{mins_text(cur)}</b> до пары\n"
    if custom:
        text += "\nСвоё время у пар:\n" + "\n".join(
            f"• {DAYS_SHORT[x['d']]} {x['t'].split(' ')[0]} {escape(x['s'])} — <b>{mins_text(storage.remind_minutes(data, x))}</b>"
            for x in sorted(custom, key=lambda x: (x["d"], start_minutes(x["t"])))
        ) + "\n"
    text += f"\nНапоминаю о <b>{on}</b> из {len(lessons)} пар в неделю."
    text += "\n\n<i>«⏰ Настроить каждую пару» — своё время для конкретной пары: например ML в Пн за 1 час, а ML в Пт за 10 мин.</i>"
    return {"text": text, "reply_markup": b.as_markup(), "parse_mode": "HTML"}


@router.callback_query(F.data.startswith("rem:"))
async def cb_remind(call: CallbackQuery):
    m = int(call.data.split(":")[1])
    await storage.set_remind(call.from_user.id, m)
    await _edit(call, await remind_view(call.from_user.id))
    await call.answer("По умолчанию: " + mins_text(m))


async def remind_pick_view(uid: int, d: int) -> dict:
    data = await storage.load(uid)
    lessons = sorted([x for x in data["lessons"] if x["d"] == d and not x.get("date")],
                     key=lambda x: start_minutes(x["t"]))
    b = InlineKeyboardBuilder()
    for x in lessons:
        m = storage.remind_minutes(data, x)
        own = "rm" in x or x.get("mute")
        icon = "🔕" if not m else ("⏰" if own else "🔔")
        val = mins_label(m) if own or not m else f"{mins_label(m)} (по умолч.)"
        b.row(InlineKeyboardButton(text=f"{icon} {x['t'].split(' ')[0]} {x['s'][:22]} — {val}",
                                   callback_data=f"rl:{d}:{x['id']}"))
    b.row(*[
        InlineKeyboardButton(text=f"• {n} •" if i == d else n, callback_data=f"rsd:{i}")
        for i, n in enumerate(DAYS_SHORT)
    ])
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="rback"))
    if lessons:
        text = (f"⏰ <b>{DAYS_RU[d]}</b>\nНажми на пару, чтобы выбрать, за сколько о ней напомнить.\n\n"
                f"🔔 — по умолчанию ({mins_text(data['remind'])}) · ⏰ — своё время · 🔕 — без напоминания")
    else:
        text = f"⏰ <b>{DAYS_RU[d]}</b> — пар нет."
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


@router.callback_query(F.data.startswith("rl:"))
async def cb_remind_lesson(call: CallbackQuery):
    _, d, lid = call.data.split(":")
    data = await storage.load(call.from_user.id)
    x = next((y for y in data["lessons"] if y["id"] == lid), None)
    if not x:
        return await call.answer("Пара не найдена")
    own = x.get("rm", 0 if x.get("mute") else None)
    b = InlineKeyboardBuilder()
    for m in REMIND_OPTIONS:
        b.button(text=("✅ " if own == m else "") + mins_label(m), callback_data=f"rls:{d}:{lid}:{m}")
    b.adjust(4, 3)
    b.row(InlineKeyboardButton(text=("✅ " if own is None else "") + f"По умолчанию ({mins_text(data['remind'])})",
                               callback_data=f"rls:{d}:{lid}:-1"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"rsd:{d}"))
    await _edit(call, {
        "text": f"⏰ <b>{DAYS_RU[int(d)]} {x['t'].split(' ')[0]} — {escape(x['s'])}</b>\n"
                f"За сколько напомнить именно об этой паре?",
        "reply_markup": b.as_markup(), "parse_mode": "HTML"})
    await call.answer()


@router.callback_query(F.data.startswith("rls:"))
async def cb_remind_lesson_set(call: CallbackQuery):
    _, d, lid, m = call.data.split(":")
    m = int(m)
    x = await storage.set_lesson_remind(call.from_user.id, lid, None if m < 0 else m)
    await _edit(call, await remind_pick_view(call.from_user.id, int(d)))
    if x:
        await call.answer(f"{x['s'][:25]}: " + ("по умолчанию" if m < 0 else mins_text(m)))
    else:
        await call.answer("Пара не найдена")


@router.callback_query(F.data.startswith("rtog:") | F.data.startswith("rall:") | F.data.startswith("rsub"))
async def cb_remind_old(call: CallbackQuery):
    """Кнопки из старых версий меню."""
    await _edit(call, await remind_view(call.from_user.id))
    await call.answer("Меню обновилось 🙂")


@router.callback_query(F.data == "rback")
async def cb_remind_back(call: CallbackQuery):
    await _edit(call, await remind_view(call.from_user.id))
    await call.answer()


_sent_mem: dict[str, set] = {}


async def check_reminders(bot: Bot) -> int:
    """Отправляет напоминания о парах и вечернюю сводку по домашке.
    Можно вызывать с любой периодичностью: уже отправленное не дублируется.
    Обычный вызов (нечего отправлять) не трогает хранилище вообще — план читается из памяти."""
    now = now_kg()
    cur = now.hour * 60 + now.minute
    today = now.date().isoformat()
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    plan = await storage.get_plan()

    todo: list[tuple[str, int, str, InlineKeyboardMarkup | None]] = []  # (ключ, uid, текст, кнопки)
    for uid_s, p in plan.items():
        uid = int(uid_s)
        if now.weekday() <= 5:
            for x in lessons_on(p.get("lessons", []), now.date()):
                if x.get("_cancel"):
                    continue
                m = storage.remind_minutes(p, x)
                left = start_minutes(x["t"]) - cur
                if m and 0 < left <= m:
                    todo.append((f"{uid}:{x['id']}", uid, f"🔔 <b>Через {_dur(left)}</b>\n" + fmt_card(x), None))
        if now.hour >= 20:
            due = [h for h in p.get("hw", []) if h.get("due") == tomorrow]
            if due:
                lines = [f"• <b>{escape(h.get('s') or 'Без предмета')}</b> — {escape(h['text'])}" for h in due]
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📝 Открыть домашку", callback_data="hwlist")]])
                todo.append((f"{uid}:hw", uid, "📝 <b>На завтра задано:</b>\n\n" + "\n".join(lines), kb))

    mem = _sent_mem.setdefault(today, set())
    for old in [k for k in _sent_mem if k != today]:
        _sent_mem.pop(old)
    todo = [t for t in todo if t[0] not in mem]
    if not todo:
        return 0
    fresh = set(await storage.mark_sent([k for k, *_ in todo], today))
    mem.update(k for k, *_ in todo)
    sent_count = 0
    for key, uid, text, kb in todo:
        if key not in fresh:
            continue
        try:
            await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
            sent_count += 1
        except Exception:
            logging.exception("reminder send failed")
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
    wait = await message.answer("🧠 Разбираю…" + (" фото обычно 5–10 сек" if image else ""))

    async def typing():
        while True:
            try:
                await message.bot.send_chat_action(message.chat.id, "typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    typer = asyncio.create_task(typing())
    try:
        new, summary = await edit_schedule(current, instruction, image, mime)
    except AIBusyError:
        typer.cancel()
        return await wait.edit_text(
            "⏳ Gemini сейчас перегружен (это на стороне Google). Попробуй ещё раз через минуту — "
            "просто отправь то же сообщение."
        )
    except Exception as e:
        typer.cancel()
        logging.exception("AI edit failed")
        return await wait.edit_text(f"❌ Не получилось: {escape(str(e))[:300]}", parse_mode="HTML")
    typer.cancel()

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


# =====================================================================
# ДОМАШКА
# =====================================================================
def hw_list_kb(hw: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    open_items = sorted([h for h in hw if not h.get("done")], key=hwlib.sort_key)
    for h in open_items[:20]:
        label = f"{(h.get('s') or '—')[:14]}: {h['text'][:22]}"
        b.row(
            InlineKeyboardButton(text=f"✅ {label}", callback_data=f"hwd:{h['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"hwx:{h['id']}"),
        )
    done_n = sum(1 for h in hw if h.get("done"))
    b.row(InlineKeyboardButton(text="➕ Добавить", callback_data="hwadd"))
    if done_n:
        b.row(InlineKeyboardButton(text=f"📋 Выполненные ({done_n})", callback_data="hwdone"))
    return b.as_markup()


async def hw_view(uid: int) -> dict:
    hw = (await storage.load(uid)).get("hw", [])
    text = hwlib.fmt_list(hw, now_kg().date())
    if any(not h.get("done") for h in hw):
        text += "\n\n<i>Нажми ✅, когда сделаешь. Быстро добавить: напиши «дз ML практическая 4 к четвергу»</i>"
    return {"text": text[:4000], "reply_markup": hw_list_kb(hw), "parse_mode": "HTML"}


async def hw_save(uid: int, item: dict) -> None:
    data = await storage.load(uid)
    data["hw"] = hwlib.prune(data.get("hw", []) + [item], now_kg().date())
    await storage.save(uid, data)


async def hw_update(uid: int, hid: str, fn) -> dict | None:
    data = await storage.load(uid)
    for h in data.get("hw", []):
        if h["id"] == hid:
            fn(data, h)
            await storage.save(uid, data)
            return h
    return None


def hw_saved_text(item: dict) -> str:
    mark, lbl = hwlib.due_label(item.get("due"), now_kg().date())
    subj = escape(item["s"]) if item.get("s") else "Без предмета"
    return f"✅ Записал\n\n{mark} <b>{subj}</b> · <i>{lbl}</i>\n{escape(item['text'])}"


def hw_saved_kb(hid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📅 Другой срок", callback_data=f"hwdue:{hid}"),
        InlineKeyboardButton(text="↩️ Отменить", callback_data=f"hwx:{hid}"),
    ], [InlineKeyboardButton(text="📝 Вся домашка", callback_data="hwlist")]])


def due_choice_kb(lessons: list[dict], subject: str | None, prefix: str) -> InlineKeyboardMarkup:
    today = now_kg().date()
    b = InlineKeyboardBuilder()
    if subject:
        nd = hwlib.next_lesson_date(lessons, subject, today)
        if nd:
            b.row(InlineKeyboardButton(text=f"📚 К след. паре — {DAYS_SHORT[nd.weekday()] if nd.weekday() < 6 else 'Вс'} {nd.strftime('%d.%m')}",
                                       callback_data=f"{prefix}:{nd.isoformat()}"))
    tm = today + timedelta(days=1)
    wk = today + timedelta(days=7)
    b.row(
        InlineKeyboardButton(text=f"Завтра {tm.strftime('%d.%m')}", callback_data=f"{prefix}:{tm.isoformat()}"),
        InlineKeyboardButton(text=f"Через неделю {wk.strftime('%d.%m')}", callback_data=f"{prefix}:{wk.isoformat()}"),
    )
    b.row(InlineKeyboardButton(text="Без срока", callback_data=f"{prefix}:none"))
    return b.as_markup()


@router.message(F.text == BTN_HW)
@router.message(Command("hw"))
async def hw_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(**await hw_view(message.from_user.id))


@router.callback_query(F.data == "hwlist")
async def cb_hw_list(call: CallbackQuery):
    await call.answer()
    await call.message.answer(**await hw_view(call.from_user.id))


@router.callback_query(F.data.startswith("hwd:"))
async def cb_hw_done(call: CallbackQuery):
    def done(data, h):
        h["done"] = True
        h["done_at"] = now_kg().date().isoformat()
    h = await hw_update(call.from_user.id, call.data.split(":")[1], done)
    await _edit(call, await hw_view(call.from_user.id))
    await call.answer(f"✅ Сделано: {h['text'][:40]}" if h else "Уже нет в списке")


@router.callback_query(F.data.startswith("hwx:"))
async def cb_hw_delete(call: CallbackQuery):
    hid = call.data.split(":")[1]
    h = await hw_update(call.from_user.id, hid, lambda data, h: data["hw"].remove(h))
    if call.message.text and call.message.text.startswith("✅ Записал"):
        await call.message.edit_text("↩️ Отменено — задание удалено")
    else:
        await _edit(call, await hw_view(call.from_user.id))
    await call.answer("🗑 Удалено" if h else "Уже удалено")


@router.callback_query(F.data == "hwdone")
async def cb_hw_done_list(call: CallbackQuery):
    hw = (await storage.load(call.from_user.id)).get("hw", [])
    b = InlineKeyboardBuilder()
    for h in sorted([h for h in hw if h.get("done")], key=hwlib.sort_key, reverse=True)[:15]:
        b.row(InlineKeyboardButton(text=f"↩️ Вернуть: {(h.get('s') or '—')[:12]}: {h['text'][:22]}", callback_data=f"hwu:{h['id']}"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="hwback"))
    await _edit(call, {"text": hwlib.fmt_list(hw, now_kg().date(), show_done=True)[:4000],
                       "reply_markup": b.as_markup(), "parse_mode": "HTML"})
    await call.answer()


@router.callback_query(F.data.startswith("hwu:"))
async def cb_hw_undo(call: CallbackQuery):
    def undo(data, h):
        h["done"] = False
        h.pop("done_at", None)
    await hw_update(call.from_user.id, call.data.split(":")[1], undo)
    await _edit(call, await hw_view(call.from_user.id))
    await call.answer("Вернул в список")


@router.callback_query(F.data == "hwback")
async def cb_hw_back(call: CallbackQuery):
    await _edit(call, await hw_view(call.from_user.id))
    await call.answer()


# --- добавление по шагам: предмет -> текст -> срок
@router.callback_query(F.data == "hwadd")
async def cb_hw_add(call: CallbackQuery, state: FSMContext):
    await state.clear()
    lessons = await lessons_of(call.from_user.id)
    b = InlineKeyboardBuilder()
    for i, name in enumerate(hwlib.subjects(lessons)):
        b.button(text=name[:30], callback_data=f"hws:{i}")
    b.button(text="Другое / без предмета", callback_data="hws:-1")
    b.adjust(2)
    await call.message.answer("📝 По какому предмету задание?", reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("hws:"))
async def cb_hw_subject(call: CallbackQuery, state: FSMContext):
    i = int(call.data.split(":")[1])
    data = await state.get_data()
    subs = hwlib.subjects(await lessons_of(call.from_user.id))
    subject = subs[i] if 0 <= i < len(subs) else None
    pending_text = data.get("hw_text")
    if pending_text:  # предмет выбирали для уже написанного «дз ...»
        lessons = await lessons_of(call.from_user.id)
        due = data.get("hw_due")
        if not due and subject:
            nd = hwlib.next_lesson_date(lessons, subject, now_kg().date())
            due = nd.isoformat() if nd else None
        item = hwlib.make(subject, pending_text, due, now_kg().date())
        await hw_save(call.from_user.id, item)
        await state.clear()
        await call.message.edit_text(hw_saved_text(item), reply_markup=hw_saved_kb(item["id"]), parse_mode="HTML")
        return await call.answer()
    await state.set_state(HwAdd.text)
    await state.update_data(hw_subject=subject)
    await call.message.edit_text(f"📝 <b>{escape(subject or 'Без предмета')}</b>\nЧто задали? Напиши текстом:", parse_mode="HTML")
    await call.answer()


@router.message(HwAdd.text, F.text, ~F.text.in_(MENU_BUTTONS))
async def hw_add_text(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(hw_text=message.text.strip())
    await state.set_state(HwAdd.due)
    lessons = await lessons_of(message.from_user.id)
    await message.answer("📅 К какому сроку? Выбери или напиши дату (например <code>30.10</code> или <code>к пятнице</code>):",
                         reply_markup=due_choice_kb(lessons, data.get("hw_subject"), "hwnew"), parse_mode="HTML")


async def _hw_finish(uid: int, state: FSMContext, due: str | None) -> dict:
    data = await state.get_data()
    item = hwlib.make(data.get("hw_subject"), data.get("hw_text", ""), due, now_kg().date())
    await hw_save(uid, item)
    await state.clear()
    return item


@router.callback_query(HwAdd.due, F.data.startswith("hwnew:"))
async def cb_hw_new_due(call: CallbackQuery, state: FSMContext):
    v = call.data.split(":", 1)[1]
    item = await _hw_finish(call.from_user.id, state, None if v == "none" else v)
    await call.message.edit_text(hw_saved_text(item), reply_markup=hw_saved_kb(item["id"]), parse_mode="HTML")
    await call.answer()


@router.message(HwAdd.due, F.text, ~F.text.in_(MENU_BUTTONS))
async def hw_add_due_text(message: Message, state: FSMContext):
    d, _ = parse_date_any(message.text)
    if not d:
        return await message.answer("Не понял дату 🤔 Напиши, например, <code>30.10</code>, <code>завтра</code> или <code>к пятнице</code>",
                                    parse_mode="HTML")
    item = await _hw_finish(message.from_user.id, state, d.isoformat())
    await message.answer(hw_saved_text(item), reply_markup=hw_saved_kb(item["id"]), parse_mode="HTML")


def parse_date_any(text: str):
    from quick_edit import parse_date
    return parse_date(text, now_kg().date())


# --- смена срока у уже записанного
@router.callback_query(F.data.startswith("hwdue:"))
async def cb_hw_change_due(call: CallbackQuery):
    hid = call.data.split(":")[1]
    data = await storage.load(call.from_user.id)
    h = next((h for h in data.get("hw", []) if h["id"] == hid), None)
    if not h:
        return await call.answer("Задание уже удалено")
    await call.message.edit_reply_markup(reply_markup=due_choice_kb(data["lessons"], h.get("s"), f"hwset:{hid}"))
    await call.answer()


@router.callback_query(F.data.startswith("hwset:"))
async def cb_hw_set_due(call: CallbackQuery):
    _, hid, v = call.data.split(":", 2)
    h = await hw_update(call.from_user.id, hid, lambda data, h: h.update(due=None if v == "none" else v))
    if not h:
        return await call.answer("Задание уже удалено")
    await call.message.edit_text(hw_saved_text(h), reply_markup=hw_saved_kb(hid), parse_mode="HTML")
    await call.answer("Срок изменён")


# --- быстрое добавление текстом: «дз ML практическая 4 к четвергу»
@router.message(StateFilter(None, AiConfirm.waiting), F.text.func(lambda t: bool(hwlib.PREFIX_RE.match(t))))
async def hw_quick(message: Message, state: FSMContext):
    await state.clear()
    lessons = await lessons_of(message.from_user.id)
    p = hwlib.parse_quick(message.text, lessons, now_kg().date())
    if not p or not p["text"]:
        await state.set_state(HwAdd.text)
        await state.update_data(hw_subject=p["s"] if p else None)
        return await message.answer("📝 Что задали? Напиши текст задания:")
    if not p["s"]:
        await state.update_data(hw_text=p["text"], hw_due=p["due"])
        b = InlineKeyboardBuilder()
        for i, name in enumerate(hwlib.subjects(lessons)):
            b.button(text=name[:30], callback_data=f"hws:{i}")
        b.button(text="Без предмета", callback_data="hws:-1")
        b.adjust(2)
        return await message.answer(f"📝 «{escape(p['text'])}» — по какому предмету?", reply_markup=b.as_markup(),
                                    parse_mode="HTML")
    item = hwlib.make(p["s"], p["text"], p["due"], now_kg().date())
    await hw_save(message.from_user.id, item)
    await message.answer(hw_saved_text(item), reply_markup=hw_saved_kb(item["id"]), parse_mode="HTML")


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
