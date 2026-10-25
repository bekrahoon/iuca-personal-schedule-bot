"""
Хранилище личных расписаний.
- Если задан BLOB_READ_WRITE_TOKEN (Vercel) — данные лежат в приватном Vercel Blob.
- Иначе — JSON-файлы в папке data/ (локальный запуск).
"""
import json
import os
import uuid
from pathlib import Path

USE_BLOB = bool(os.environ.get("BLOB_READ_WRITE_TOKEN"))
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
if not USE_BLOB:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

TIME_SLOTS = [
    "8:00 - 9:20",
    "9:30 - 10:50",
    "11:05 - 12:25",
    "13:10 - 14:30",
    "14:45 - 16:05",
    "16:20 - 17:40",
]


# ---------------------------------------------------------------------
# Низкоуровневое key-value: key -> dict
# ---------------------------------------------------------------------
async def kv_get(key: str) -> dict | None:
    if USE_BLOB:
        from vercel.blob import BlobNotFoundError, get_blob
        try:
            res = await get_blob(f"{key}.json", access="private")
            return json.loads(res.body)
        except BlobNotFoundError:
            return None
    p = DATA_DIR / f"{key}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


async def kv_set(key: str, value: dict) -> None:
    raw = json.dumps(value, ensure_ascii=False).encode()
    if USE_BLOB:
        from vercel.blob import put_blob
        await put_blob(
            f"{key}.json", raw, access="private", content_type="application/json",
            allow_overwrite=True, cache_control_max_age=60,
        )
        return
    p = DATA_DIR / f"{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(raw)
    tmp.replace(p)


async def kv_delete(key: str) -> None:
    if USE_BLOB:
        from vercel.blob import BlobNotFoundError, delete_blob
        try:
            await delete_blob(f"{key}.json")
        except BlobNotFoundError:
            pass
        return
    (DATA_DIR / f"{key}.json").unlink(missing_ok=True)


async def kv_keys(prefix: str) -> list[str]:
    if USE_BLOB:
        from vercel.blob import query_blobs
        out = []
        async for b in query_blobs(prefix=prefix):
            if b.pathname.endswith(".json"):
                out.append(b.pathname[:-5])
        return out
    base = DATA_DIR / prefix
    return [f"{prefix}{p.stem}" for p in base.glob("*.json")] if base.exists() else []


# ---------------------------------------------------------------------
# Пользовательские данные
# ---------------------------------------------------------------------
def _default() -> dict:
    return {"lessons": [], "remind": 0, "sent": {}}


async def load(user_id: int) -> dict:
    data = await kv_get(f"users/{user_id}") or _default()
    for k, v in _default().items():
        data.setdefault(k, v)
    return data


async def save(user_id: int, data: dict) -> None:
    await kv_set(f"users/{user_id}", data)


async def all_user_ids() -> list[int]:
    ids = []
    for k in await kv_keys("users/"):
        tail = k.rsplit("/", 1)[-1]
        if tail.isdigit():
            ids.append(int(tail))
    return ids


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
        "r": (r or "").strip(),
        "e": (e or "").strip(),
        "note": (note or "").strip(),
    }


def lesson_key(x: dict) -> tuple:
    return (x["d"], x["t"], x["s"].strip().lower())


def sort_lessons(lessons: list[dict]) -> list[dict]:
    return sorted(lessons, key=lambda x: (x["d"], start_minutes(x["t"])))


async def add_lessons(user_id: int, new: list[dict]) -> int:
    """Добавляет пары, пропуская точные дубликаты. Возвращает число добавленных."""
    data = await load(user_id)
    existing = {lesson_key(x) for x in data["lessons"]}
    added = 0
    for x in new:
        if lesson_key(x) not in existing:
            data["lessons"].append(x)
            existing.add(lesson_key(x))
            added += 1
    data["lessons"] = sort_lessons(data["lessons"])
    await save(user_id, data)
    return added


async def delete_lesson(user_id: int, lesson_id: str) -> dict | None:
    data = await load(user_id)
    for i, x in enumerate(data["lessons"]):
        if x["id"] == lesson_id:
            removed = data["lessons"].pop(i)
            await save(user_id, data)
            return removed
    return None


async def replace_lessons(user_id: int, lessons: list[dict]) -> None:
    data = await load(user_id)
    data["lessons"] = sort_lessons(lessons)
    await save(user_id, data)


async def set_remind(user_id: int, minutes: int) -> None:
    data = await load(user_id)
    data["remind"] = minutes
    await save(user_id, data)
