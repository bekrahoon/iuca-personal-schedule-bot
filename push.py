"""Web Push для PWA: напоминания и утренний будильник приходят уведомлением на телефон, без Telegram.

VAPID-ключ не нужно заводить отдельно: он детерминированно выводится из WEBHOOK_SECRET
(или APP_SECRET), поэтому после деплоя всё работает без новых переменных окружения.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os

_key = None


def _secret() -> bytes:
    return (os.environ.get("APP_SECRET") or os.environ.get("WEBHOOK_SECRET") or "").encode()


def enabled() -> bool:
    return bool(_secret())


def _private_key():
    global _key
    if _key is None:
        from cryptography.hazmat.primitives.asymmetric import ec
        n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551  # порядок группы P-256
        seed = hmac.new(_secret(), b"vapid-key-v1", hashlib.sha256).digest()
        d = int.from_bytes(seed, "big") % (n - 1) + 1
        _key = ec.derive_private_key(d, ec.SECP256R1())
    return _key


def public_key() -> str:
    """applicationServerKey для pushManager.subscribe — base64url несжатой точки."""
    from cryptography.hazmat.primitives import serialization
    raw = _private_key().public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _send_one(sub: dict, payload: dict, ttl: int) -> int:
    """Возвращает HTTP-статус сервиса push (201 — доставлено, 404/410 — подписка умерла)."""
    from py_vapid import Vapid02
    from pywebpush import WebPushException, webpush
    try:
        r = webpush(
            subscription_info=sub,
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=Vapid02(private_key=_private_key()),
            vapid_claims={"sub": "mailto:" + os.environ.get("PUSH_CONTACT", "noreply@example.com")},
            ttl=ttl,
            timeout=10,
            headers={"Urgency": "high"},
        )
        return r.status_code
    except WebPushException as e:
        code = e.response.status_code if e.response is not None else 0
        logging.warning("push failed %s: %s", code, e)
        return code
    except Exception:
        logging.exception("push failed")
        return 0


async def send(subs: list[dict], payload: dict, ttl: int = 1800) -> tuple[int, list[str]]:
    """Шлёт на все подписки. Возвращает (сколько доставлено, endpoint'ы мёртвых подписок)."""
    if not subs or not enabled():
        return 0, []
    codes = await asyncio.gather(*(asyncio.to_thread(_send_one, s, payload, ttl) for s in subs))
    ok = sum(1 for c in codes if 200 <= c < 300)
    dead = [s["endpoint"] for s, c in zip(subs, codes) if c in (404, 410)]
    return ok, dead


async def drop_dead(uid: int, endpoints: list[str]) -> None:
    if not endpoints:
        return
    import storage
    data = await storage.load(uid)
    data["push"] = [s for s in data.get("push", []) if s.get("endpoint") not in endpoints]
    await storage.save(uid, data)
