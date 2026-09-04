import pickle
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

from agents.utils import (
    collect_file_transfer_paths,
    generate_linux_install,
    get_agent_url,
    is_posix_abs_path,
    is_windows_path,
    strip_relation_caches_for_cache,
    validate_file_browser_path,
    validate_file_transfer_destination_path,
    validate_file_transfer_filename,
    validate_file_transfer_source_path,
)
from automation.models import Policy
from checks.models import Check
from scripts.models import Script
from tacticalrmm.test import TacticalTestCase


class TestStripRelationCaches(SimpleTestCase):
    def test_returns_isolated_copy_with_only_required_relations(self):
        policy = Policy(pk=1)
        script = Script(pk=2)
        check = Check(pk=3, policy=policy, script=script)
        check._prefetched_objects_cache = {"assignedtasks": [object()]}
        check.check_result = object()

        cleaned = strip_relation_caches_for_cache([check])[0]

        self.assertIsNot(cleaned, check)
        self.assertIsNot(cleaned._state, check._state)
        self.assertEqual(cleaned.policy_id, policy.pk)
        self.assertEqual(cleaned.script_id, script.pk)
        self.assertEqual(cleaned._state.fields_cache, {"script": script})
        self.assertEqual(cleaned._prefetched_objects_cache, {})
        self.assertNotIn("check_result", cleaned.__dict__)

        restored = pickle.loads(pickle.dumps(cleaned))
        self.assertEqual(restored.pk, check.pk)
        self.assertEqual(restored.policy_id, policy.pk)
        self.assertEqual(restored.script, script)

        # should not modify instances that callers may still use
        self.assertEqual(check._state.fields_cache["policy"], policy)
        self.assertEqual(check._state.fields_cache["script"], script)
        self.assertIn("assignedtasks", check._prefetched_objects_cache)
        self.assertIn("check_result", check.__dict__)


class TestAgentUtils(TacticalTestCase):
    def setUp(self) -> None:
        self.authenticate()
        self.setup_coresettings()
        self.setup_base_instance()

    def test_get_agent_url(self):
        ver = settings.LATEST_AGENT_VER

        # test without token
        r = get_agent_url(goarch="amd64", plat="windows", token="")
        expected = f"https://github.com/amidaware/rmmagent/releases/download/v{ver}/tacticalagent-v{ver}-windows-amd64.exe"
        self.assertEqual(r, expected)

        # test with token
        r = get_agent_url(goarch="386", plat="linux", token="token123")
        expected = f"https://{settings.AGENTS_URL}version={ver}&arch=386&token=token123&plat=linux&api=api.example.com"

    @patch("agents.utils.get_mesh_device_id")
    @patch("agents.utils.asyncio.run")
    @patch("agents.utils.get_mesh_ws_url")
    @patch("agents.utils.get_core_settings")
    def test_generate_linux_install(
        self, mock_core, mock_mesh, mock_async_run, mock_mesh_device_id
    ):
        mock_mesh_device_id.return_value = "meshdeviceid"
        mock_core.return_value.mesh_site = "meshsite"
        mock_async_run.return_value = "meshid"
        mock_mesh.return_value = "meshws"
        r = generate_linux_install(
            client="1",
            site="1",
            agent_type="server",
            arch="amd64",
            token="token123",
            api="api.example.com",
            download_url="asdasd3423",
        )

        ret = r.getvalue().decode("utf-8")

        self.assertIn(r"agentDL='asdasd3423'", ret)
        self.assertIn(
            r"meshDL='meshsite/meshagents?id=meshid&installflags=2&meshinstall=6'", ret
        )
        self.assertIn(r"apiURL='api.example.com'", ret)
        self.assertIn(r"agentDL='asdasd3423'", ret)
        self.assertIn(r"token='token123'", ret)
        self.assertIn(r"clientID='1'", ret)
        self.assertIn(r"siteID='1'", ret)
        self.assertIn(r"agentType='server'", ret)


class TestFileTransferPathValidation(SimpleTestCase):
    """Transfer paths must match listing: apostrophe/ampersand/etc are valid names."""

    def test_transfer_allows_shell_meta_chars_in_paths(self) -> None:
        windows_dir = r"C:\Users\John's Docs"
        windows_file = r"C:\Users\John's Docs\it's & co.txt"
        posix_dir = "/tmp/it's & co"
        posix_file = "/tmp/it's & co/x.txt;done"

        self.assertIsNone(
            validate_file_transfer_destination_path(windows_dir, "windows")
        )
        self.assertIsNone(
            validate_file_transfer_destination_path(windows_file, "windows")
        )
        self.assertIsNone(validate_file_transfer_source_path(windows_file, "windows"))
        self.assertIsNone(validate_file_browser_path(windows_dir, "windows"))

        self.assertIsNone(validate_file_transfer_destination_path(posix_dir, "linux"))
        self.assertIsNone(validate_file_transfer_source_path(posix_file, "linux"))
        self.assertIsNone(validate_file_browser_path(posix_dir, "linux"))
        self.assertIsNone(validate_file_transfer_filename("it's & co.txt"))

        paths, err = collect_file_transfer_paths(
            [posix_file, '/tmp/quote"file.txt'], "linux"
        )
        self.assertIsNone(err)
        self.assertEqual(len(paths), 2)

        paths, err = collect_file_transfer_paths(
            [r"C:\Users\John's Docs", r"C:\Users\a|b"], "windows"
        )
        self.assertIsNone(err)
        self.assertEqual(len(paths), 2)

    def test_transfer_still_rejects_control_traversal_and_relative(self) -> None:
        self.assertIsNotNone(
            validate_file_transfer_destination_path("Public", "windows")
        )
        self.assertIsNotNone(validate_file_transfer_source_path("readme.txt", "linux"))
        self.assertIsNotNone(
            validate_file_transfer_destination_path(r"C:\Users\..\Windows", "windows")
        )
        self.assertIsNotNone(
            validate_file_transfer_source_path("/tmp/foo\nbar", "linux")
        )
        self.assertIsNotNone(
            validate_file_transfer_destination_path("/tmp/foo\x00bar", "linux")
        )

    def test_shell_helpers_still_ban_metas(self) -> None:
        """Custom shell fields still must not contain injection characters."""
        self.assertFalse(is_posix_abs_path("/bin/bash;id"))
        self.assertFalse(is_posix_abs_path("/tmp/it's"))
        self.assertTrue(is_posix_abs_path("/bin/bash"))
        self.assertFalse(is_windows_path(r"C:\Program Files\it's.exe"))
        self.assertTrue(is_windows_path(r"C:\Windows\System32\cmd.exe"))
