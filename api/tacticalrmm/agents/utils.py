import asyncio
import re
import threading
import urllib.parse
from io import StringIO
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from packaging import version as pyver
from rest_framework.response import Response

from checks.models import CheckResult
from core.utils import get_core_settings, get_mesh_device_id, get_mesh_ws_url
from tacticalrmm.constants import (
    AGENT_DEFER,
    AlertSeverity,
    CheckStatus,
    CheckType,
    MeshAgentIdent,
)
from tacticalrmm.helpers import notify_error


def get_agent_url(*, goarch: str, plat: str, token: str = "") -> str:
    ver = settings.LATEST_AGENT_VER
    if token:
        params = {
            "version": ver,
            "arch": goarch,
            "token": token,
            "plat": plat,
            "api": settings.ALLOWED_HOSTS[0],
        }
        return settings.AGENTS_URL + urllib.parse.urlencode(params)

    return f"https://github.com/amidaware/rmmagent/releases/download/v{ver}/tacticalagent-v{ver}-{plat}-{goarch}.exe"


def generate_linux_install(
    client: str,
    site: str,
    agent_type: str,
    arch: str,
    token: str,
    api: str,
    download_url: str,
) -> FileResponse:
    match arch:
        case "amd64":
            arch_id = MeshAgentIdent.LINUX64
        case "386":
            arch_id = MeshAgentIdent.LINUX32
        case "arm64":
            arch_id = MeshAgentIdent.LINUX_ARM_64
        case "arm":
            arch_id = MeshAgentIdent.LINUX_ARM_HF
        case _:
            arch_id = "not_found"

    core = get_core_settings()

    uri = get_mesh_ws_url()
    mesh_id = asyncio.run(get_mesh_device_id(uri, core.mesh_device_group))
    mesh_dl = (
        f"{core.mesh_site}/meshagents?id={mesh_id}&installflags=2&meshinstall={arch_id}"
    )

    text = Path(settings.LINUX_AGENT_SCRIPT).read_text()

    replace = {
        "agentDLChange": download_url,
        "meshDLChange": mesh_dl,
        "clientIDChange": client,
        "siteIDChange": site,
        "agentTypeChange": agent_type,
        "tokenChange": token,
        "apiURLChange": api,
    }

    for i, j in replace.items():
        text = text.replace(i, j)

    text += "\n"
    with StringIO(text) as fp:
        return FileResponse(
            fp.read(), as_attachment=True, filename="linux_agent_install.sh"
        )


def get_validated_agent(agent_id, min_version="2.10.0"):
    from .models import Agent

    agent = get_object_or_404(Agent.objects.defer(*AGENT_DEFER), agent_id=agent_id)

    if pyver.parse(agent.version) < pyver.parse(min_version):
        return notify_error(
            f"This feature requires agent version {min_version} or higher."
        )

    return agent


_nats_notify_loop: Optional[asyncio.AbstractEventLoop] = None
_nats_notify_thread: Optional[threading.Thread] = None
_nats_notify_lock = threading.Lock()


def _ensure_nats_notify_loop() -> asyncio.AbstractEventLoop:
    global _nats_notify_loop, _nats_notify_thread
    with _nats_notify_lock:
        if _nats_notify_loop is None:
            loop = asyncio.new_event_loop()

            def _run() -> None:
                asyncio.set_event_loop(loop)
                loop.run_forever()

            _nats_notify_thread = threading.Thread(
                target=_run, daemon=True, name="nats-notify"
            )
            _nats_notify_thread.start()
            _nats_notify_loop = loop
    return _nats_notify_loop


def send_nats_notification(agent, func: str, payload: dict) -> Optional[Response]:
    loop = _ensure_nats_notify_loop()
    data = {"func": func, "payload": payload}
    try:
        future = asyncio.run_coroutine_threadsafe(
            agent.nats_cmd(data, wait=False),
            loop,
        )
        future.result(timeout=10)
    except Exception as e:
        return notify_error(f"NATS communication failed: {str(e)}")
    return None


def send_nats_command(agent, func: str, payload: dict, timeout: int = 60):
    try:
        data = {"func": func, "payload": payload}
        response = asyncio.run(agent.nats_cmd(data, timeout=timeout))
    except Exception as e:
        return notify_error(f"NATS communication failed: {str(e)}")

    if response == "timeout":
        return notify_error("Unable to contact the agent")

    if isinstance(response, dict) and "error" in response:
        return notify_error(
            f"{func.replace('_', ' ').title()} failed: {response['error']}"
        )

    return response


