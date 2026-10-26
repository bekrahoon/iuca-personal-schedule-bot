"""FSM-хранилище aiogram поверх storage.kv_* — переживает перезапуски serverless-функций.

Чтобы не ходить в хранилище на каждое сообщение, состояние кэшируется в памяти
тёплого инстанса, а запись делается только если что-то реально поменялось."""
import copy
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

import storage

EMPTY = {"state": None, "data": {}}


def _key(key: StorageKey) -> str:
    return f"fsm/{key.chat_id}_{key.user_id}"


class KVStorage(BaseStorage):
    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}

    async def _get(self, key: StorageKey) -> dict:
        k = _key(key)
        if k not in self._cache:
            rec = await storage.kv_get(k) or {}
            self._cache[k] = {"state": rec.get("state"), "data": rec.get("data") or {}}
        return copy.deepcopy(self._cache[k])

    async def _put(self, key: StorageKey, rec: dict) -> None:
        k = _key(key)
        rec = {"state": rec.get("state"), "data": rec.get("data") or {}}
        if self._cache.get(k) == rec:
            return  # ничего не поменялось — не трогаем хранилище
        self._cache[k] = copy.deepcopy(rec)
        if rec == EMPTY:
            await storage.kv_delete(k)
        else:
            await storage.kv_set(k, rec)

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
