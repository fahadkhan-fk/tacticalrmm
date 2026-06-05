import threading
from dataclasses import dataclass
from typing import Optional
from uuid import UUID


@dataclass
class PendingUploadChunk:
    start: int
    end: int
    data: bytes


_lock = threading.Lock()
_pending_upload_chunks: dict[UUID, PendingUploadChunk] = {}


def store_pending_upload_chunk(
    session_id: UUID, start: int, end: int, data: bytes
) -> Optional[str]:
    with _lock:
        if session_id in _pending_upload_chunks:
            return "A chunk is already pending for this session"
        _pending_upload_chunks[session_id] = PendingUploadChunk(
            start=start, end=end, data=data
        )
    return None


def pop_pending_upload_chunk(session_id: UUID) -> Optional[PendingUploadChunk]:
    with _lock:
        return _pending_upload_chunks.pop(session_id, None)


def clear_pending_upload_chunk(session_id: UUID) -> None:
    with _lock:
        _pending_upload_chunks.pop(session_id, None)
