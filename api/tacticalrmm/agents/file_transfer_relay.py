import json
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional
from uuid import UUID

from django.conf import settings
from redis import Redis, from_url

from tacticalrmm.constants import (
    FILE_TRANSFER_ACK_WAIT_SECONDS,
    FILE_TRANSFER_REDIS_ACK_TTL_SECONDS,
    FILE_TRANSFER_REDIS_CHUNK_TTL_SECONDS,
)


@dataclass
class PendingUploadChunk:
    start: int
    end: int
    data: bytes


_STORE_CHUNK_SCRIPT = """
local created = redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3], 'NX')
if not created then
    return 0
end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
redis.call('SET', KEYS[3], ARGV[4], 'EX', ARGV[5])
return 1
"""

_POP_CHUNK_SCRIPT = """
local meta = redis.call('GET', KEYS[1])
if not meta then
    return {false}
end
local data = redis.call('GET', KEYS[2])
if not data then
    redis.call('DEL', KEYS[1])
    return {false}
end
redis.call('DEL', KEYS[1], KEYS[2])
return {meta, data}
"""


@lru_cache(maxsize=1)
def _redis_client() -> Redis:
    return from_url(f"redis://{settings.REDIS_HOST}", decode_responses=False)


@lru_cache(maxsize=1)
def _store_chunk_redis_script():
    return _redis_client().register_script(_STORE_CHUNK_SCRIPT)


@lru_cache(maxsize=1)
def _pop_chunk_redis_script():
    return _redis_client().register_script(_POP_CHUNK_SCRIPT)


def _chunk_prefix(session_id: UUID) -> str:
    return f"upload:chunk:{session_id}/"


def _chunk_data_key_at(session_id: UUID, start: int) -> str:
    return f"{_chunk_prefix(session_id)}{start}:data"


def _chunk_meta_key_at(session_id: UUID, start: int) -> str:
    return f"{_chunk_prefix(session_id)}{start}:meta"


def _ack_key(session_id: UUID) -> str:
    return f"upload:ack:{session_id}"


def _accepted_key(session_id: UUID) -> str:
    return f"upload:accepted:{session_id}"


def _ack_event_key(session_id: UUID, expected_offset: int) -> str:
    return f"upload:ack_event:{session_id}:{expected_offset}"


def get_accepted_offset(session_id: UUID) -> Optional[int]:
    raw = _redis_client().get(_accepted_key(session_id))
    if raw is None:
        return None
    return int(raw.decode())


def get_upload_ack(session_id: UUID) -> Optional[int]:
    raw = _redis_client().get(_ack_key(session_id))
    if raw is None:
        return None
    return int(raw.decode())


def store_upload_chunk(
    session_id: UUID, start: int, end: int, data: bytes
) -> Optional[str]:
    """Store chunk at offset-indexed keys and advance accepted_offset atomically.

    Returns an error string if the chunk is already pending at this offset,
    or None on success.
    """
    meta_key = _chunk_meta_key_at(session_id, start)
    data_key = _chunk_data_key_at(session_id, start)
    accepted_key = _accepted_key(session_id)
    meta = json.dumps({"start": start, "end": end}).encode()
    chunk_ttl = FILE_TRANSFER_REDIS_CHUNK_TTL_SECONDS
    accepted_ttl = FILE_TRANSFER_REDIS_ACK_TTL_SECONDS

    created = _store_chunk_redis_script()(
        keys=[meta_key, data_key, accepted_key],
        args=[meta, data, chunk_ttl, str(end + 1), accepted_ttl],
    )
    if not created:
        return "Chunk at this offset is already pending"
    return None


def rollback_upload_chunk(session_id: UUID, start: int, prev_accepted: int) -> None:
    """Undo a store_upload_chunk call (e.g. after NATS failure).

    Deletes the chunk keys and resets accepted_offset to prev_accepted.
    """
    client = _redis_client()
    client.delete(
        _chunk_meta_key_at(session_id, start),
        _chunk_data_key_at(session_id, start),
    )
    client.set(
        _accepted_key(session_id),
        str(prev_accepted).encode(),
        ex=FILE_TRANSFER_REDIS_ACK_TTL_SECONDS,
    )


def pop_upload_chunk(session_id: UUID, start: int) -> Optional[PendingUploadChunk]:
    """Atomically fetch and delete the chunk stored at `start`.

    Executes via a Lua script so that GET meta + GET data + DEL both keys runs
    as a single Redis transaction.  A concurrent caller at the same offset will
    always see None rather than a partial result.
    """
    meta_key = _chunk_meta_key_at(session_id, start)
    data_key = _chunk_data_key_at(session_id, start)

    result = _pop_chunk_redis_script()(keys=[meta_key, data_key])

    if not result or result[0] is False or result[0] is None:
        return None

    meta_raw, data = result[0], result[1]
    meta = json.loads(meta_raw.decode())
    return PendingUploadChunk(
        start=int(meta["start"]),
        end=int(meta["end"]),
        data=data,
    )


def signal_upload_ack(session_id: UUID, committed_offset: int) -> None:
    client = _redis_client()
    ttl = FILE_TRANSFER_REDIS_ACK_TTL_SECONDS
    value = str(committed_offset).encode()
    event_key = _ack_event_key(session_id, committed_offset)
    pipe = client.pipeline()
    pipe.set(_ack_key(session_id), value, ex=ttl)
    pipe.lpush(event_key, value)
    pipe.expire(event_key, ttl)
    pipe.execute()


def wait_for_upload_ack(
    session_id: UUID, min_offset: int, timeout: Optional[float] = None
) -> Optional[int]:
    """Block until committed_offset >= min_offset, or until timeout.

    Used for two purposes:
      - Depth-1 prefetch: wait for committed to advance before accepting
        the next-next chunk (min_offset = next_start - chunk_size).
      - Complete: wait for final commit (min_offset = total_size).
    """
    if timeout is None:
        timeout = float(FILE_TRANSFER_ACK_WAIT_SECONDS)

    client = _redis_client()
    ack_key = _ack_key(session_id)
    event_key = _ack_event_key(session_id, min_offset)

    existing = client.get(ack_key)
    if existing is not None:
        offset = int(existing.decode())
        if offset >= min_offset:
            return offset

    # Clear any stale event for this offset before blocking.
    client.delete(event_key)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        wait_seconds = max(1, min(int(remaining), 5))
        result = client.blpop(event_key, timeout=wait_seconds)
        if result is not None:
            _, raw = result
            offset = int(raw.decode())
            if offset >= min_offset:
                return offset

        existing = client.get(ack_key)
        if existing is not None:
            offset = int(existing.decode())
            if offset >= min_offset:
                return offset

    existing = client.get(ack_key)
    if existing is not None:
        offset = int(existing.decode())
        if offset >= min_offset:
            return offset
    return None


def clear_upload_session_redis(session_id: UUID) -> None:
    client = _redis_client()
    client.delete(_ack_key(session_id), _accepted_key(session_id))
    for pattern in (
        f"{_chunk_prefix(session_id)}*",
        f"upload:ack_event:{session_id}:*",
    ):
        for key in client.scan_iter(match=pattern):
            client.delete(key)
