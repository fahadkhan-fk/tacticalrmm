import datetime as dt
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from django.urls import reverse
from django.utils import timezone as djangotime
from model_bakery import baker
from rest_framework import status

from agents.models import Agent, FileTransferSession
from tacticalrmm.constants import (
    FILE_BROWSER_DEFAULT_PAGE_SIZE,
    FILE_BROWSER_MAX_PAGE_SIZE,
    FILE_TRANSFER_CHUNK_SIZE,
    FILE_TRANSFER_MAX_SESSIONS_PER_AGENT,
    FileTransferConflictPolicy,
    FileTransferOperation,
    FileTransferStatus,
)
from tacticalrmm.test import TacticalTestCase


class BaseFileBrowserAPITest(TacticalTestCase):
    """Base setup for File Browser and file-transfer API tests."""

    api_name = None

    def setUp(self) -> None:
        self.authenticate()
        self.setup_coresettings()
        self.agent = baker.make(Agent, version="2.10.0", plat="windows")
        if self.api_name:
            self.url = reverse(self.api_name, args=[self.agent.agent_id])

    def _session_url(self, api_name: str, session_id) -> str:
        return reverse(api_name, args=[self.agent.agent_id, session_id])

    def _make_transfer_session(self, **kwargs) -> FileTransferSession:
        defaults = {
            "agent": self.agent,
            "user": self.john,
            "operation": FileTransferOperation.UPLOAD,
            "status": FileTransferStatus.TRANSFERRING,
            "destination_path": r"C:\Users\Public\demo.txt",
            "filename": "demo.txt",
            "conflict_policy": FileTransferConflictPolicy.REPLACE,
            "total_size": 1024,
            "chunk_size": FILE_TRANSFER_CHUNK_SIZE,
            "committed_offset": 0,
            "expires_at": djangotime.now() + dt.timedelta(hours=1),
        }
        defaults.update(kwargs)
        return FileTransferSession.objects.create(**defaults)

    def _fill_agent_session_cap(self) -> None:
        for i in range(FILE_TRANSFER_MAX_SESSIONS_PER_AGENT):
            self._make_transfer_session(
                filename=f"cap-{i}.txt",
                destination_path=rf"C:\Users\Public\cap-{i}.txt",
            )


