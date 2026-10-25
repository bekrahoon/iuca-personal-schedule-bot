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

import storage
from ai_editor import ai_enabled, edit_schedule
from storage import TIME_SLOTS, make_lesson, start_minutes

# =====================================================================
# НАСТРОЙКИ
# =====================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

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


def fmt_lesson(x: dict, idx: int | None = None) -> str:
    head = f"<b>{idx}. ⏰ {x['t']}</b>" if idx else f"<b>⏰ {x['t']}</b>"
    lines = [head, f"📚 {escape(x['s'])}"]
    if x.get("r"):
        lines.append(f"🚪 Ауд.: <code>{escape(x['r'])}</code>")
    if x.get("e"):
        lines.append(f"👤 {escape(x['e'])}")
    if x.get("note"):
        lines.append(f"📝 <i>{escape(x['note'])}</i>")
    return "\n".join(lines)


async def fmt_day(user_id: int, d: int) -> str:
    if d > 5:
        return "🎉 <b>Воскресенье</b> — выходной!"
    items = [x for x in await lessons_of(user_id) if x["d"] == d]
    header = f"📅 <b>{DAYS_RU[d]}</b>\n"
    if not items:
        return header + "\n😴 Пар нет!"
    return header + "\n" + "\n\n".join(fmt_lesson(x, i) for i, x in enumerate(items, 1))


def fmt_week(lessons: list[dict], title: str = "🗓 <b>Моё расписание</b>") -> str:
    blocks = []
    for d in range(6):
        items = [x for x in lessons if x["d"] == d]
        if not items:
            continue
        rows = [f"📌 <b>{DAYS_RU[d].upper()}</b>"]
        for x in items:
            room = f" (<b>{escape(x['r'])}</b>)" if x.get("r") else ""
            rows.append(f"• <code>{x['t']}</code> — {escape(x['s'])}{room}")
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
            lines.append(f"➕ {DAYS_SHORT[x['d']]} {x['t']} — {escape(x['s'])}")
        elif any(o.get(k) != x.get(k) for k in ("d", "t", "s", "r", "e", "note")):
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
    d = now_kg().weekday()
    if message.text == BTN_TOMORROW:
        d = (d + 1) % 7
    await message.answer(await fmt_day(message.from_user.id, d), reply_markup=days_kb(min(d, 5)), parse_mode="HTML")


@router.callback_query(F.data.startswith("day:"))
async def cb_day(call: CallbackQuery):
    d = int(call.data.split(":")[1])
    try:
        await call.message.edit_text(await fmt_day(call.from_user.id, d), reply_markup=days_kb(d), parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.message(F.text == BTN_WEEK)
async def show_week(message: Message, state: FSMContext):
    await state.clear()
    for part in chunks(fmt_week(await lessons_of(message.from_user.id))):
        await message.answer(part, parse_mode="HTML")


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
    cur = (await storage.load(message.from_user.id))["remind"]
    b = InlineKeyboardBuilder()
    for m in (0, 5, 10, 15, 30):
        label = "Выкл" if m == 0 else f"за {m} мин"
        b.button(text=("✅ " if m == cur else "") + label, callback_data=f"rem:{m}")
    b.adjust(5)
    await message.answer("🔔 Напоминать перед парой:", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("rem:"))
async def cb_remind(call: CallbackQuery):
    m = int(call.data.split(":")[1])
    await storage.set_remind(call.from_user.id, m)
    await call.message.edit_text("🔕 Напоминания выключены" if m == 0 else f"🔔 Буду напоминать за {m} мин до пары")
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
        for x in data["lessons"]:
            left = start_minutes(x["t"]) - cur
            if x["d"] == now.weekday() and 0 < left <= m and x["id"] not in sent_today:
                try:
                    await bot.send_message(uid, f"🔔 Через {left} мин:\n\n" + fmt_lesson(x), parse_mode="HTML")
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
    if not ai_enabled():
        return await message.answer("⚠️ Для этого нужен GEMINI_API_KEY в переменных окружения.")
    current = await lessons_of(message.from_user.id)
    wait = await message.answer("🧠 Разбираю…")
    try:
        new, summary = await edit_schedule(current, instruction, image, mime)
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
def build_dispatcher(fsm_storage=None) -> Dispatcher:
    dp = Dispatcher(storage=fsm_storage) if fsm_storage else Dispatcher()
    dp.include_router(router)
    return dp


async def main():
    """Локальный запуск (long polling)."""
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=BOT_TOKEN)
    await bot.delete_webhook()
    dp = build_dispatcher()
    asyncio.create_task(reminder_loop(bot))
    print("🚀 Личный бот расписания запущен (polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
