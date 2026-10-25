"""FSM-хранилище aiogram поверх storage.kv_* — переживает перезапуски serverless-функций."""
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

import storage


def _key(key: StorageKey) -> str:
    return f"fsm/{key.chat_id}_{key.user_id}"


class KVStorage(BaseStorage):
    async def _get(self, key: StorageKey) -> dict:
        return await storage.kv_get(_key(key)) or {"state": None, "data": {}}

    async def _put(self, key: StorageKey, rec: dict) -> None:
        if rec.get("state") is None and not rec.get("data"):
            await storage.kv_delete(_key(key))
        else:
            await storage.kv_set(_key(key), rec)

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        rec = await self._get(key)
        rec["state"] = state.state if isinstance(state, State) else state
        await self._put(key, rec)

    async def get_state(self, key: StorageKey) -> str | None:
        return (await self._get(key)).get("state")

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        rec = await self._get(key)
        rec["data"] = dict(data)
        await self._put(key, rec)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return dict((await self._get(key)).get("data") or {})

    async def close(self) -> None:
        pass
