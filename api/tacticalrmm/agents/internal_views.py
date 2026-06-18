import datetime as dt
import logging
import time

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone as djangotime
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from tacticalrmm.constants import (
    FILE_TRANSFER_ACK_CHECKPOINT_BYTES,
    FILE_TRANSFER_ACK_WAIT_SECONDS,
    FILE_TRANSFER_CHUNK_SIZE_MAX,
    FILE_TRANSFER_DL_DEPTH_WAIT_SECONDS,
    FILE_TRANSFER_PIPELINE_DEPTH,
    FILE_TRANSFER_SESSION_TTL_HOURS,
    FileTransferOperation,
    FileTransferStatus,
)
from tacticalrmm.helpers import notify_error

from .file_transfer_relay import (
    get_accepted_offset,
    get_download_ack,
    get_download_offered_offset,
    get_upload_ack,
    pop_upload_chunk,
    signal_upload_ack,
    store_download_chunk,
    wait_for_download_ack,
)
from .models import FileTransferSession
from .utils import parse_upload_content_range

logger = logging.getLogger("trmm_file_transfer")


class FileTransferNextChunk(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, session_id):
        agent = getattr(request.user, "agent", None)
        if agent is None:
            return Response(
                "Invalid agent credentials",
                status=status.HTTP_403_FORBIDDEN,
            )

        session = get_object_or_404(
            FileTransferSession,
            session_id=session_id,
            agent=agent,
            operation=FileTransferOperation.UPLOAD,
        )

        if session.expires_at <= djangotime.now():
            session.status = FileTransferStatus.EXPIRED
            session.save(update_fields=["status", "updated_at"])
            return notify_error("Upload session has expired")

        if session.status not in (
            FileTransferStatus.AGENT_READY,
            FileTransferStatus.TRANSFERRING,
        ):
            return notify_error("Upload session is not ready for chunk transfer")

        redis_committed = get_upload_ack(session.session_id)
        committed = max(session.committed_offset, redis_committed or 0)

        chunk = pop_upload_chunk(session.session_id, committed)
        if chunk is None:
            return Response("No pending chunk", status=status.HTTP_404_NOT_FOUND)

        response = HttpResponse(
            chunk.data,
            content_type="application/octet-stream",
        )
        response["Content-Range"] = (
            f"bytes {chunk.start}-{chunk.end}/{session.total_size}"
        )
        response["X-Chunk-Start"] = str(chunk.start)
        response["X-Chunk-End"] = str(chunk.end)
        return response


