"""Durable local attachment storage; database stores only ownership metadata."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from app.core.config import settings


_MIME_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


class AttachmentStorage:
    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or settings.bugtrack.attachment_root).expanduser().resolve()

    def save(
        self,
        *,
        draft_id: uuid.UUID,
        content: bytes,
        mime_type: str,
    ) -> tuple[str, str]:
        digest = hashlib.sha256(content).hexdigest()
        suffix = _MIME_SUFFIX.get((mime_type or "").lower(), ".bin")
        relative = Path(str(draft_id)) / f"{digest}{suffix}"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix(target.suffix + f".{uuid.uuid4().hex}.tmp")
            with temporary.open("wb") as fp:
                fp.write(content)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(temporary, target)
        return relative.as_posix(), digest

    def read(self, storage_key: str) -> bytes:
        candidate = (self.root / storage_key).resolve()
        if self.root not in candidate.parents:
            raise ValueError("attachment storage key escapes configured root")
        return candidate.read_bytes()

    def delete(self, storage_key: str) -> None:
        candidate = (self.root / storage_key).resolve()
        if self.root not in candidate.parents:
            raise ValueError("attachment storage key escapes configured root")
        candidate.unlink(missing_ok=True)


attachment_storage = AttachmentStorage()