def calculate_agent_checks(agent) -> dict:
    total, passing, failing, warning, info = 0, 0, 0, 0, 0

    for check in agent.get_checks_with_policies(exclude_overridden=True):
        total += 1
        if (
            not hasattr(check.check_result, "status")
            or isinstance(check.check_result, CheckResult)
            and check.check_result.status == CheckStatus.PASSING
        ):
            passing += 1
        elif (
            isinstance(check.check_result, CheckResult)
            and check.check_result.status == CheckStatus.FAILING
        ):
            alert_severity = (
                check.check_result.alert_severity
                if check.check_type
                in (
                    CheckType.MEMORY,
                    CheckType.CPU_LOAD,
                    CheckType.DISK_SPACE,
                    CheckType.SCRIPT,
                )
                else check.alert_severity
            )
            if alert_severity == AlertSeverity.ERROR:
                failing += 1
            elif alert_severity == AlertSeverity.WARNING:
                warning += 1
            elif alert_severity == AlertSeverity.INFO:
                info += 1

    ret = {
        "total": total,
        "passing": passing,
        "failing": failing,
        "warning": warning,
        "info": info,
        "has_failing_checks": failing > 0 or warning > 0,
    }
    return ret


def is_windows_path(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return False
    if any(x in s for x in ('"', "'", "\n", "\r", "&", "|", ";")):
        return False
    if not re.match(r"^(?:[a-zA-Z]:\\|\\\\)", s):
        return False
    if not s.lower().endswith(".exe"):
        return False
    return True


def is_posix_abs_path(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return False
    if any(x in s for x in ('"', "'", "\n", "\r", "&", "|", ";")):
        return False
    return s.startswith("/")


_INVALID_FILENAME_CHARS = frozenset('<>:"/\\|?*\x00')
_WINDOWS_ABS_PATH_RE = re.compile(r"^(?:[a-zA-Z]:\\|\\\\[^\\]+\\[^\\]+)")


def validate_file_transfer_filename(filename: str) -> Optional[str]:
    name = (filename or "").strip()
    if not name:
        return "filename is required"
    if name in (".", ".."):
        return "filename is invalid"
    if len(name) > 255:
        return "filename is too long"
    if any(ch in _INVALID_FILENAME_CHARS for ch in name):
        return "filename contains invalid characters"
    return None


def _path_has_traversal(path: str) -> bool:
    parts = re.split(r"[\\/]+", path.strip())
    return ".." in parts


def validate_file_transfer_destination_path(path: str, plat: str) -> Optional[str]:
    value = (path or "").strip()
    if not value:
        return "destination_path is required"
    if any(x in value for x in ('"', "'", "\n", "\r", "&", "|", ";", "\x00")):
        return "destination_path contains invalid characters"
    if _path_has_traversal(value):
        return "destination_path must not contain path traversal"

    if plat == "windows":
        if not _WINDOWS_ABS_PATH_RE.match(value):
            return "destination_path must be an absolute Windows path"
        return None

    if not is_posix_abs_path(value):
        return "destination_path must be an absolute path"
    return None


def resolve_upload_destination_path(
    destination_path: str, filename: str, plat: str
) -> str:
    destination_path = destination_path.strip()
    filename = filename.strip()
    sep = "\\" if plat == "windows" else "/"

    basename = re.split(r"[\\/]+", destination_path.rstrip("\\/"))[-1]
    if basename.lower() == filename.lower():
        return destination_path

    base = destination_path.rstrip("\\/")
    return f"{base}{sep}{filename}"


_UPLOAD_CONTENT_RANGE_RE = re.compile(
    r"^bytes (\d+)-(\d+)/(\d+|\*)$",
    re.IGNORECASE,
)


def parse_upload_content_range(
    header: str, total_size: int
) -> tuple[Optional[tuple[int, int]], Optional[str]]:
    value = (header or "").strip()
    if not value:
        return None, "Content-Range header is required"

    match = _UPLOAD_CONTENT_RANGE_RE.match(value)
    if not match:
        return None, "Invalid Content-Range header"

    try:
        start = int(match.group(1))
        end = int(match.group(2))
        total_raw = match.group(3)
        range_total = total_size if total_raw == "*" else int(total_raw)
    except ValueError:
        return None, "Invalid Content-Range header"

    if range_total != total_size:
        return None, "Content-Range total does not match session total_size"
    if start < 0 or end < start:
        return None, "Invalid Content-Range byte range"
    if end >= total_size:
        return None, "Content-Range exceeds file size"

    return (start, end), None


def validate_file_browser_path(path: str, plat: str) -> Optional[str]:
    return validate_file_transfer_destination_path(path, plat)


def normalize_file_browser_items(raw_items) -> list:
    if not isinstance(raw_items, list):
        return []

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue

        item_type = raw.get("type")
        if item_type not in ("file", "folder"):
            continue

        name = (raw.get("name") or "").strip()
        path = (raw.get("path") or "").strip()
        if not name or not path:
            continue

        item = {
            "id": str(raw.get("id") or path),
            "name": name,
            "path": path,
            "type": item_type,
            "size": str(raw.get("size") or "0"),
            "modified": raw.get("modified") or "",
            "created": raw.get("created") or "",
            "accessed": raw.get("accessed") or "",
            "hidden": bool(raw.get("hidden", False)),
            "system": bool(raw.get("system", False)),
            "readonly": bool(raw.get("readonly", False)),
        }
        extension = raw.get("extension")
        if extension:
            item["extension"] = str(extension)
        items.append(item)

    return items