class FileTransferAck(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        agent = getattr(request.user, "agent", None)
        if agent is None:
            return Response(
                "Invalid agent credentials",
                status=status.HTTP_403_FORBIDDEN,
            )

        session = get_object_or_404(
            FileTransferSession,
            session_id=session_id,
            agent=agent,
            operation=FileTransferOperation.UPLOAD,
        )

        if session.expires_at <= djangotime.now():
            session.status = FileTransferStatus.EXPIRED
            session.save(update_fields=["status", "updated_at"])
            return notify_error("Upload session has expired")

        if session.status not in (
            FileTransferStatus.AGENT_READY,
            FileTransferStatus.TRANSFERRING,
        ):
            return notify_error("Upload session is not ready for chunk transfer")

        try:
            committed_offset = int(request.data.get("committed_offset"))
        except (TypeError, ValueError):
            return notify_error("committed_offset must be a positive integer")

        if committed_offset < 1 or committed_offset > session.total_size:
            return notify_error("committed_offset is out of range")

        redis_committed = get_upload_ack(session.session_id)
        current_committed = max(session.committed_offset, redis_committed or 0)

        if committed_offset < current_committed:
            return notify_error("committed_offset cannot decrease")

        accepted_offset = get_accepted_offset(session.session_id)
        if accepted_offset is not None and committed_offset > accepted_offset:
            return notify_error("committed_offset exceeds accepted_offset")

        if committed_offset == current_committed:
            signal_upload_ack(session.session_id, committed_offset)
            return Response({"committed_offset": committed_offset})

        should_checkpoint = (
            committed_offset == session.total_size
            or committed_offset - session.committed_offset
            >= FILE_TRANSFER_ACK_CHECKPOINT_BYTES
        )
        if should_checkpoint:
            now = djangotime.now()
            update_fields = [
                "committed_offset",
                "pending_chunk_start",
                "pending_chunk_end",
                "pending_chunk_created_at",
                "last_ack_at",
                "expires_at",
                "updated_at",
            ]
            session.committed_offset = committed_offset
            session.pending_chunk_start = None
            session.pending_chunk_end = None
            session.pending_chunk_created_at = None
            session.last_ack_at = now
            session.expires_at = now + dt.timedelta(
                hours=FILE_TRANSFER_SESSION_TTL_HOURS
            )
            if session.status == FileTransferStatus.AGENT_READY:
                session.status = FileTransferStatus.TRANSFERRING
                update_fields.append("status")
            session.save(update_fields=update_fields)

        signal_upload_ack(session.session_id, committed_offset)

        return Response({"committed_offset": committed_offset})


class FileTransferDownloadPutChunk(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def put(self, request, session_id):
        agent = getattr(request.user, "agent", None)
        if agent is None:
            return Response(
                "Invalid agent credentials",
                status=status.HTTP_403_FORBIDDEN,
            )

        session = get_object_or_404(
            FileTransferSession,
            session_id=session_id,
            agent=agent,
            operation=FileTransferOperation.DOWNLOAD,
        )

        if session.expires_at <= djangotime.now():
            session.status = FileTransferStatus.EXPIRED
            session.save(update_fields=["status", "updated_at"])
            return notify_error("Download session has expired")

        if session.status not in (
            FileTransferStatus.AGENT_READY,
            FileTransferStatus.TRANSFERRING,
        ):
            return notify_error("Download session is not ready for chunk transfer")

        content_range = request.META.get("HTTP_CONTENT_RANGE", "")
        byte_range, range_err = parse_upload_content_range(
            content_range, session.total_size
        )
        if range_err:
            return notify_error(range_err)

        start, end = byte_range
        expected_len = end - start + 1

        if expected_len < 1 or expected_len > session.chunk_size:
            return notify_error("Chunk size exceeds allowed limit")

        content_length = request.META.get("CONTENT_LENGTH")
        if content_length:
            try:
                if int(content_length) != expected_len:
                    return notify_error(
                        "Request body size does not match Content-Range"
                    )
            except ValueError:
                return notify_error("Invalid Content-Length header")

        if expected_len > FILE_TRANSFER_CHUNK_SIZE_MAX:
            return notify_error("Chunk exceeds maximum request size")

        chunk_data = request.body
        if len(chunk_data) != expected_len:
            return notify_error("Request body size does not match Content-Range")

        redis_committed = get_download_ack(session.session_id)
        committed = max(session.committed_offset, redis_committed or 0)

        redis_offered = get_download_offered_offset(session.session_id)
        offered = max(redis_offered or 0, committed)

        if start != offered:
            return notify_error(
                f"Chunk start offset {start} does not match expected {offered}"
            )

        depth_bytes = FILE_TRANSFER_PIPELINE_DEPTH * session.chunk_size
        depth_wait_ms = 0.0
        if start - committed > depth_bytes:
            min_committed = start - depth_bytes
            depth_wait_t0 = time.monotonic()
            new_committed = wait_for_download_ack(
                session.session_id,
                min_committed,
                timeout=float(FILE_TRANSFER_DL_DEPTH_WAIT_SECONDS),
            )
            depth_wait_ms = (time.monotonic() - depth_wait_t0) * 1000
            if new_committed is None:
                session.refresh_from_db(fields=["status", "error_message"])
                if session.status == FileTransferStatus.FAILED:
                    return notify_error(session.error_message or "Download failed")
                session.status = FileTransferStatus.FAILED
                session.error_message = (
                    "Timed out waiting for client to ACK previous chunk"
                )
                session.save(update_fields=["status", "error_message", "updated_at"])
                return notify_error(session.error_message)
            committed = new_committed

        store_err = store_download_chunk(session.session_id, start, end, chunk_data)
        if store_err:
            return notify_error(store_err)

        if session.status == FileTransferStatus.AGENT_READY:
            session.status = FileTransferStatus.TRANSFERRING
            session.save(update_fields=["status", "updated_at"])

        logger.info(
            "file_transfer download chunk stored session=%s start=%s end=%s "
            "depth_wait_ms=%.1f",
            session.session_id,
            start,
            end,
            depth_wait_ms,
        )

        return Response(
            {
                "session_id": str(session.session_id),
                "status": session.status,
                "offered_offset": end + 1,
                "committed_offset": committed,
                "chunk_start": start,
                "chunk_end": end,
                "chunk_bytes": len(chunk_data),
            }
        )
