"""Photo storage. Local disk for development; swap for S3/Blob in production (same interface)."""
import uuid
from pathlib import Path
from typing import Protocol

from app.config import get_settings


class PhotoStorage(Protocol):
    async def put(self, data: bytes, suffix: str) -> str: ...
    async def get(self, key: str) -> bytes: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("Invalid storage key")
        return p

    async def put(self, data: bytes, suffix: str) -> str:
        key = f"{uuid.uuid4().hex}{suffix}"
        self._path(key).write_bytes(data)
        return key

    async def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()


_storage: PhotoStorage | None = None


def get_storage() -> PhotoStorage:
    global _storage
    if _storage is None:
        _storage = LocalStorage(get_settings().storage_dir)
    return _storage
