"""Точка входа для Vercel: Telegram webhook + эндпоинт напоминаний."""
import hmac
import logging
import os

from aiogram import Bot
from aiogram.types import BotCommand, Update
from fastapi import FastAPI, Header, HTTPException, Request

import bot as botmod
from fsm_storage import KVStorage

logging.basicConfig(level=logging.INFO)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
dp = botmod.build_dispatcher(KVStorage())
app = FastAPI()


def _check(secret: str | None) -> None:
    if not WEBHOOK_SECRET or not secret or not hmac.compare_digest(secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=403)


@app.get("/")
async def health():
    import storage
    missing = [k for k in ("BOT_TOKEN", "WEBHOOK_SECRET", "BLOB_READ_WRITE_TOKEN", "GEMINI_API_KEY") if not os.environ.get(k)]
    return {"ok": not missing[:3] or missing == ["GEMINI_API_KEY"], "bot": "iuca-personal-schedule",
            "missing_env": missing, "storage": "vercel-blob" if storage.USE_BLOB else "local (НЕ сохранится!)"}


@app.post("/api/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    _check(x_telegram_bot_api_secret_token)
    update = Update.model_validate(await request.json(), context={"bot": None})
    async with Bot(token=botmod.BOT_TOKEN) as bot:
        await dp.feed_update(bot, update)
    return {"ok": True}


@app.get("/api/remind")
async def remind():
    """Дёргается GitHub Actions по расписанию. Безопасно вызывать сколько угодно раз."""
    async with Bot(token=botmod.BOT_TOKEN) as bot:
        sent = await botmod.check_reminders(bot)
    return {"ok": True, "sent": sent}


@app.get("/api/setup")
async def setup(request: Request, key: str = ""):
    """Один раз: регистрирует webhook в Telegram. /api/setup?key=<WEBHOOK_SECRET>"""
    _check(key)
    host = request.headers.get("x-forwarded-host") or request.url.hostname
    url = f"https://{host}/api/webhook"
    async with Bot(token=botmod.BOT_TOKEN) as bot:
        await bot.set_webhook(url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
        await bot.set_my_commands([
            BotCommand(command="start", description="Меню"),
            BotCommand(command="export", description="Скачать расписание JSON"),
        ])
        info = await bot.get_webhook_info()
    return {"ok": True, "webhook": info.url, "pending": info.pending_update_count}


@app.get("/api/selftest")
async def selftest(key: str = ""):
    """Проверка хранилища: запись сразу же читается обратно (нет устаревшего кэша)."""
    _check(key)
    import time
    import storage
    results = []
    for i in range(3):
        v = {"n": i, "t": time.time()}
        await storage.kv_set("selftest/x", v)
        got = await storage.kv_get("selftest/x")
        results.append(got == v)
    await storage.kv_delete("selftest/x")
    return {"ok": all(results), "results": results, "blob": storage.USE_BLOB}
