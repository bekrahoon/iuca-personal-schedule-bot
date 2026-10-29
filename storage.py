"""
Хранилище личных расписаний.
- Если задан BLOB_READ_WRITE_TOKEN (Vercel) — данные лежат в приватном Vercel Blob.
- Иначе — JSON-файлы в папке data/ (локальный запуск).
"""
import copy
import json
import os
import uuid
from contextvars import ContextVar
from pathlib import Path

BLOB_ENV_VARS = ("BLOB_READ_WRITE_TOKEN", "VERCEL_BLOB_READ_WRITE_TOKEN", "BLOB_STORE_ID")
USE_BLOB = any(os.environ.get(k) for k in BLOB_ENV_VARS)

if os.environ.get("DATA_DIR"):
    DATA_DIR = Path(os.environ["DATA_DIR"])
elif os.environ.get("VERCEL"):
    DATA_DIR = Path("/tmp/data")  # на Vercel писать можно только в /tmp (и он не сохраняется)
else:
    DATA_DIR = Path(__file__).parent / "data"

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
# В пределах одного запроса чтения кэшируются (каждое обращение к Blob — это сетевой запрос).
# ---------------------------------------------------------------------
_memo: ContextVar[dict | None] = ContextVar("kv_memo", default=None)
_MISSING = object()


def begin_request() -> None:
    """Вызывать в начале каждого входящего запроса — новый пустой кэш чтений."""
    _memo.set({})


async def kv_get(key: str) -> dict | None:
    memo = _memo.get()
    if memo is not None and key in memo:
        v = memo[key]
        return None if v is _MISSING else copy.deepcopy(v)
    v = await _kv_get_raw(key)
    if memo is not None:
        memo[key] = _MISSING if v is None else copy.deepcopy(v)
    return v


async def _kv_get_raw(key: str) -> dict | None:
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
    memo = _memo.get()
    if memo is not None:
        memo[key] = copy.deepcopy(value)
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
    memo = _memo.get()
    if memo is not None:
        memo[key] = _MISSING
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
    return {"lessons": [], "remind": 0, "sent": {}, "hw": [], "push": [], "wake": 0, "tg": True}


async def load(user_id: int) -> dict:
    data = await kv_get(f"users/{user_id}") or _default()
    for k, v in _default().items():
        data.setdefault(k, v)
    return data


async def save(user_id: int, data: dict) -> None:
    data["lessons"] = prune_past(data.get("lessons", []))
    data.pop("sent", None)  # старое поле — отметки об отправке теперь в sent.json
    await kv_set(f"users/{user_id}", data)
    await update_plan(user_id, data)


# ---------------------------------------------------------------------
# План напоминаний — один общий файл для всех пользователей.
# Проверка напоминаний (каждые несколько минут) читает только его, и то из памяти,
# пока он не устарел: так бесплатного лимита хранилища хватает с запасом.
# ---------------------------------------------------------------------
import time as _time

PLAN_KEY = "remind_plan"
SENT_KEY = "remind_sent"
_plan_cache: dict = {"t": 0.0, "plan": None}


def _plan_entry(data: dict) -> dict:
    return {
        "remind": data.get("remind", 0),
        "rs": data.get("remind_by_subject", {}),
        "wake": data.get("wake", 0),
        "tg": data.get("tg", True),
        "push": data.get("push", []),
        "lessons": [
            {k: x[k] for k in ("id", "d", "t", "s", "r", "e", "note", "mute", "rm", "date", "once") if k in x}
            for x in data.get("lessons", [])
        ],
        "hw": [
            {"s": h.get("s", ""), "text": h["text"], "due": h["due"]}
            for h in data.get("hw", []) if not h.get("done") and h.get("due")
        ],
    }


async def _read_plan() -> dict:
    plan = await kv_get(PLAN_KEY)
    if plan is None:  # первый запуск — собираем из всех пользователей
        plan = {}
        for k in await kv_keys("users/"):
            tail = k.rsplit("/", 1)[-1]
            if tail.isdigit():
                plan[tail] = _plan_entry(await load(int(tail)))
        await kv_set(PLAN_KEY, plan)
    return plan


async def get_plan(max_age: float = 1800) -> dict:
    if _plan_cache["plan"] is None or _time.time() - _plan_cache["t"] > max_age:
        _plan_cache["plan"] = await _read_plan()
        _plan_cache["t"] = _time.time()
    return _plan_cache["plan"]


async def update_plan(user_id: int, data: dict) -> None:
    entry = _plan_entry(data)
    plan = await get_plan(max_age=300)
    if plan.get(str(user_id)) == entry:
        return
    plan = await _read_plan()  # свежая копия, чтобы не затереть чужие изменения
    plan[str(user_id)] = entry
    await kv_set(PLAN_KEY, plan)
    _plan_cache["plan"], _plan_cache["t"] = plan, _time.time()


async def mark_sent(keys: list[str], today: str) -> list[str]:
    """Отмечает напоминания отправленными. Возвращает только те, что ещё не были отправлены."""
    sent = await kv_get(SENT_KEY) or {}
    done = set(sent.get(today, []))
    fresh = [k for k in keys if k not in done]
    if fresh:
        await kv_set(SENT_KEY, {today: sorted(done | set(fresh))})
    return fresh


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