class TestListFiles(BaseFileBrowserAPITest):
    api_name = "list_files"

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_list_files_success(self, mock_nats_cmd) -> None:
        """Should return normalized directory listing when agent responds."""
        mock_nats_cmd.return_value = {
            "path": r"C:\Users\Public",
            "items": [
                {
                    "id": r"C:\Users\Public\Docs",
                    "name": "Docs",
                    "path": r"C:\Users\Public\Docs",
                    "type": "folder",
                    "size": "0",
                    "modified": "2026-06-18T12:00:00Z",
                },
                {
                    "name": "readme.txt",
                    "path": r"C:\Users\Public\readme.txt",
                    "type": "file",
                    "size": "12",
                    "extension": "txt",
                },
            ],
            "has_more": False,
            "total": 2,
        }

        response = self.client.get(
            self.url,
            {"path": r"C:\Users\Public"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["path"], r"C:\Users\Public")
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(body["items"][0]["type"], "folder")
        self.assertEqual(body["items"][1]["extension"], "txt")
        self.assertFalse(body["has_more"])
        self.assertEqual(body["total"], 2)
        mock_nats_cmd.assert_called_once()

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_list_files_default_path_and_pagination(self, mock_nats_cmd) -> None:
        """Empty path on page 1 should ask the agent for its default browse root."""
        mock_nats_cmd.return_value = {
            "path": r"C:\Users\Public",
            "items": [],
            "has_more": False,
            "total": 0,
        }

        response = self.client.get(self.url, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["path"], r"C:\Users\Public")
        mock_nats_cmd.assert_called_once_with(
            {
                "func": "files_list",
                "payload": {
                    "path": "",
                    "page": "1",
                    "page_size": str(FILE_BROWSER_DEFAULT_PAGE_SIZE),
                },
            },
            timeout=30,
        )

        self.check_not_authenticated("get", self.url)

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_list_files_with_filter(self, mock_nats_cmd) -> None:
        """Should pass sanitized name filter through to the agent."""
        mock_nats_cmd.return_value = {
            "path": r"C:\Users\Public",
            "items": [],
            "has_more": False,
            "total": 0,
        }

        response = self.client.get(
            self.url,
            {
                "path": r"C:\Users\Public",
                "filter": "readme",
                "page": 1,
                "page_size": 50,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        mock_nats_cmd.assert_called_once_with(
            {
                "func": "files_list",
                "payload": {
                    "path": r"C:\Users\Public",
                    "page": "1",
                    "page_size": "50",
                    "filter": "readme",
                },
            },
            timeout=30,
        )

    def test_list_files_empty_path_requires_page_one(self) -> None:
        """Empty path is only allowed for page 1 (agent-resolved default)."""
        response = self.client.get(self.url, {"page": 2}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("path is required", response.json())

    def test_list_files_invalid_page_size(self) -> None:
        """Should reject page_size outside the allowed range."""
        response = self.client.get(
            self.url,
            {
                "path": r"C:\Users\Public",
                "page_size": FILE_BROWSER_MAX_PAGE_SIZE + 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("page_size must be between 1 and", response.json())

    def test_list_files_invalid_filter_chars(self) -> None:
        """Should reject filters containing control characters."""
        response = self.client.get(
            self.url,
            {"path": r"C:\Users\Public", "filter": "bad\nfilter"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("filter contains invalid characters", response.json())

    def test_list_files_invalid_path(self) -> None:
        """Should reject relative / traversal paths before contacting the agent."""
        response = self.client.get(
            self.url,
            {"path": r"Users\Public"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("absolute Windows path", response.json())

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_list_files_timeout(self, mock_nats_cmd) -> None:
        """Should return error if agent times out."""
        mock_nats_cmd.return_value = "timeout"
        response = self.client.get(
            self.url, {"path": r"C:\Users\Public"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unable to contact the agent", response.json())

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_list_files_error_from_agent(self, mock_nats_cmd) -> None:
        """Should surface agent error messages."""
        mock_nats_cmd.return_value = {"error": "Access denied"}
        response = self.client.get(
            self.url, {"path": r"C:\Users\Public"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Access denied", response.json())

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_list_files_missing_resolved_path(self, mock_nats_cmd) -> None:
        """Agent must return a browsable path when the client sent an empty path."""
        mock_nats_cmd.return_value = {"items": [], "has_more": False, "total": 0}
        response = self.client.get(self.url, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Agent did not return a browsable path", response.json())

    def test_list_files_invalid_agent(self) -> None:
        """Should return 404 if agent does not exist."""
        invalid_url = reverse("list_files", args=["A" * 22])
        response = self.client.get(invalid_url, format="json")
        self.assertEqual(response.status_code, 404)

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_delete_files_success(self, mock_nats_cmd) -> None:
        """DELETE on files/ should delete paths and return per-path results."""
        mock_nats_cmd.return_value = {
            "results": [
                {"path": r"C:\Users\Public\old.txt", "success": True},
                {
                    "path": r"C:\Users\Public\locked.txt",
                    "success": False,
                    "error": "Access denied",
                },
            ]
        }

        response = self.client.delete(
            self.url,
            {
                "paths": [
                    r"C:\Users\Public\old.txt",
                    r"C:\Users\Public\locked.txt",
                ]
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(len(body["results"]), 2)
        self.assertTrue(body["results"][0]["success"])
        self.assertFalse(body["results"][1]["success"])
        mock_nats_cmd.assert_called_once()
        call_args = mock_nats_cmd.call_args[0][0]
        self.assertEqual(call_args["func"], "files_delete")

    def test_delete_files_missing_paths(self) -> None:
        """Should require a non-empty paths list."""
        response = self.client.delete(self.url, {"paths": []}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("paths is required", response.json())

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_delete_files_nats_exception(self, mock_nats_cmd: AsyncMock) -> None:
        """Should handle NATS communication exception."""
        mock_nats_cmd.side_effect = Exception("Connection refused")
        response = self.client.delete(
            self.url,
            {"paths": [r"C:\Users\Public\old.txt"]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("NATS communication failed", response.json())


class TestGetFileProperties(BaseFileBrowserAPITest):
    api_name = "get_file_properties"

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_get_file_properties_success(self, mock_nats_cmd) -> None:
        """Should return normalized properties for a path."""
        mock_nats_cmd.return_value = {
            "name": "Docs",
            "path": r"C:\Users\Public\Docs",
            "type": "folder",
            "size": "0",
            "location": r"C:\Users\Public",
            "file_count": 3,
            "folder_count": 1,
            "summary_truncated": False,
        }

        response = self.client.get(
            self.url, {"path": r"C:\Users\Public\Docs"}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["name"], "Docs")
        self.assertEqual(body["file_count"], 3)
        self.assertEqual(body["folder_count"], 1)
        mock_nats_cmd.assert_called_once()
        self.assertEqual(mock_nats_cmd.call_args[0][0]["func"], "files_properties")

    def test_get_file_properties_missing_path(self) -> None:
        """Should require path query param."""
        response = self.client.get(self.url, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("path is required", response.json())

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_get_file_properties_timeout(self, mock_nats_cmd) -> None:
        """Should handle agent timeout."""
        mock_nats_cmd.return_value = "timeout"
        response = self.client.get(
            self.url, {"path": r"C:\Users\Public\Docs"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unable to contact the agent", response.json())


class TestCreateFileFolder(BaseFileBrowserAPITest):
    api_name = "create_file_folder"

    def test_create_file_folder_missing_path(self) -> None:
        """Should return error if path is missing."""
        response = self.client.post(self.url, {"name": "NewFolder"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("path is required", response.json())

    def test_create_file_folder_invalid_name(self) -> None:
        """Should reject invalid folder names."""
        response = self.client.post(
            self.url,
            {"path": r"C:\Users\Public", "name": "bad/name"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.json())

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_create_file_folder_success(self, mock_nats_cmd) -> None:
        """Should create folder when agent responds with an item."""
        mock_nats_cmd.return_value = {
            "name": "NewFolder",
            "path": r"C:\Users\Public\NewFolder",
            "type": "folder",
            "size": "0",
        }

        response = self.client.post(
            self.url,
            {"path": r"C:\Users\Public", "name": "NewFolder"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["item"]["name"], "NewFolder")
        mock_nats_cmd.assert_called_once_with(
            {
                "func": "files_mkdir",
                "payload": {"path": r"C:\Users\Public", "name": "NewFolder"},
            },
            timeout=30,
        )

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_create_file_folder_agent_error(self, mock_nats_cmd) -> None:
        """Should return agent error without the prefixed title (prefix_error=False)."""
        mock_nats_cmd.return_value = {"error": "Folder already exists"}
        response = self.client.post(
            self.url,
            {"path": r"C:\Users\Public", "name": "NewFolder"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), "Folder already exists")

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_create_file_folder_timeout(self, mock_nats_cmd) -> None:
        """Should handle agent timeout."""
        mock_nats_cmd.return_value = "timeout"
        response = self.client.post(
            self.url,
            {"path": r"C:\Users\Public", "name": "NewFolder"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unable to contact the agent", response.json())


class TestRenameFile(BaseFileBrowserAPITest):
    api_name = "rename_file"

    def test_rename_file_missing_path(self) -> None:
        """Should require path."""
        response = self.client.post(
            self.url, {"new_name": "renamed.txt"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("path is required", response.json())

    def test_rename_file_invalid_new_name(self) -> None:
        """Should reject invalid new_name values."""
        response = self.client.post(
            self.url,
            {"path": r"C:\Users\Public\old.txt", "new_name": ""},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("new_name", response.json())

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_rename_file_success(self, mock_nats_cmd) -> None:
        """Should rename when agent returns the updated item."""
        mock_nats_cmd.return_value = {
            "name": "renamed.txt",
            "path": r"C:\Users\Public\renamed.txt",
            "type": "file",
            "size": "10",
            "extension": "txt",
        }

        response = self.client.post(
            self.url,
            {"path": r"C:\Users\Public\old.txt", "new_name": "renamed.txt"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")
        self.assertEqual(response.json()["item"]["name"], "renamed.txt")
        mock_nats_cmd.assert_called_once_with(
            {
                "func": "files_rename",
                "payload": {
                    "path": r"C:\Users\Public\old.txt",
                    "new_name": "renamed.txt",
                },
            },
            timeout=30,
        )

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_rename_file_nats_failure(self, mock_nats_cmd: AsyncMock) -> None:
        """Should handle NATS communication error."""
        mock_nats_cmd.side_effect = Exception("NATS down")
        response = self.client.post(
            self.url,
            {"path": r"C:\Users\Public\old.txt", "new_name": "renamed.txt"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("NATS communication failed", response.json())


class TestInitFileUpload(BaseFileBrowserAPITest):
    api_name = "init_file_upload"

    def _upload_payload(self, **overrides) -> dict:
        data = {
            "filename": "demo.txt",
            "destination_path": r"C:\Users\Public",
            "total_size": 1024,
        }
        data.update(overrides)
        return data

    def test_init_file_upload_missing_filename(self) -> None:
        """Should require a valid filename."""
        response = self.client.post(
            self.url,
            self._upload_payload(filename=""),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("filename", response.json())

    def test_init_file_upload_invalid_total_size(self) -> None:
        """Should require a positive total_size."""
        response = self.client.post(
            self.url,
            self._upload_payload(total_size=0),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("total_size must be a positive integer", response.json())

    def test_init_file_upload_invalid_conflict_policy(self) -> None:
        """Should only accept replace or skip."""
        response = self.client.post(
            self.url,
            self._upload_payload(conflict_policy="keep"),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("conflict_policy must be 'replace' or 'skip'", response.json())

    def test_init_file_upload_invalid_destination(self) -> None:
        """Should reject non-absolute destination paths."""
        response = self.client.post(
            self.url,
            self._upload_payload(destination_path="Public"),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("destination_path", response.json())

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_init_file_upload_allows_apostrophe_in_path(self, mock_nats_cmd) -> None:
        """Should upload into folders like John's Docs."""
        mock_nats_cmd.return_value = {"status": "ready", "committed_offset": 0}
        response = self.client.post(
            self.url,
            self._upload_payload(
                filename="it's & co.txt",
                destination_path=r"C:\Users\John's Docs",
            ),
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        session = FileTransferSession.objects.get(
            session_id=response.json()["session_id"]
        )
        self.assertEqual(
            session.destination_path, r"C:\Users\John's Docs\it's & co.txt"
        )

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_init_file_upload_success(self, mock_nats_cmd) -> None:
        """Should create a session and return agent-ready offsets."""
        mock_nats_cmd.return_value = {"status": "ready", "committed_offset": 0}

        response = self.client.post(
            self.url,
            self._upload_payload(conflict_policy="skip"),
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], FileTransferStatus.AGENT_READY)
        self.assertEqual(body["committed_offset"], 0)
        self.assertEqual(body["chunk_size"], FILE_TRANSFER_CHUNK_SIZE)
        self.assertTrue(body["session_id"])

        session = FileTransferSession.objects.get(session_id=body["session_id"])
        self.assertEqual(session.operation, FileTransferOperation.UPLOAD)
        self.assertEqual(session.conflict_policy, FileTransferConflictPolicy.SKIP)
        self.assertEqual(session.destination_path, r"C:\Users\Public\demo.txt")
        mock_nats_cmd.assert_called_once()
        payload = mock_nats_cmd.call_args[0][0]["payload"]
        self.assertEqual(payload["conflict_policy"], FileTransferConflictPolicy.SKIP)
        self.assertEqual(payload["destination_path"], r"C:\Users\Public\demo.txt")

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_init_file_upload_agent_error_marks_failed(self, mock_nats_cmd) -> None:
        """Agent prepare failures should mark the session failed."""
        mock_nats_cmd.return_value = {"error": "destination already exists"}
        response = self.client.post(self.url, self._upload_payload(), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("destination already exists", response.json())
        self.assertEqual(FileTransferSession.objects.count(), 1)
        self.assertEqual(
            FileTransferSession.objects.get().status, FileTransferStatus.FAILED
        )

    def test_init_file_upload_session_limit_returns_429(self) -> None:
        """Fresh init should 429 when the per-agent concurrency cap is full."""
        self._fill_agent_session_cap()
        response = self.client.post(self.url, self._upload_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("Too many concurrent file transfers", response.json())

    @patch("agents.views.clear_upload_session_redis")
    @patch("agents.views.get_upload_ack", return_value=None)
    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_init_file_upload_resume_skips_session_cap(
        self, mock_nats_cmd, _mock_ack, _mock_clear
    ) -> None:
        """Resume of an existing session must not be blocked by the concurrency cap."""
        session = self._make_transfer_session(
            status=FileTransferStatus.TRANSFERRING,
            committed_offset=512,
        )
        for i in range(FILE_TRANSFER_MAX_SESSIONS_PER_AGENT - 1):
            self._make_transfer_session(
                filename=f"other-{i}.txt",
                destination_path=rf"C:\Users\Public\other-{i}.txt",
            )

        mock_nats_cmd.return_value = {
            "status": "ready",
            "committed_offset": 512,
        }
        response = self.client.post(
            self.url,
            {
                "session_id": str(session.session_id),
                "filename": session.filename,
                "total_size": session.total_size,
                "destination_path": r"C:\Users\Public",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("resumed"))
        self.assertEqual(response.json()["committed_offset"], 512)


class TestCancelFileUpload(BaseFileBrowserAPITest):
    @patch("agents.views.clear_upload_session_redis")
    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_cancel_file_upload_success(self, mock_nats_cmd, _mock_clear) -> None:
        """Cancel should abort on the agent and mark the session cancelled."""
        session = self._make_transfer_session(status=FileTransferStatus.TRANSFERRING)
        mock_nats_cmd.return_value = {"status": "aborted"}

        url = self._session_url("cancel_file_upload", session.session_id)
        response = self.client.post(url, {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], FileTransferStatus.CANCELLED)
        session.refresh_from_db()
        self.assertEqual(session.status, FileTransferStatus.CANCELLED)
        mock_nats_cmd.assert_called_once()
        self.assertEqual(mock_nats_cmd.call_args[0][0]["func"], "files_upload_abort")

    @patch("agents.views.clear_upload_session_redis")
    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_cancel_file_upload_already_terminal(
        self, mock_nats_cmd, _mock_clear
    ) -> None:
        """Terminal sessions should be idempotent without contacting the agent."""
        session = self._make_transfer_session(status=FileTransferStatus.COMPLETED)
        url = self._session_url("cancel_file_upload", session.session_id)
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], FileTransferStatus.COMPLETED)
        mock_nats_cmd.assert_not_called()

    def test_cancel_file_upload_not_found(self) -> None:
        """Unknown session should 404."""
        url = self._session_url("cancel_file_upload", uuid4())
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, 404)


class TestInitFileDownload(BaseFileBrowserAPITest):
    api_name = "init_file_download"

    def test_init_file_download_invalid_source(self) -> None:
        """Should reject non-absolute source paths."""
        response = self.client.post(
            self.url, {"source_path": "readme.txt"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("source_path", response.json())

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_init_file_download_allows_apostrophe_in_path(self, mock_nats_cmd) -> None:
        """Should download files from folders like John's Docs."""
        mock_nats_cmd.return_value = {"status": "ready", "total_size": 2048}
        response = self.client.post(
            self.url,
            {"source_path": r"C:\Users\John's Docs\it's & co.txt"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        session = FileTransferSession.objects.get(
            session_id=response.json()["session_id"]
        )
        self.assertEqual(
            session.destination_path, r"C:\Users\John's Docs\it's & co.txt"
        )

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_init_file_download_success(self, mock_nats_cmd) -> None:
        """Should create a download session when agent reports ready + size."""
        mock_nats_cmd.return_value = {"status": "ready", "total_size": 2048}

        response = self.client.post(
            self.url,
            {"source_path": r"C:\Users\Public\readme.txt"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], FileTransferStatus.AGENT_READY)
        self.assertEqual(body["total_size"], 2048)
        self.assertEqual(body["committed_offset"], 0)

        session = FileTransferSession.objects.get(session_id=body["session_id"])
        self.assertEqual(session.operation, FileTransferOperation.DOWNLOAD)
        self.assertEqual(session.filename, "readme.txt")
        mock_nats_cmd.assert_called_once()
        self.assertEqual(
            mock_nats_cmd.call_args[0][0]["func"], "files_download_prepare"
        )

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_init_file_download_empty_file(self, mock_nats_cmd) -> None:
        """Agent reporting empty/invalid size should fail the session."""
        mock_nats_cmd.return_value = {"status": "ready", "total_size": 0}
        response = self.client.post(
            self.url,
            {"source_path": r"C:\Users\Public\empty.txt"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("empty or invalid file", response.json())
        self.assertEqual(
            FileTransferSession.objects.get().status, FileTransferStatus.FAILED
        )

    def test_init_file_download_session_limit_returns_429(self) -> None:
        """Fresh download init should honor the per-agent concurrency cap."""
        self._fill_agent_session_cap()
        response = self.client.post(
            self.url,
            {"source_path": r"C:\Users\Public\readme.txt"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class TestInitFileDownloadArchive(BaseFileBrowserAPITest):
    api_name = "init_file_download_archive"

    def test_init_archive_missing_paths(self) -> None:
        """Archive init requires at least one path."""
        response = self.client.post(self.url, {"paths": []}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("paths", response.json())

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_init_archive_success_returns_202(self, mock_nats_cmd) -> None:
        """Async prepare should return 202 while the agent builds the ZIP."""
        mock_nats_cmd.return_value = {"status": "building"}

        response = self.client.post(
            self.url,
            {
                "paths": [r"C:\Users\Public\Docs", r"C:\Users\Public\readme.txt"],
                "filename": "bundle",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        body = response.json()
        self.assertTrue(body["preparing"])
        self.assertTrue(body["is_archive"])
        self.assertEqual(body["filename"], "bundle.zip")
        self.assertEqual(body["status"], FileTransferStatus.WAITING_FOR_AGENT)
        self.assertEqual(body["total_size"], 0)

        session = FileTransferSession.objects.get(session_id=body["session_id"])
        self.assertTrue(session.is_archive)
        mock_nats_cmd.assert_called_once()
        self.assertEqual(
            mock_nats_cmd.call_args[0][0]["func"],
            "files_download_archive_prepare",
        )

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_init_archive_allows_apostrophe_in_paths(self, mock_nats_cmd) -> None:
        """ZIP download must accept the same names listing/mkdir already allow."""
        mock_nats_cmd.return_value = {"status": "building"}
        response = self.client.post(
            self.url,
            {"paths": [r"C:\Users\John's Docs", r"C:\Users\Public\a;b.txt"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_init_archive_agent_error(self, mock_nats_cmd) -> None:
        """Immediate validation failures from the agent should fail the session."""
        mock_nats_cmd.return_value = {"error": "too many files"}
        response = self.client.post(
            self.url,
            {"paths": [r"C:\Users\Public\Docs"]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("too many files", response.json())
        self.assertEqual(
            FileTransferSession.objects.get().status, FileTransferStatus.FAILED
        )


class TestCancelFileDownload(BaseFileBrowserAPITest):
    @patch("agents.views.clear_download_session_redis")
    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_cancel_file_download_success(self, mock_nats_cmd, _mock_clear) -> None:
        """Cancel should finalize on the agent and mark the session cancelled."""
        session = self._make_transfer_session(
            operation=FileTransferOperation.DOWNLOAD,
            status=FileTransferStatus.TRANSFERRING,
            destination_path=r"C:\Users\Public\readme.txt",
            filename="readme.txt",
        )
        mock_nats_cmd.return_value = {"status": "completed", "sha256": "abc"}

        url = self._session_url("cancel_file_download", session.session_id)
        response = self.client.post(url, {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], FileTransferStatus.CANCELLED)
        session.refresh_from_db()
        self.assertEqual(session.status, FileTransferStatus.CANCELLED)
        self.assertEqual(
            mock_nats_cmd.call_args[0][0]["func"], "files_download_finalize"
        )

    @patch("agents.views.clear_download_session_redis")
    @patch("agents.models.Agent.nats_cmd", new_callable=AsyncMock)
    def test_cancel_file_download_reason_error(
        self, mock_nats_cmd, _mock_clear
    ) -> None:
        """reason=error should mark FAILED (auto-release after client failure)."""
        session = self._make_transfer_session(
            operation=FileTransferOperation.DOWNLOAD,
            status=FileTransferStatus.AGENT_READY,
            destination_path=r"C:\Users\Public\readme.txt",
            filename="readme.txt",
        )
        mock_nats_cmd.return_value = {"status": "completed"}

        url = self._session_url("cancel_file_download", session.session_id)
        response = self.client.post(url, {"reason": "error"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], FileTransferStatus.FAILED)
        session.refresh_from_db()
        self.assertEqual(session.status, FileTransferStatus.FAILED)
        self.assertIn("failure", session.error_message.lower())


class TestGetFileDownloadStatus(BaseFileBrowserAPITest):
    def test_get_file_download_status_success(self) -> None:
        """Status poll should return session fields without contacting the agent."""
        session = self._make_transfer_session(
            operation=FileTransferOperation.DOWNLOAD,
            status=FileTransferStatus.WAITING_FOR_AGENT,
            destination_path=r"C:\Users\Public\Docs",
            filename="Docs.zip",
            is_archive=True,
            total_size=0,
            warnings='["skipped symlink"]',
        )

        url = self._session_url("get_file_download_status", session.session_id)
        response = self.client.get(url, format="json")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["session_id"], str(session.session_id))
        self.assertEqual(body["status"], FileTransferStatus.WAITING_FOR_AGENT)
        self.assertTrue(body["is_archive"])
        self.assertEqual(body["warnings"], ["skipped symlink"])
        self.assertEqual(body["filename"], "Docs.zip")

    def test_get_file_download_status_marks_expired(self) -> None:
        """Active sessions past expires_at should flip to expired on poll."""
        session = self._make_transfer_session(
            operation=FileTransferOperation.DOWNLOAD,
            status=FileTransferStatus.WAITING_FOR_AGENT,
            destination_path=r"C:\Users\Public\Docs",
            filename="Docs.zip",
            is_archive=True,
            expires_at=djangotime.now() - dt.timedelta(minutes=1),
        )

        url = self._session_url("get_file_download_status", session.session_id)
        response = self.client.get(url, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], FileTransferStatus.EXPIRED)
        session.refresh_from_db()
        self.assertEqual(session.status, FileTransferStatus.EXPIRED)


class TestListFileTransfers(BaseFileBrowserAPITest):
    api_name = "list_file_transfers"

    @patch("agents.views.get_download_ack", return_value=None)
    @patch("agents.views.get_upload_ack", return_value=750)
    def test_list_file_transfers_success(
        self, _mock_upload_ack, _mock_download_ack
    ) -> None:
        """Should list resumable sessions for the current user/agent only."""
        upload = self._make_transfer_session(
            status=FileTransferStatus.TRANSFERRING,
            committed_offset=500,
            filename="up.txt",
            destination_path=r"C:\Users\Public\up.txt",
        )
        download = self._make_transfer_session(
            operation=FileTransferOperation.DOWNLOAD,
            status=FileTransferStatus.AGENT_READY,
            filename="down.txt",
            destination_path=r"C:\Users\Public\down.txt",
            total_size=4096,
            committed_offset=0,
        )
        self._make_transfer_session(
            status=FileTransferStatus.COMPLETED,
            filename="done.txt",
            destination_path=r"C:\Users\Public\done.txt",
        )
        self._make_transfer_session(
            status=FileTransferStatus.TRANSFERRING,
            filename="expired.txt",
            destination_path=r"C:\Users\Public\expired.txt",
            expires_at=djangotime.now() - dt.timedelta(minutes=5),
        )
        self._make_transfer_session(
            user=self.alice,
            status=FileTransferStatus.TRANSFERRING,
            filename="alice.txt",
            destination_path=r"C:\Users\Public\alice.txt",
        )

        response = self.client.get(self.url, format="json")
        self.assertEqual(response.status_code, 200)
        transfers = response.json()["transfers"]
        self.assertEqual(len(transfers), 2)

        by_id = {item["session_id"]: item for item in transfers}
        self.assertIn(str(upload.session_id), by_id)
        self.assertIn(str(download.session_id), by_id)
        self.assertEqual(by_id[str(upload.session_id)]["committed_offset"], 750)
        self.assertEqual(
            by_id[str(upload.session_id)]["conflict_policy"],
            FileTransferConflictPolicy.REPLACE,
        )
        self.assertEqual(by_id[str(download.session_id)]["operation"], "download")
        self.assertNotIn("conflict_policy", by_id[str(download.session_id)])

        self.check_not_authenticated("get", self.url)
