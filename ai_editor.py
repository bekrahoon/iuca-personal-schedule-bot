"""Редактирование расписания через Gemini: по фото и/или текстовой команде."""
import asyncio
import json
import logging
import os

from storage import TIME_SLOTS, make_lesson, normalize_extras, today_kg

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
# Запасные модели — если основная перегружена (503) или упёрлась в лимит (429)
FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash-lite").split(",")
    if m.strip()
]
_client = None  # google.genai импортируется лениво — он тяжёлый, а нужен только для фото/команд

DAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


class AIBusyError(Exception):
    """Все модели Gemini сейчас перегружены."""


def ai_enabled() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


SYSTEM = f"""Ты редактируешь личное расписание студента университета IUCA (Кыргызстан, UTC+6).
Дни недели (поле d): 0=Понедельник, 1=Вторник, 2=Среда, 3=Четверг, 4=Пятница, 5=Суббота.
Стандартные пары IUCA: {", ".join(TIME_SLOTS)}. Если время на фото чуть отличается — бери как на фото.

Расписание — НЕДЕЛЬНОЕ (повторяется каждую неделю). Плюс бывают РАЗОВЫЕ изменения на конкретную дату.

Тебе дают сегодняшнюю дату, ТЕКУЩЕЕ расписание (JSON) и фото и/или команду пользователя.

Постоянные изменения (меняют неделю навсегда):
- Полное недельное расписание на фото (и пользователь не сказал иначе) — ЗАМЕНИ всё расписание.
- Общее расписание многих групп — бери только пары нужной группы/предметов. Если непонятно чьи — верни текущее без изменений и задай вопрос в summary.
- «перенеси X на четверг», «X теперь в B201» без конкретной даты — меняй поля d/t/r самой пары.

РАЗОВЫЕ изменения (если упомянута конкретная дата: «27 октября», «завтра», «в эту пятницу», «на следующей неделе в среду»):
- НЕ меняй основные поля d/t/s/r/e пары.
- Добавь запись в массив once этой пары: {{"date": "YYYY-MM-DD", ...только то, что меняется}}:
  r — новая аудитория, t — новое время, note — пояснение, cancel=true — пара отменена в этот день.
- Если в один день меняются несколько пар (например, «в 13:10 и 14:45») — добавь once каждой подходящей паре именно на ту дату.
  Сверь день недели даты с полем d: изменение относится только к парам, которые реально идут в этот день.
- Разовая дополнительная пара (только в одну дату) — новая пара с полем date="YYYY-MM-DD" и d = день недели этой даты.
- Год не указан — бери ближайшую такую дату, не раньше сегодняшней.

Общие правила:
- Сохраняй поле id у пар, которые остались (даже если их изменили). У новых пар id — пустая строка.
- Сохраняй существующие записи once и поле date, если пользователь их не трогает.
- Не выдумывай кабинет и преподавателя: если их не видно — пустая строка.
- Всегда возвращай ПОЛНОЕ итоговое расписание.
- summary — 1-2 коротких предложения по-русски: что именно изменилось и на какие даты (или вопрос, если что-то неясно)."""

ONCE_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string", "description": "YYYY-MM-DD"},
        "cancel": {"type": "boolean"},
        "t": {"type": "string"},
        "r": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["date"],
}

SCHEMA = {
    "type": "object",
    "properties": {
        "lessons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "d": {"type": "integer"},
                    "t": {"type": "string", "description": "Время, формат '9:30 - 10:50'"},
                    "s": {"type": "string", "description": "Название предмета"},
                    "r": {"type": "string", "description": "Аудитория"},
                    "e": {"type": "string", "description": "Преподаватель"},
                    "note": {"type": "string"},
                    "date": {"type": "string", "description": "Только для разовой пары: YYYY-MM-DD"},
                    "once": {"type": "array", "items": ONCE_SCHEMA},
                },
                "required": ["d", "t", "s"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["lessons", "summary"],
}


def _is_busy(e: Exception) -> bool:
    from google.genai import errors
    code = getattr(e, "code", None)
    return isinstance(e, errors.ServerError) or code in (429, 500, 503, 504) or "UNAVAILABLE" in str(e)


async def _generate(parts: list, config):
    last: Exception | None = None
    for i, model in enumerate([MODEL, MODEL, *FALLBACK_MODELS]):
        try:
            return await asyncio.wait_for(
                _get_client().aio.models.generate_content(model=model, contents=parts, config=config),
                timeout=25,
            )
        except asyncio.TimeoutError as e:
            last = e
            logging.warning("Gemini %s timeout", model)
        except Exception as e:
            if not _is_busy(e):
                raise
            last = e
            logging.warning("Gemini %s busy: %s", model, e)
            if i == 0:
                await asyncio.sleep(2)
    raise AIBusyError(str(last))


async def edit_schedule(
    current: list[dict],
    instruction: str = "",
    image_bytes: bytes | None = None,
    media_type: str = "image/jpeg",
) -> tuple[list[dict], str]:
    """Возвращает (новое_расписание, краткое_описание_изменений)."""
    from google.genai import types
    today = today_kg()
    parts: list = []
    if image_bytes:
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type=media_type))
    parts.append(
        f"СЕГОДНЯ: {today.isoformat()} ({DAYS_RU[today.weekday()]})\n\n"
        "ТЕКУЩЕЕ РАСПИСАНИЕ:\n"
        + json.dumps(current, ensure_ascii=False)
        + "\n\nКОМАНДА ПОЛЬЗОВАТЕЛЯ: "
        + (instruction.strip() or "(нет — просто используй фото)")
    )

    resp = await _generate(parts, types.GenerateContentConfig(
        system_instruction=SYSTEM,
        response_mime_type="application/json",
        response_json_schema=SCHEMA,
        temperature=0.1,
    ))
    raw = json.loads(resp.text)
    old_by_id = {x["id"]: x for x in current}

    result = []
    for x in raw.get("lessons", []):
        try:
            if not 0 <= int(x["d"]) <= 5:
                continue
            lesson = make_lesson(x["d"], x["t"], x["s"], x.get("r", ""), x.get("e", ""), x.get("note", ""))
            normalize_extras(lesson, x)
        except Exception:
            continue
        old = old_by_id.get(x.get("id"))
        if old:
            lesson["id"] = old["id"]
            if old.get("mute"):
                lesson["mute"] = True  # сохраняем выключенное напоминание
        result.append(lesson)
    return result, raw.get("summary", "")
