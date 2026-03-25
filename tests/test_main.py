import asyncio
import hashlib
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def load_plugin_module():
    fake_decky = types.ModuleType("decky")
    fake_decky.HOME = "/tmp"
    fake_decky.DECKY_PLUGIN_DIR = "/tmp"
    fake_decky.logger = types.SimpleNamespace(info=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None)
    sys.modules["decky"] = fake_decky

    if "main" in sys.modules:
        return importlib.reload(sys.modules["main"])
    return importlib.import_module("main")


plugin_main = load_plugin_module()


class PluginUnderTest(plugin_main.Plugin):
    def __init__(self, *, appid: str, name: str, install_root: Path, asset_path: Path):
        self._appid = str(appid)
        self._name = name
        self._install_root = Path(install_root)
        self._asset_path = Path(asset_path)

    def _log(self, message: str) -> None:
        pass

    def _verify_bundled_asset(self) -> Path:
        return self._asset_path

    def _game_record(self, appid: str) -> dict | None:
        if str(appid) != self._appid:
            return None
        return {
            "appid": self._appid,
            "name": self._name,
            "install_path": str(self._install_root),
        }

    def _is_game_running(self, game_info: dict) -> bool:
        return False


class LaunchOptionTests(unittest.TestCase):
    def setUp(self):
        self.plugin = plugin_main.Plugin()

    def test_managed_launch_options_are_fixed_format(self):
        self.assertEqual(
            self.plugin._managed_launch_options("dxgi"),
            "WINEDLLOVERRIDES=dxgi=n,b SteamDeck=0 %command%",
        )

    def test_is_managed_launch_options_accepts_current_and_legacy_formats(self):
        self.assertTrue(self.plugin._is_managed_launch_options("WINEDLLOVERRIDES=dxgi=n,b SteamDeck=0 %command%"))
        self.assertTrue(self.plugin._is_managed_launch_options("WINEDLLOVERRIDES=dxgi=n,b"))

    def test_is_managed_launch_options_rejects_user_launch_options(self):
        self.assertFalse(self.plugin._is_managed_launch_options("MANGOHUD=1 %command% -fullscreen"))
        self.assertFalse(self.plugin._is_managed_launch_options("WINEDLLOVERRIDES=dxgi=n,b %command%"))

    def test_original_launch_options_to_restore_prefers_cleanup_metadata(self):
        self.assertEqual(
            self.plugin._original_launch_options_to_restore(
                "WINEDLLOVERRIDES=dxgi=n,b SteamDeck=0 %command%",
                "PROTON_LOG=1 %command%",
            ),
            "PROTON_LOG=1 %command%",
        )

    def test_original_launch_options_to_restore_drops_managed_current_options(self):
        self.assertEqual(
            self.plugin._original_launch_options_to_restore("WINEDLLOVERRIDES=winmm=n,b SteamDeck=0 %command%"),
            "",
        )

    def test_original_launch_options_to_restore_keeps_unmanaged_current_options(self):
        self.assertEqual(
            self.plugin._original_launch_options_to_restore("MANGOHUD=1 %command% -novid"),
            "MANGOHUD=1 %command% -novid",
        )


class PatchUnpatchFlowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.install_root = self.root / "Game"
        self.target_dir = self.install_root / "Binaries" / "Win64"
        self.target_dir.mkdir(parents=True)
        self.exe_path = self.target_dir / "Game-Win64-Shipping.exe"
        self.exe_path.write_bytes(b"exe")
        self.asset_path = self.root / plugin_main.BUNDLED_ASSET_NAME
        self.asset_bytes = b"fake bundled dlss enabler dll"
        self.asset_path.write_bytes(self.asset_bytes)
        self.asset_hash = hashlib.sha256(self.asset_bytes).hexdigest()
        self.legacy_asset_bytes = b"fake legacy dlss enabler dll"
        self.legacy_asset_hash = hashlib.sha256(self.legacy_asset_bytes).hexdigest()
        self.plugin = PluginUnderTest(appid="123", name="Test Game", install_root=self.install_root, asset_path=self.asset_path)

        self.fake_assets_by_version = {
            "4.3.1.0": {
                "version": "4.3.1.0",
                "sha256": self.legacy_asset_hash,
                "release_tag": "bins",
            },
            plugin_main.DLSS_ENABLER_VERSION: {
                "version": plugin_main.DLSS_ENABLER_VERSION,
                "sha256": self.asset_hash,
                "release_tag": plugin_main.KNOWN_DLSS_ENABLER_ASSETS_BY_VERSION[plugin_main.DLSS_ENABLER_VERSION]["release_tag"],
            },
        }
        self.hash_patch = mock.patch.object(plugin_main, "BUNDLED_ASSET_SHA256", self.asset_hash)
        self.version_map_patch = mock.patch.dict(plugin_main.KNOWN_DLSS_ENABLER_ASSETS_BY_VERSION, self.fake_assets_by_version, clear=True)
        self.sha_map_patch = mock.patch.dict(
            plugin_main.KNOWN_DLSS_ENABLER_ASSETS_BY_SHA256,
            {asset["sha256"].lower(): asset for asset in self.fake_assets_by_version.values()},
            clear=True,
        )
        self.token_map_patch = mock.patch.dict(
            plugin_main.KNOWN_DLSS_ENABLER_ASSETS_BY_TOKEN,
            {plugin_main._version_token(asset["version"]): asset for asset in self.fake_assets_by_version.values()},
            clear=True,
        )
        self.hash_patch.start()
        self.version_map_patch.start()
        self.sha_map_patch.start()
        self.token_map_patch.start()

    def tearDown(self):
        self.token_map_patch.stop()
        self.sha_map_patch.stop()
        self.version_map_patch.stop()
        self.hash_patch.stop()
        self.tempdir.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def read_marker_metadata(self, method: str) -> dict:
        marker_path = self.target_dir / self.plugin._marker_filename(method)
        return json.loads(marker_path.read_text(encoding="utf-8"))

    def test_patch_game_writes_fixed_launch_options_and_marker(self):
        result = self.run_async(self.plugin.patch_game("123", "dxgi", "PROTON_LOG=1 %command%"))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["launch_options"], "WINEDLLOVERRIDES=dxgi=n,b SteamDeck=0 %command%")
        self.assertEqual(result["original_launch_options"], "PROTON_LOG=1 %command%")

        proxy_path = self.target_dir / "dxgi.dll"
        marker_path = self.target_dir / self.plugin._marker_filename("dxgi")
        self.assertTrue(proxy_path.exists())
        self.assertEqual(proxy_path.read_bytes(), self.asset_bytes)
        self.assertTrue(marker_path.exists())
        self.assertEqual(marker_path.name, "DLSS_ENABLER_DXGI_DLL")

        marker = self.read_marker_metadata("dxgi")
        self.assertEqual(marker["marker_format"], "stable")
        self.assertEqual(marker["asset_version"], plugin_main.DLSS_ENABLER_VERSION)
        self.assertEqual(marker["asset_sha256"], self.asset_hash)
        self.assertEqual(marker["original_launch_options"], "PROTON_LOG=1 %command%")
        self.assertFalse(marker["backup_created"])
        self.assertEqual(marker["target_exe"], str(self.exe_path))

    def test_patch_and_unpatch_restore_previous_launch_options(self):
        patch_result = self.run_async(self.plugin.patch_game("123", "dxgi", "MANGOHUD=1 %command% -windowed"))
        unpatch_result = self.run_async(self.plugin.unpatch_game("123"))

        self.assertEqual(patch_result["status"], "success")
        self.assertEqual(unpatch_result["status"], "success")
        self.assertEqual(unpatch_result["launch_options"], "MANGOHUD=1 %command% -windowed")
        self.assertFalse((self.target_dir / "dxgi.dll").exists())
        self.assertFalse((self.target_dir / self.plugin._marker_filename("dxgi")).exists())
        self.assertIn("Removed managed dxgi.dll", unpatch_result["notes"])

    def test_patch_and_unpatch_restore_original_dll_backup(self):
        original_dll_bytes = b"stock dxgi dll"
        original_dll_path = self.target_dir / "dxgi.dll"
        original_dll_path.write_bytes(original_dll_bytes)

        patch_result = self.run_async(self.plugin.patch_game("123", "dxgi", ""))
        backup_path = self.target_dir / "dxgi.dll.backup"
        self.assertEqual(patch_result["status"], "success")
        self.assertTrue(backup_path.exists())
        self.assertEqual(backup_path.read_bytes(), original_dll_bytes)

        unpatch_result = self.run_async(self.plugin.unpatch_game("123"))
        self.assertEqual(unpatch_result["status"], "success")
        self.assertTrue(original_dll_path.exists())
        self.assertEqual(original_dll_path.read_bytes(), original_dll_bytes)
        self.assertFalse(backup_path.exists())
        self.assertIn("Restored original dxgi.dll", unpatch_result["notes"])

    def test_switching_methods_keeps_original_launch_options(self):
        first_patch = self.run_async(self.plugin.patch_game("123", "dxgi", "PROTON_LOG=1 %command%"))
        second_patch = self.run_async(self.plugin.patch_game("123", "winmm", first_patch["launch_options"]))

        self.assertEqual(second_patch["status"], "success")
        self.assertEqual(second_patch["launch_options"], "WINEDLLOVERRIDES=winmm=n,b SteamDeck=0 %command%")
        self.assertEqual(second_patch["original_launch_options"], "PROTON_LOG=1 %command%")
        self.assertFalse((self.target_dir / "dxgi.dll").exists())
        self.assertFalse((self.target_dir / self.plugin._marker_filename("dxgi")).exists())
        self.assertTrue((self.target_dir / "winmm.dll").exists())

        marker = self.read_marker_metadata("winmm")
        self.assertEqual(marker["original_launch_options"], "PROTON_LOG=1 %command%")

    def test_repatch_from_managed_launch_options_does_not_save_managed_string(self):
        result = self.run_async(self.plugin.patch_game("123", "dxgi", "WINEDLLOVERRIDES=dxgi=n,b SteamDeck=0 %command%"))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["original_launch_options"], "")
        marker = self.read_marker_metadata("dxgi")
        self.assertEqual(marker["original_launch_options"], "")

    def test_get_game_status_reports_upgrade_available_for_legacy_marker(self):
        proxy_path = self.target_dir / "dxgi.dll"
        proxy_path.write_bytes(self.legacy_asset_bytes)
        legacy_marker_path = self.target_dir / self.plugin._legacy_marker_filename("dxgi", "4.3.1.0")
        legacy_marker_path.write_text(
            json.dumps(
                {
                    "appid": "123",
                    "game_name": "Test Game",
                    "method": "dxgi",
                    "proxy_filename": "dxgi.dll",
                    "asset_name": plugin_main.BUNDLED_ASSET_NAME,
                    "asset_sha256": self.legacy_asset_hash,
                    "asset_version": "4.3.1.0",
                    "original_launch_options": "PROTON_LOG=1 %command%",
                    "target_exe": str(self.exe_path),
                }
            ),
            encoding="utf-8",
        )

        result = self.run_async(self.plugin.get_game_status("123"))

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["patched"])
        self.assertEqual(result["marker_name"], legacy_marker_path.name)
        self.assertEqual(result["marker_format"], "legacy")
        self.assertEqual(result["installed_asset_version"], "4.3.1.0")
        self.assertEqual(result["bundled_asset_version"], plugin_main.DLSS_ENABLER_VERSION)
        self.assertTrue(result["upgrade_available"])
        self.assertFalse(result["reinstall_recommended"])
        self.assertTrue(result["integrity_ok"])

    def test_patch_game_upgrades_legacy_marker_and_rewrites_stable_marker(self):
        proxy_path = self.target_dir / "dxgi.dll"
        proxy_path.write_bytes(self.legacy_asset_bytes)
        legacy_marker_path = self.target_dir / self.plugin._legacy_marker_filename("dxgi", "4.3.1.0")
        legacy_marker_path.write_text(
            json.dumps(
                {
                    "appid": "123",
                    "game_name": "Test Game",
                    "method": "dxgi",
                    "proxy_filename": "dxgi.dll",
                    "asset_name": plugin_main.BUNDLED_ASSET_NAME,
                    "asset_sha256": self.legacy_asset_hash,
                    "asset_version": "4.3.1.0",
                    "original_launch_options": "MANGOHUD=1 %command% -windowed",
                    "target_exe": str(self.exe_path),
                }
            ),
            encoding="utf-8",
        )

        result = self.run_async(self.plugin.patch_game("123", "dxgi", "WINEDLLOVERRIDES=dxgi=n,b SteamDeck=0 %command%"))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["original_launch_options"], "MANGOHUD=1 %command% -windowed")
        self.assertFalse(legacy_marker_path.exists())
        self.assertTrue((self.target_dir / self.plugin._marker_filename("dxgi")).exists())
        self.assertEqual(proxy_path.read_bytes(), self.asset_bytes)
        marker = self.read_marker_metadata("dxgi")
        self.assertEqual(marker["marker_format"], "stable")
        self.assertEqual(marker["asset_version"], plugin_main.DLSS_ENABLER_VERSION)
        self.assertEqual(marker["original_launch_options"], "MANGOHUD=1 %command% -windowed")


if __name__ == "__main__":
    unittest.main()
