"""PWA «Расписание МУЦА»: то же личное расписание, но как приложение на телефоне, без захода в Telegram.

Вход: в боте команда /app присылает личную ссылку вида /app#k=<id>.<подпись>.
Приложение запоминает ключ и дальше открывается с главного экрана сразу.
Данные — те же, что у бота (storage.load / storage.save), так что бот и приложение всегда синхронны.
"""
import hashlib
import hmac
import os
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

import homework as hwlib
import storage
from storage import TIME_SLOTS, lessons_on, make_lesson, normalize_extras, sort_lessons, today_kg

WEB_DIR = Path(__file__).parent / "web"
router = APIRouter()


# ---------------------------------------------------------------------
# Ключ доступа
# ---------------------------------------------------------------------
def _secret() -> bytes:
    s = os.environ.get("APP_SECRET") or os.environ.get("WEBHOOK_SECRET") or ""
    return s.encode()


def make_key(uid: int) -> str:
    sig = hmac.new(_secret(), f"app:{uid}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{uid}.{sig}"


def app_link(host: str, uid: int) -> str:
    return f"https://{host}/app#k={make_key(uid)}"


def _uid(auth: str | None) -> int:
    if not _secret() or not auth:
        raise HTTPException(401, "Нет ключа")
    key = auth.removeprefix("Bearer ").strip()
    uid, _, _sig = key.partition(".")
    if not uid.isdigit() or not hmac.compare_digest(key, make_key(int(uid))):
        raise HTTPException(401, "Неверный ключ — получи новую ссылку командой /app в боте")
    owner = os.environ.get("OWNER_ID")
    if owner and owner != uid:
        raise HTTPException(403)
    return int(uid)


# ---------------------------------------------------------------------
# Статика PWA
# ---------------------------------------------------------------------
_STATIC = {
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "text/javascript",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-maskable.png": "image/png",
    "apple-touch-icon.png": "image/png",
}


@router.get("/app", include_in_schema=False)
async def app_index():
    return FileResponse(WEB_DIR / "index.html", media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@router.get("/app/", include_in_schema=False)
async def app_index_slash():
    return RedirectResponse("/app")


@router.get("/app/{name}", include_in_schema=False)
async def app_static(name: str):
    if name not in _STATIC:
        raise HTTPException(404)
    headers = {"Cache-Control": "no-cache", "Service-Worker-Allowed": "/app"} if name == "sw.js" else \
        {"Cache-Control": "public, max-age=86400"}
    return FileResponse(WEB_DIR / name, media_type=_STATIC[name], headers=headers)


# ---------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------
def _public(x: dict) -> dict:
    return {k: v for k, v in x.items() if k in ("id", "d", "t", "s", "r", "e", "note", "date", "once", "mute", "rm",
                                                   "_cancel", "_changed")}


async def _state(uid: int) -> dict:
    data = await storage.load(uid)
    today = today_kg()
    lessons = data["lessons"]
    days = []
    for i in range(-1, 15):  # вчера + две недели вперёд — хватает и офлайн
        day = today + timedelta(days=i)
        days.append({"date": day.isoformat(), "lessons": [_public(x) for x in lessons_on(lessons, day)]})
    hw = sorted(hwlib.prune(data.get("hw", []), today), key=hwlib.sort_key)
    return {
        "today": today.isoformat(),
        "days": days,
        "lessons": [_public(x) for x in sort_lessons(lessons)],
        "hw": hw,
        "subjects": hwlib.subjects(lessons),
        "slots": TIME_SLOTS,
        "remind": data.get("remind", 0),
        "wake": data.get("wake", 0),
        "tg": data.get("tg", True),
        "push": [x.get("endpoint", "") for x in data.get("push", [])],
    }


@router.get("/api/app/data")
async def get_data(authorization: str | None = Header(default=None)):
    return await _state(_uid(authorization))


# ----- домашка -----
class HwIn(BaseModel):
    text: str
    s: str | None = None
    due: str | None = None   # "YYYY-MM-DD", "next" (к следующей паре) или None — разобрать из текста


@router.post("/api/app/hw")
async def hw_add(body: HwIn, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    data = await storage.load(uid)
    today = today_kg()
    text, subj, due = body.text.strip(), body.s, body.due
    if not text:
        raise HTTPException(400, "Пустое задание")
    if subj is None and due is None:
        parsed = hwlib.parse_quick(text if hwlib.PREFIX_RE.match(text) else f"дз {text}", data["lessons"], today)
        if parsed:
            subj, due, text = parsed["s"], parsed["due"], parsed["text"] or text
    elif due == "next":
        nd = hwlib.next_lesson_date(data["lessons"], subj or "", today) if subj else None
        due = nd.isoformat() if nd else None
    item = hwlib.make(subj, text, due, today)
    data["hw"] = hwlib.prune(data.get("hw", []) + [item], today)
    await storage.save(uid, data)
    return await _state(uid)


async def _hw_change(uid: int, hid: str, fn) -> dict:
    data = await storage.load(uid)
    for h in data.get("hw", []):
        if h["id"] == hid:
            fn(data, h)
            await storage.save(uid, data)
            return await _state(uid)
    raise HTTPException(404, "Задание не найдено")


@router.post("/api/app/hw/{hid}/toggle")
async def hw_toggle(hid: str, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)

    def fn(_data, h):
        h["done"] = not h.get("done")
        if h["done"]:
            h["done_at"] = today_kg().isoformat()
        else:
            h.pop("done_at", None)
    return await _hw_change(uid, hid, fn)


@router.delete("/api/app/hw/{hid}")
async def hw_delete(hid: str, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    return await _hw_change(uid, hid, lambda data, h: data["hw"].remove(h))


# ----- пары -----
class LessonIn(BaseModel):
    d: int
    t: str
    s: str
    r: str = ""
    e: str = ""


@router.post("/api/app/lesson")
async def lesson_add(body: LessonIn, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    if not 0 <= body.d <= 5 or not body.s.strip():
        raise HTTPException(400, "Нужны день (Пн–Сб) и предмет")
    await storage.add_lessons(uid, [make_lesson(body.d, body.t, body.s, body.r, body.e)])
    return await _state(uid)


@router.delete("/api/app/lesson/{lid}")
async def lesson_delete(lid: str, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    if not await storage.delete_lesson(uid, lid):
        raise HTTPException(404, "Пара не найдена")
    return await _state(uid)


# ----- правка фото/текстом (как в боте: сначала показываем изменения, потом «Применить») -----
@router.post("/api/app/edit")
async def edit(
    text: str = Form(default=""),
    image: UploadFile | None = File(default=None),
    authorization: str | None = Header(default=None),
):
    uid = _uid(authorization)
    from bot import fmt_diff
    current = (await storage.load(uid))["lessons"]
    img = await image.read() if image is not None else None
    if img and len(img) > 8 * 1024 * 1024:
        raise HTTPException(413, "Фото слишком большое (до 8 МБ)")
    if not img and not text.strip():
        raise HTTPException(400, "Напиши, что поменять, или прикрепи фото")

    result = None
    if not img:
        from quick_edit import quick_edit
        result = quick_edit(text, current, today_kg())
    if result is None:
        from ai_editor import AIBusyError, ai_enabled, edit_schedule
        if not ai_enabled():
            raise HTTPException(503, "Для этого нужен GEMINI_API_KEY")
        try:
            result = await edit_schedule(current, text, img, (image.content_type if image else None) or "image/jpeg")
        except AIBusyError:
            raise HTTPException(503, "Gemini сейчас перегружен — попробуй через минуту")
    new, summary = result
    return {"summary": summary, "diff": fmt_diff(current, new), "pending": new}


class ApplyIn(BaseModel):
    lessons: list[dict]


@router.post("/api/app/apply")
async def apply(body: ApplyIn, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    clean = []
    for x in body.lessons:
        try:
            lesson = make_lesson(x["d"], x["t"], x["s"], x.get("r", ""), x.get("e", ""), x.get("note", ""))
            if not 0 <= lesson["d"] <= 5:
                continue
            normalize_extras(lesson, x)
            if x.get("id"):
                lesson["id"] = str(x["id"])[:16]
            clean.append(lesson)
        except Exception:
            continue
    await storage.replace_lessons(uid, clean)
    return await _state(uid)


# ----- будильник и уведомления на телефон -----
@router.get("/api/app/push-key")
async def push_key():
    import push
    return {"key": push.public_key() if push.enabled() else None}


class SubIn(BaseModel):
    endpoint: str
    keys: dict


@router.post("/api/app/push")
async def push_subscribe(body: SubIn, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    if not body.endpoint.startswith("https://") or not {"p256dh", "auth"} <= set(body.keys):
        raise HTTPException(400, "Неверная подписка")
    data = await storage.load(uid)
    subs = [x for x in data.get("push", []) if x.get("endpoint") != body.endpoint]
    subs.append({"endpoint": body.endpoint, "keys": {"p256dh": body.keys["p256dh"], "auth": body.keys["auth"]}})
    data["push"] = subs[-5:]  # не больше 5 устройств
    await storage.save(uid, data)
    return await _state(uid)


class EndpointIn(BaseModel):
    endpoint: str


@router.post("/api/app/push/off")
async def push_unsubscribe(body: EndpointIn, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    data = await storage.load(uid)
    data["push"] = [x for x in data.get("push", []) if x.get("endpoint") != body.endpoint]
    await storage.save(uid, data)
    return await _state(uid)


@router.post("/api/app/push/test")
async def push_test(body: EndpointIn, authorization: str | None = Header(default=None)):
    import push
    uid = _uid(authorization)
    data = await storage.load(uid)
    subs = [x for x in data.get("push", []) if x.get("endpoint") == body.endpoint]
    if not subs:
        raise HTTPException(404, "Этот телефон не подписан — включи уведомления ещё раз")
    ok, dead = await push.send(subs, {"title": "⏰ Проверка будильника",
                                      "body": "Так будут выглядеть напоминания о парах", "tag": "test", "alarm": True},
                               ttl=60)
    await push.drop_dead(uid, dead)
    if not ok:
        raise HTTPException(502, "Сервис уведомлений не принял сообщение — выключи и включи уведомления")
    return {"ok": True}


class SettingsIn(BaseModel):
    remind: int | None = None
    wake: int | None = None
    tg: bool | None = None


@router.post("/api/app/settings")
async def settings(body: SettingsIn, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    data = await storage.load(uid)
    if body.remind is not None:
        data["remind"] = max(0, min(int(body.remind), 180))
    if body.wake is not None:
        data["wake"] = max(0, min(int(body.wake), 180))
    if body.tg is not None:
        data["tg"] = bool(body.tg)
    await storage.save(uid, data)
    return await _state(uid)
