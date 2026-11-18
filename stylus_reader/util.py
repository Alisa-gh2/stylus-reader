"""Общие числовые помощники (без GTK)."""

from __future__ import annotations


def clamp(v, lo, hi):
    return max(lo, min(hi, v))
