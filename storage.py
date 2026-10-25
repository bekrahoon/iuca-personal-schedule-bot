"""Хранилище личных расписаний: один JSON-файл на пользователя в папке data/."""
import json
import os
import threading
import uuid
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()

TIME_SLOTS = [
    "8:00 - 9:20",
    "9:30 - 10:50",
    "11:05 - 12:25",
    "13:10 - 14:30",
    "14:45 - 16:05",
    "16:20 - 17:40",
]


def _path(user_id: int) -> Path:
    return DATA_DIR / f"{user_id}.json"


def _default() -> dict:
    return {"lessons": [], "remind": 0}


def load(user_id: int) -> dict:
    p = _path(user_id)
    if not p.exists():
        return _default()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("lessons", [])
        data.setdefault("remind", 0)
        return data
    except Exception:
        return _default()


def save(user_id: int, data: dict) -> None:
    with _lock:
        tmp = _path(user_id).with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(_path(user_id))


def all_user_ids() -> list[int]:
    return [int(p.stem) for p in DATA_DIR.glob("*.json") if p.stem.isdigit()]


# ---------------------------------------------------------------------
# Пары
# ---------------------------------------------------------------------
def normalize_time(t: str) -> str:
    """'09:30-10:50' / '9.30 – 10.50' -> '9:30 - 10:50'."""
    t = t.replace("–", "-").replace("—", "-").replace(".", ":")
    parts = [p.strip() for p in t.split("-")]
    if len(parts) != 2:
        return t.strip()
    out = []
    for p in parts:
        h, _, m = p.partition(":")
        out.append(f"{int(h)}:{(m or '00').zfill(2)}")
    return f"{out[0]} - {out[1]}"


def start_minutes(t: str) -> int:
    try:
        h, m = t.split("-")[0].strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def make_lesson(d: int, t: str, s: str, r: str = "", e: str = "", note: str = "") -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "d": int(d),
        "t": normalize_time(t),
        "s": s.strip(),
        "r": r.strip(),
        "e": e.strip(),
        "note": note.strip(),
    }


def lesson_key(x: dict) -> tuple:
    return (x["d"], x["t"], x["s"].strip().lower())


def sort_lessons(lessons: list[dict]) -> list[dict]:
    return sorted(lessons, key=lambda x: (x["d"], start_minutes(x["t"])))


def add_lessons(user_id: int, new: list[dict]) -> int:
    """Добавляет пары, пропуская точные дубликаты. Возвращает число добавленных."""
    data = load(user_id)
    existing = {lesson_key(x) for x in data["lessons"]}
    added = 0
    for x in new:
        if lesson_key(x) not in existing:
            data["lessons"].append(x)
            existing.add(lesson_key(x))
            added += 1
    data["lessons"] = sort_lessons(data["lessons"])
    save(user_id, data)
    return added


def delete_lesson(user_id: int, lesson_id: str) -> dict | None:
    data = load(user_id)
    for i, x in enumerate(data["lessons"]):
        if x["id"] == lesson_id:
            removed = data["lessons"].pop(i)
            save(user_id, data)
            return removed
    return None


def replace_lessons(user_id: int, lessons: list[dict]) -> None:
    data = load(user_id)
    data["lessons"] = sort_lessons(lessons)
    save(user_id, data)


def set_remind(user_id: int, minutes: int) -> None:
    data = load(user_id)
    data["remind"] = minutes
    save(user_id, data)
