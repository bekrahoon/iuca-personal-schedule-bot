"""Редактирование расписания через Gemini: по фото и/или текстовой команде."""
import json
import os

from google import genai
from google.genai import types

from storage import TIME_SLOTS, make_lesson

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
_client: genai.Client | None = None


def ai_enabled() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


SYSTEM = f"""Ты редактируешь личное недельное расписание студента университета IUCA (Кыргызстан).
Дни: 0=Понедельник, 1=Вторник, 2=Среда, 3=Четверг, 4=Пятница, 5=Суббота.
Стандартные пары IUCA: {", ".join(TIME_SLOTS)}. Если время на фото чуть отличается — бери как на фото.

Тебе дают ТЕКУЩЕЕ расписание (JSON) и фото и/или команду пользователя.
Правила:
- Если на фото полное недельное расписание и пользователь не сказал иначе — ЗАМЕНИ всё расписание тем, что на фото.
- Если на фото общее расписание многих групп — бери только то, что относится к группе/предметам, которые назвал пользователь. Если непонятно, чьи пары брать, верни текущее расписание без изменений и задай вопрос в summary.
- Если на фото/в команде только изменение (перенос, отмена, новая пара, замена кабинета) — примени ТОЛЬКО его, остальное сохрани как есть.
- Сохраняй поле id у пар, которые остались (даже если их изменили). У новых пар id — пустая строка.
- Не выдумывай кабинет и преподавателя: если их не видно — пустая строка.
- Всегда возвращай ПОЛНОЕ итоговое расписание.
- summary — 1-2 коротких предложения по-русски: что именно изменилось (или вопрос, если что-то неясно)."""

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
                },
                "required": ["d", "t", "s"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["lessons", "summary"],
}


async def edit_schedule(
    current: list[dict],
    instruction: str = "",
    image_bytes: bytes | None = None,
    media_type: str = "image/jpeg",
) -> tuple[list[dict], str]:
    """Возвращает (новое_расписание, краткое_описание_изменений)."""
    parts: list = []
    if image_bytes:
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type=media_type))
    parts.append(
        "ТЕКУЩЕЕ РАСПИСАНИЕ:\n"
        + json.dumps(current, ensure_ascii=False)
        + "\n\nКОМАНДА ПОЛЬЗОВАТЕЛЯ: "
        + (instruction.strip() or "(нет — просто используй фото)")
    )

    resp = await _get_client().aio.models.generate_content(
        model=MODEL,
        contents=parts,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_json_schema=SCHEMA,
            temperature=0.1,
        ),
    )
    raw = json.loads(resp.text)
    old_ids = {x["id"] for x in current}

    result = []
    for x in raw.get("lessons", []):
        try:
            if not 0 <= int(x["d"]) <= 5:
                continue
            lesson = make_lesson(x["d"], x["t"], x["s"], x.get("r", ""), x.get("e", ""), x.get("note", ""))
        except Exception:
            continue
        if x.get("id") in old_ids:
            lesson["id"] = x["id"]
        result.append(lesson)
    return result, raw.get("summary", "")
