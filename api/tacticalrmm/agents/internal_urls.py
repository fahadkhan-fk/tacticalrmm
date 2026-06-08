from django.urls import path

from .internal_views import FileTransferAck, FileTransferNextChunk

urlpatterns = [
    path(
        "file-transfers/<uuid:session_id>/next-chunk/",
        FileTransferNextChunk.as_view(),
        name="file_transfer_next_chunk",
    ),
    path(
        "file-transfers/<uuid:session_id>/ack/",
        FileTransferAck.as_view(),
        name="file_transfer_ack",
    ),
]
