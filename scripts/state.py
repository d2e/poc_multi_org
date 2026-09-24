"""Tiny key-file store for provisioning idempotency (mirrors the old scripts/.state/*.id files)."""
from __future__ import annotations

from pathlib import Path

STATE_DIR = Path(__file__).parent / ".state"


def save(name: str, value: str) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / name).write_text(f"{value.strip()}\n")


def load(name: str) -> str | None:
    p = STATE_DIR / name
    return p.read_text().strip() if p.exists() else None


def has(name: str) -> bool:
    return (STATE_DIR / name).exists()
