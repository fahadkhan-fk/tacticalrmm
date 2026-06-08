from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone as djangotime
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from tacticalrmm.constants import FileTransferOperation, FileTransferStatus
from tacticalrmm.helpers import notify_error

from .file_transfer_relay import pop_pending_upload_chunk
from .models import FileTransferSession


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

        chunk = pop_pending_upload_chunk(session.session_id)
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

        if committed_offset < session.committed_offset:
            return notify_error("committed_offset cannot decrease")

        if committed_offset == session.committed_offset:
            return Response({"committed_offset": session.committed_offset})

        update_fields = ["committed_offset", "updated_at"]
        session.committed_offset = committed_offset
        if session.status == FileTransferStatus.AGENT_READY:
            session.status = FileTransferStatus.TRANSFERRING
            update_fields.append("status")
        session.save(update_fields=update_fields)

        return Response({"committed_offset": session.committed_offset})
