"""Bounded TTL cache with monotonic time and copy isolation."""

from collections import OrderedDict
from copy import deepcopy
from threading import RLock
from time import monotonic
from typing import Any, Callable


class TTLCache:
    def __init__(self, capacity: int = 256, clock: Callable[[], float] = monotonic) -> None:
        self.capacity, self.clock = capacity, clock
        self.items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.lock = RLock()

    def get(self, key: str) -> Any:
        with self.lock:
            row = self.items.get(key)
            if row is None:
                return None
            if row[0] <= self.clock():
                self.items.pop(key)
                return None
            self.items.move_to_end(key)
            return deepcopy(row[1])

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self.lock:
            self.items[key] = (self.clock() + ttl, deepcopy(value))
            self.items.move_to_end(key)
            for old in list(self.items):
                if self.items[old][0] <= self.clock():
                    del self.items[old]
            while len(self.items) > self.capacity:
                self.items.popitem(last=False)

    def delete(self, key: str) -> None:
        with self.lock:
            self.items.pop(key, None)
