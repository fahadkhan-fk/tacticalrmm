"""Django system checks for the file-transfer subsystem.

These guard the timing relationships the chunked transfer protocol relies on.
If an operator overrides one of the constants in a way that breaks the ladder,
`manage.py check` (run on startup/migrate) fails fast with a clear message
instead of the protocol silently misbehaving at runtime.
"""

from django.core.checks import Error, register


@register()
def file_transfer_timeout_ladder_check(app_configs, **kwargs):
    from tacticalrmm.constants import (
        FILE_TRANSFER_ACK_WAIT_SECONDS,
        FILE_TRANSFER_DL_DEPTH_WAIT_SECONDS,
        FILE_TRANSFER_REDIS_CHUNK_TTL_SECONDS,
    )

    errors = []

    if FILE_TRANSFER_DL_DEPTH_WAIT_SECONDS <= FILE_TRANSFER_ACK_WAIT_SECONDS:
        errors.append(
            Error(
                "FILE_TRANSFER_DL_DEPTH_WAIT_SECONDS must be greater than "
                "FILE_TRANSFER_ACK_WAIT_SECONDS so the agent's depth-check wait "
                "outlives the client's chunk wait.",
                hint=(
                    f"DL_DEPTH_WAIT={FILE_TRANSFER_DL_DEPTH_WAIT_SECONDS}, "
                    f"ACK_WAIT={FILE_TRANSFER_ACK_WAIT_SECONDS}"
                ),
                id="agents.E001",
            )
        )

    if FILE_TRANSFER_REDIS_CHUNK_TTL_SECONDS <= FILE_TRANSFER_DL_DEPTH_WAIT_SECONDS:
        errors.append(
            Error(
                "FILE_TRANSFER_REDIS_CHUNK_TTL_SECONDS must be greater than "
                "FILE_TRANSFER_DL_DEPTH_WAIT_SECONDS so buffered chunks do not "
                "expire while a depth wait is in progress.",
                hint=(
                    f"CHUNK_TTL={FILE_TRANSFER_REDIS_CHUNK_TTL_SECONDS}, "
                    f"DL_DEPTH_WAIT={FILE_TRANSFER_DL_DEPTH_WAIT_SECONDS}"
                ),
                id="agents.E002",
            )
        )

    return errors