def subject_key(s: str) -> str:
    import re as _re
    return _re.sub(r"\(.*?\)", "", s).strip().lower()


def remind_minutes(data_or_plan: dict, lesson: dict) -> int:
    """За сколько минут напоминать об этой паре: 0 — не напоминать.
    Своё время у конкретной пары (lesson["rm"]) > общая настройка."""
    if "rm" in lesson:
        return int(lesson["rm"])
    if lesson.get("mute"):
        return 0
    return int(data_or_plan.get("remind", 0) or 0)


async def set_lesson_remind(user_id: int, lesson_id: str, minutes: int | None) -> dict | None:
    """minutes=None — «как у всех»; 0 — не напоминать."""
    data = await load(user_id)
    for x in data["lessons"]:
        if x["id"] == lesson_id:
            x.pop("mute", None)
            if minutes is None:
                x.pop("rm", None)
            else:
                x["rm"] = minutes
            await save(user_id, data)
            return x
    return None


async def set_day_remind(user_id: int, d: int, minutes: int | None) -> None:
    data = await load(user_id)
    for x in data["lessons"]:
        if x["d"] == d:
            x.pop("mute", None)
            if minutes is None:
                x.pop("rm", None)
            else:
                x["rm"] = minutes
    await save(user_id, data)


async def set_subject_remind(user_id: int, subject: str, minutes: int | None) -> None:
    """minutes=None — вернуть «как у всех»."""
    data = await load(user_id)
    rs = data.setdefault("remind_by_subject", {})
    k = subject_key(subject)
    if minutes is None:
        rs.pop(k, None)
    else:
        rs[k] = minutes
    await save(user_id, data)


async def toggle_mute(user_id: int, lesson_id: str) -> dict | None:
    data = await load(user_id)
    for x in data["lessons"]:
        if x["id"] == lesson_id:
            if x.get("mute"):
                x.pop("mute")
            else:
                x["mute"] = True
            await save(user_id, data)
            return x
    return None


async def set_mute_day(user_id: int, d: int, mute: bool) -> None:
    data = await load(user_id)
    for x in data["lessons"]:
        if x["d"] == d:
            if mute:
                x["mute"] = True
            else:
                x.pop("mute", None)
    await save(user_id, data)


# ---------------------------------------------------------------------
# Разовые изменения на конкретную дату
#   lesson["once"] = [{"date": "2026-10-27", "r": "Актовый зал"}, {"date": "...", "cancel": True}, ...]
#   lesson["date"] = "2026-10-27"  -> разовая пара, только в этот день
# ---------------------------------------------------------------------
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta, timezone as _timezone

KG_TZ = _timezone(_timedelta(hours=6))


def today_kg() -> _date:
    return _datetime.now(KG_TZ).date()


def _iso(v) -> str | None:
    try:
        return _date.fromisoformat(str(v).strip()[:10]).isoformat()
    except Exception:
        return None


def normalize_extras(lesson: dict, raw: dict) -> dict:
    """Переносит в lesson поля date/once/mute из сырых данных (AI, импорт), с проверкой."""
    d = _iso(raw.get("date")) if raw.get("date") else None
    if d:
        lesson["date"] = d
        lesson["d"] = _date.fromisoformat(d).weekday()
    once = []
    for o in raw.get("once") or []:
        od = _iso(o.get("date"))
        if not od:
            continue
        entry = {"date": od}
        if o.get("cancel"):
            entry["cancel"] = True
        for k in ("t", "r", "note"):
            if o.get(k):
                entry[k] = normalize_time(o[k]) if k == "t" else str(o[k]).strip()
        if len(entry) > 1:
            once.append(entry)
    if once:
        lesson["once"] = sorted(once, key=lambda e: e["date"])
    if raw.get("mute"):
        lesson["mute"] = True
    if raw.get("rm") is not None:
        try:
            lesson["rm"] = int(raw["rm"])
        except (TypeError, ValueError):
            pass
    return lesson


def prune_past(lessons: list[dict]) -> list[dict]:
    """Убирает прошедшие разовые пары и прошедшие разовые изменения."""
    today = today_kg().isoformat()
    out = []
    for x in lessons:
        if x.get("date") and x["date"] < today:
            continue
        if x.get("once"):
            x["once"] = [o for o in x["once"] if o["date"] >= today]
            if not x["once"]:
                x.pop("once")
        out.append(x)
    return out


def lessons_on(lessons: list[dict], day: _date) -> list[dict]:
    """Фактические пары на конкретную дату: с учётом разовых изменений, отмен и разовых пар.
    Отменённые возвращаются с флагом _cancel, изменённые — с флагом _changed."""
    iso = day.isoformat()
    out = []
    for x in lessons:
        if x["d"] != day.weekday():
            continue
        if x.get("date") and x["date"] != iso:
            continue
        y = dict(x)
        for o in x.get("once", []):
            if o["date"] == iso:
                if o.get("cancel"):
                    y["_cancel"] = True
                for k in ("t", "r", "note"):
                    if o.get(k):
                        y[k] = o[k]
                        y["_changed"] = True
        out.append(y)
    return sorted(out, key=lambda x: start_minutes(x["t"]))


def next_date_for_weekday(d: int, base: _date | None = None) -> _date:
    base = base or today_kg()
    return base + _timedelta(days=(d - base.weekday()) % 7)
