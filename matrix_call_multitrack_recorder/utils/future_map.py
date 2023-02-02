""" Map that works in an async env

The idea is that we can await this map.

Code by tulir:
https://matrix.to/#/!xALORqBdeiSfgdrmUb:bpulse.org/$yjWpFraV0brev_g6Wg7Ke3i-Pc-508qtsO0RIz7sUcE?via=bpulse.org&via=matrix.org&via=envs.net
"""

import asyncio
from typing import Dict, Generic, List, Tuple, TypeVar, Union

import logbook  # type: ignore
from logbook import Logger

Key = TypeVar("Key")
Value = TypeVar("Value")

logger = Logger(__name__)
logger.level = logbook.INFO


class FutureMap(Generic[Key, Value]):
    """
    A map that allows you to wait until the key exists
    """

    def __init__(self) -> None:
        self.data: Dict[Key, Union[Value, asyncio.Future[Value]]] = {}

    async def __getitem__(self, key: Key) -> Value:
        try:
            value = self.data[key]
            if isinstance(value, asyncio.Future):
                return await asyncio.shield(value)
            return value
        except KeyError:
            self.data[key] = fut = asyncio.get_running_loop().create_future()
            return await asyncio.shield(fut)

    def __setitem__(self, key: Key, value: Value) -> None:
        try:
            existing = self.data[key]
        except KeyError:
            pass
        else:
            if isinstance(existing, asyncio.Future) and not existing.done():
                existing.set_result(value)
        self.data[key] = value

    def __delitem__(self, key: Key) -> None:
        try:
            del self.data[key]
        except KeyError:
            pass

    async def items(self) -> List[Tuple[Key, Value]]:
        return_list = []
        for key in self.data:
            try:
                conn = await asyncio.wait_for(self[key], timeout=0.5)
                return_list.append((key, conn))
            except asyncio.TimeoutError:
                logger.warning("Gave up waiting for call, task canceled")
        return return_list
