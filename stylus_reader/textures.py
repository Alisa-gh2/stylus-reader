"""LRU-кэш декодированных GdkTexture с бюджетом по пикселям (P4).

Бюджет считается в байтах (ширина*высота*4), а не в числе страниц: тяжёлый
409-dpi кадр (4499x3477 ≈ 62 МБ) не должен вытеснять десяток preview.
Декод PNG 409 dpi стоит 140–160 мс, поэтому попадание в кэш = показ без декода.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

BYTES_PER_PIXEL = 4
DEFAULT_BUDGET = 112 * 1024 * 1024  # ~96–128 МБ по требованию P4


def final_key(page: int, dpi: int) -> tuple:
    return ("final", int(page), int(dpi))


def preview_key(page: int) -> tuple:
    return ("preview", int(page))


class TextureCache:
    """Потокобезопасный LRU текстур; ключи — кортежи из main.py."""

    def __init__(self, budget: int = DEFAULT_BUDGET):
        self.budget = int(budget)
        self._lock = threading.Lock()
        self._items: "OrderedDict[tuple, object]" = OrderedDict()
        self._sizes: dict[tuple, int] = {}
        self._bytes = 0
        self.hits = 0

    def get(self, key):
        with self._lock:
            tex = self._items.get(key)
            if tex is None:
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return tex

    def put(self, key, texture) -> None:
        if texture is None:
            return
        try:
            cost = int(texture.get_width()) * int(texture.get_height()) * BYTES_PER_PIXEL
        except Exception:
            return
        if cost > self.budget:
            return  # одиночная текстура больше бюджета — не кэшируем вовсе
        with self._lock:
            if key in self._items:
                self._bytes -= self._sizes.pop(key, 0)
                self._items.pop(key, None)
            while self._items and self._bytes + cost > self.budget:
                old, _tex = self._items.popitem(last=False)
                self._bytes -= self._sizes.pop(old, 0)
            self._items[key] = texture
            self._sizes[key] = cost
            self._bytes += cost

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._sizes.clear()
            self._bytes = 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "count": len(self._items),
                "bytes": self._bytes,
                "budget": self.budget,
                "hits": self.hits,
            }
