import json
import hashlib
import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import decky

BUNDLED_ASSET_NAME = "version.dll"
BUNDLED_ASSET_SHA256 = "a07b82de96e8c278184fe01409d7b4851a67865f7b8fed56332e40028dc3b41f"
DLSS_ENABLER_VERSION = "4.3.1.0"
DLSS_ENABLER_VERSION_TOKEN = DLSS_ENABLER_VERSION.replace(".", "_")
MARKER_PREFIX = f"DLSS_ENABLER_{DLSS_ENABLER_VERSION_TOKEN}_"
MARKER_SUFFIX = "_DLL"
BACKUP_SUFFIX = ".backup"

SUPPORTED_METHODS = [
    "version",
    "winmm",
    "d3d11",
    "d3d12",
    "dinput8",
    "dxgi",
    "wininet",
    "winhttp",
    "dbghelp",
]

UNREAL_HINTS = [
    "/binaries/win64/",
    "-win64-shipping.exe",
    "shipping.exe",
]

BAD_EXE_SUBSTRINGS = [
    "crashreport",
    "crashreportclient",
    "eac",
    "easyanticheat",
    "beclient",
    "eosbootstrap",
    "benchmark",
    "uninstall",
    "setup",
    "launcher",
    "updater",
    "bootstrap",
    "_redist",
    "prereq",
]


class Plugin:
    def _log(self, message: str) -> None:
        decky.logger.info(f"[DLSS Enabler] {message}")

    async def _main(self):
        self._log("plugin loaded")

    async def _unload(self):
        self._log("plugin unloaded")

    async def _uninstall(self):
        self._log("plugin uninstalled")

    async def _migration(self):
        pass

    def _home_path(self) -> Path:
        try:
            return Path(decky.HOME)
        except TypeError:
            return Path(str(decky.HOME))

    def _plugin_bin_dir(self) -> Path:
        return Path(decky.DECKY_PLUGIN_DIR) / "bin"

    def _bundled_asset_path(self) -> Path:
        return self._plugin_bin_dir() / BUNDLED_ASSET_NAME

    def _file_sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _safe_sha256(self, path: Path) -> str | None:
        try:
            if path.exists() and path.is_file() and not path.is_symlink():
                return self._file_sha256(path)
        except Exception:
            return None
        return None

    def _verify_bundled_asset(self) -> Path:
        asset_path = self._bundled_asset_path()
        if not asset_path.exists():
            raise FileNotFoundError(f"Bundled asset missing: {asset_path}")

        asset_hash = self._file_sha256(asset_path)
        self._log(f"verify bundled asset: path={asset_path} sha256={asset_hash}")
        if asset_hash.lower() != BUNDLED_ASSET_SHA256.lower():
            raise RuntimeError(
                f"Bundled asset hash mismatch for {asset_path.name}: expected {BUNDLED_ASSET_SHA256}, got {asset_hash}"
            )
        return asset_path

    def _read_json_file(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as file:
                parsed = json.load(file)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _write_json_file(self, path: Path, payload: dict) -> None:
        with open(path, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)

    def _normalize_method(self, method: str | None) -> str:
        normalized = (method or "version").replace(".dll", "").strip().lower()
        if normalized not in SUPPORTED_METHODS:
            raise ValueError(f"Unsupported injection method '{method}'")
        return normalized

    def _marker_filename(self, method: str) -> str:
        return f"{MARKER_PREFIX}{self._normalize_method(method).upper()}{MARKER_SUFFIX}"

    def _marker_method_from_name(self, marker_name: str) -> str | None:
        pattern = rf"^{re.escape(MARKER_PREFIX)}([A-Z0-9]+){re.escape(MARKER_SUFFIX)}$"
        match = re.match(pattern, marker_name)
        if not match:
            return None
        parsed = match.group(1).lower()
        return parsed if parsed in SUPPORTED_METHODS else None

    def _steam_root_candidates(self) -> list[Path]:
        home = self._home_path()
        candidates = [
            home / ".local" / "share" / "Steam",
            home / ".steam" / "steam",
            home / ".steam" / "root",
            home / ".var" / "app" / "com.valvesoftware.Steam" / "home" / ".local" / "share" / "Steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / "home" / ".steam" / "steam",
        ]

        unique: list[Path] = []
        seen = set()
        for candidate in candidates:
            key = str(candidate)
            if key not in seen:
                unique.append(candidate)
                seen.add(key)
        return unique

    def _steam_library_paths(self) -> list[Path]:
        library_paths: list[Path] = []
        seen = set()

        for steam_root in self._steam_root_candidates():
            if steam_root.exists():
                key = str(steam_root)
                if key not in seen:
                    library_paths.append(steam_root)
                    seen.add(key)

            library_file = steam_root / "steamapps" / "libraryfolders.vdf"
            if not library_file.exists():
                continue

            try:
                with open(library_file, "r", encoding="utf-8", errors="replace") as file:
                    for line in file:
                        if '"path"' not in line:
                            continue
                        path = line.split('"path"', 1)[1].strip().strip('"').replace("\\\\", "/")
                        candidate = Path(path)
                        key = str(candidate)
                        if key not in seen:
                            library_paths.append(candidate)
                            seen.add(key)
            except Exception as exc:
                self._log(f"failed to parse libraryfolders: {library_file} error={exc}")

        return library_paths

    def _find_installed_games(self, appid: str | None = None) -> list[dict]:
        games: list[dict] = []

        for library_path in self._steam_library_paths():
            steamapps_path = library_path / "steamapps"
            if not steamapps_path.exists():
                continue

            for appmanifest in steamapps_path.glob("appmanifest_*.acf"):
                game_info = {
                    "appid": "",
                    "name": "",
                    "library_path": str(library_path),
                    "install_path": "",
                }
                install_dir = ""
                try:
                    with open(appmanifest, "r", encoding="utf-8", errors="replace") as file:
                        for line in file:
                            if '"appid"' in line:
                                game_info["appid"] = line.split('"appid"', 1)[1].strip().strip('"')
                            elif '"name"' in line:
                                game_info["name"] = line.split('"name"', 1)[1].strip().strip('"')
                            elif '"installdir"' in line:
                                install_dir = line.split('"installdir"', 1)[1].strip().strip('"')
                except Exception as exc:
                    self._log(f"skipping manifest {appmanifest}: {exc}")
                    continue

                if not game_info["appid"] or not game_info["name"]:
                    continue
                if "Proton" in game_info["name"] or "Steam Linux Runtime" in game_info["name"]:
                    continue

                install_path = steamapps_path / "common" / install_dir if install_dir else Path()
                game_info["install_path"] = str(install_path)

                if appid is None or str(game_info["appid"]) == str(appid):
                    games.append(game_info)

        deduped: dict[str, dict] = {}
        for game in games:
            deduped[str(game["appid"])] = game
        return sorted(deduped.values(), key=lambda entry: entry["name"].lower())

    def _compatdata_dirs_for_appid(self, appid: str) -> list[Path]:
        matches: list[Path] = []
        for library in self._steam_library_paths():
            compatdata_dir = library / "steamapps" / "compatdata" / str(appid)
            if compatdata_dir.exists():
                matches.append(compatdata_dir)
        return matches

    def _game_record(self, appid: str) -> dict | None:
        matches = self._find_installed_games(appid)
        return matches[0] if matches else None

    def _normalized_path_string(self, value: str) -> str:
        normalized = value.lower().replace("\\", "/")
        normalized = normalized.replace("z:/", "/")
        normalized = normalized.replace("//", "/")
        return normalized

    def _candidate_executables(self, install_root: Path) -> list[Path]:
        if not install_root.exists():
            return []

        candidates: list[Path] = []
        try:
            for exe in install_root.rglob("*.exe"):
                if not exe.is_file():
                    continue
                candidates.append(exe)
        except Exception as exc:
            self._log(f"candidate exe scan failed for {install_root}: {exc}")
        return candidates

    def _exe_score(self, exe: Path, install_root: Path, game_name: str) -> int:
        normalized = self._normalized_path_string(str(exe))
        name = exe.name.lower()
        score = 0

        if normalized.endswith("-win64-shipping.exe"):
            score += 300
        if "shipping.exe" in name:
            score += 220
        if "/binaries/win64/" in normalized:
            score += 200
        if "/win64/" in normalized:
            score += 80
        if exe.parent == install_root:
            score += 20

        sanitized_game = re.sub(r"[^a-z0-9]", "", game_name.lower())
        sanitized_name = re.sub(r"[^a-z0-9]", "", exe.stem.lower())
        sanitized_root = re.sub(r"[^a-z0-9]", "", install_root.name.lower())
        if sanitized_game and sanitized_game in sanitized_name:
            score += 120
        if sanitized_root and sanitized_root in sanitized_name:
            score += 90

        for bad in BAD_EXE_SUBSTRINGS:
            if bad in normalized:
                score -= 200

        score -= len(exe.parts)
        return score

    def _best_running_executable(self, candidates: list[Path]) -> Path | None:
        if not candidates:
            return None

        try:
            result = subprocess.run(["ps", "-eo", "args="], capture_output=True, text=True, check=False)
            process_lines = result.stdout.splitlines()
        except Exception as exc:
            self._log(f"running executable scan failed: {exc}")
            return None

        normalized_candidates = [(exe, self._normalized_path_string(str(exe))) for exe in candidates]
        matches: list[tuple[int, Path]] = []
        for line in process_lines:
            normalized_line = self._normalized_path_string(line)
            for exe, normalized_exe in normalized_candidates:
                if normalized_exe in normalized_line:
                    matches.append((len(normalized_exe), exe))

        if not matches:
            return None
        matches.sort(key=lambda item: item[0], reverse=True)
        return matches[0][1]

    def _guess_patch_target(self, game_info: dict) -> tuple[Path, Path | None]:
        install_root = Path(game_info["install_path"])
        candidates = self._candidate_executables(install_root)
        if not candidates:
            return install_root, None

        running_exe = self._best_running_executable(candidates)
        if running_exe:
            return running_exe.parent, running_exe

        best = max(candidates, key=lambda exe: self._exe_score(exe, install_root, game_info["name"]))
        return best.parent, best

    def _find_markers_under_install_root(self, install_root: Path) -> list[Path]:
        if not install_root.exists():
            return []

        markers: list[Path] = []
        try:
            for marker in install_root.rglob(f"{MARKER_PREFIX}*{MARKER_SUFFIX}"):
                if marker.is_file() and self._marker_method_from_name(marker.name):
                    markers.append(marker)
        except Exception as exc:
            self._log(f"marker scan failed under {install_root}: {exc}")

        return sorted(markers, key=lambda path: path.stat().st_mtime, reverse=True)

    def _read_marker_metadata(self, marker_path: Path) -> dict:
        metadata = {
            "marker_name": marker_path.name,
            "method": self._marker_method_from_name(marker_path.name),
            "original_launch_options": "",
            "backup_created": False,
        }
        try:
            parsed = self._read_json_file(marker_path)
            if parsed:
                metadata.update(parsed)
        except Exception:
            pass
        if not metadata.get("method"):
            metadata["method"] = self._marker_method_from_name(marker_path.name)
        return metadata

    def _write_marker_metadata(
        self,
        marker_path: Path,
        *,
        appid: str,
        game_name: str,
        method: str,
        target_dir: Path,
        target_exe: Path | None,
        original_launch_options: str,
        backup_created: bool,
    ) -> None:
        payload = {
            "appid": str(appid),
            "game_name": game_name,
            "method": self._normalize_method(method),
            "proxy_filename": f"{self._normalize_method(method)}.dll",
            "asset_name": BUNDLED_ASSET_NAME,
            "asset_sha256": BUNDLED_ASSET_SHA256,
            "asset_version": DLSS_ENABLER_VERSION,
            "target_dir": str(target_dir),
            "target_exe": str(target_exe) if target_exe else "",
            "original_launch_options": original_launch_options,
            "backup_created": bool(backup_created),
            "patched_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_json_file(marker_path, payload)

    def _describe_path(self, path: Path) -> dict:
        exists = path.exists() or path.is_symlink()
        description = {
            "path": str(path),
            "exists": exists,
            "is_symlink": path.is_symlink(),
        }
        if not exists:
            return description

        try:
            stat_result = path.lstat() if path.is_symlink() else path.stat()
            description["size"] = stat_result.st_size
        except Exception:
            pass

        if path.is_symlink():
            try:
                description["symlink_target"] = os.readlink(path)
            except Exception:
                pass
        else:
            sha = self._safe_sha256(path)
            if sha:
                description["sha256"] = sha
        return description

    def _log_target_state(self, prefix: str, target_dir: Path, method: str) -> None:
        normalized_method = self._normalize_method(method)
        proxy_filename = f"{normalized_method}.dll"
        proxy_path = target_dir / proxy_filename
        backup_path = target_dir / f"{proxy_filename}{BACKUP_SUFFIX}"
        marker_path = target_dir / self._marker_filename(normalized_method)
        self._log(
            f"{prefix}: proxy={json.dumps(self._describe_path(proxy_path), sort_keys=True)} "
            f"backup={json.dumps(self._describe_path(backup_path), sort_keys=True)} "
            f"marker={json.dumps(self._describe_path(marker_path), sort_keys=True)}"
        )

    def _is_bundled_proxy_file(self, path: Path) -> bool:
        try:
            return path.is_file() and self._file_sha256(path).lower() == BUNDLED_ASSET_SHA256.lower()
        except Exception:
            return False

    def _unique_stash_path(self, path: Path, label: str) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        base = path.with_name(f"{path.name}.{label}.{timestamp}")
        candidate = base
        counter = 1
        while candidate.exists():
            candidate = path.with_name(f"{base.name}.{counter}")
            counter += 1
        return candidate

    def _remove_path(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    def _restore_method_in_dir(self, target_dir: Path, method: str) -> list[str]:
        notes: list[str] = []
        proxy_filename = f"{self._normalize_method(method)}.dll"
        proxy_path = target_dir / proxy_filename
        backup_path = target_dir / f"{proxy_filename}{BACKUP_SUFFIX}"

        backup_exists = backup_path.exists() or backup_path.is_symlink()
        proxy_exists = proxy_path.exists() or proxy_path.is_symlink()

        if backup_exists:
            if proxy_exists:
                if self._is_bundled_proxy_file(proxy_path):
                    self._remove_path(proxy_path)
                else:
                    stashed_path = self._unique_stash_path(proxy_path, "unexpected")
                    proxy_path.rename(stashed_path)
                    notes.append(f"Stashed unexpected {proxy_filename} to {stashed_path.name}")
            backup_path.rename(proxy_path)
            notes.append(f"Restored original {proxy_filename}")
        elif proxy_exists:
            if self._is_bundled_proxy_file(proxy_path):
                self._remove_path(proxy_path)
                notes.append(f"Removed managed {proxy_filename}")
            else:
                stashed_path = self._unique_stash_path(proxy_path, "unexpected")
                proxy_path.rename(stashed_path)
                notes.append(f"Stashed unexpected {proxy_filename} to {stashed_path.name}")

        return notes

    def _cleanup_install_root(self, install_root: Path) -> dict:
        marker_paths = self._find_markers_under_install_root(install_root)
        notes: list[str] = []
        original_launch_options = ""
        cleaned_methods: list[str] = []

        self._log(f"cleanup install root: install_root={install_root} markers={[marker.name for marker in marker_paths]}")
        for marker_path in marker_paths:
            metadata = self._read_marker_metadata(marker_path)
            method = metadata.get("method")
            if not method:
                continue
            if not original_launch_options:
                original_launch_options = str(metadata.get("original_launch_options") or "")

            target_dir = marker_path.parent
            self._log(f"cleanup marker metadata: {json.dumps(metadata, sort_keys=True)}")
            self._log_target_state("cleanup before restore", target_dir, method)
            notes.extend(self._restore_method_in_dir(target_dir, method))
            cleaned_methods.append(method)
            self._log_target_state("cleanup after restore", target_dir, method)
            try:
                marker_path.unlink()
                self._log(f"cleanup removed marker: {marker_path}")
            except FileNotFoundError:
                pass

        result = {
            "notes": notes,
            "original_launch_options": original_launch_options,
            "cleaned_methods": cleaned_methods,
        }
        self._log(f"cleanup result: {json.dumps(result, sort_keys=True)}")
        return result

    def _prepare_target_proxy(self, target_dir: Path, method: str) -> bool:
        method = self._normalize_method(method)
        proxy_filename = f"{method}.dll"
        proxy_path = target_dir / proxy_filename
        backup_path = target_dir / f"{proxy_filename}{BACKUP_SUFFIX}"
        backup_created = False

        marker_for_method = target_dir / self._marker_filename(method)
        same_method_already_managed = marker_for_method.exists()

        self._log_target_state("prepare before", target_dir, method)
        self._log(
            f"prepare target proxy: target_dir={target_dir} method={method} same_method_already_managed={same_method_already_managed} "
            f"proxy_is_bundled={self._is_bundled_proxy_file(proxy_path)} backup_exists={backup_path.exists() or backup_path.is_symlink()}"
        )

        if backup_path.exists() or backup_path.is_symlink():
            if not same_method_already_managed:
                stashed_backup = self._unique_stash_path(backup_path, "preexisting-backup")
                backup_path.rename(stashed_backup)
                self._log(f"prepare stashed preexisting backup to {stashed_backup}")

        if proxy_path.exists() or proxy_path.is_symlink():
            if same_method_already_managed and self._is_bundled_proxy_file(proxy_path):
                self._remove_path(proxy_path)
                self._log(f"prepare removed existing managed proxy {proxy_path}")
            elif self._is_bundled_proxy_file(proxy_path):
                self._remove_path(proxy_path)
                self._log(f"prepare removed bundled proxy without same-method marker {proxy_path}")
            else:
                proxy_path.rename(backup_path)
                backup_created = True
                self._log(f"prepare moved existing proxy to backup {backup_path}")

        self._log_target_state("prepare after", target_dir, method)
        return backup_created

    def _is_env_assignment(self, token: str) -> bool:
        if "=" not in token or token.startswith("-"):
            return False
        key_part = token.split("=", 1)[0]
        return "/" not in key_part

    def _parse_launch_option(self, raw_command: str) -> dict:
        if not raw_command or not raw_command.strip():
            return {"env_pairs": [], "prefix": [], "suffix": []}

        try:
            parts = shlex.split(raw_command)
        except ValueError:
            parts = raw_command.split()

        try:
            command_idx = parts.index("%command%")
            left_parts = parts[:command_idx]
            right_parts = parts[command_idx + 1 :]
        except ValueError:
            temp_left: list[str] = []
            temp_right: list[str] = []

            for index, part in enumerate(parts):
                if self._is_env_assignment(part):
                    temp_left.append(part)
                    continue
                if part.startswith("-") or part.startswith("+"):
                    temp_right.append(part)
                    temp_right.extend(parts[index + 1 :])
                    break
                temp_left.append(part)

            left_parts = temp_left
            right_parts = temp_right

        env_pairs: list[tuple[str, str]] = []
        prefix: list[str] = []
        for part in left_parts:
            if self._is_env_assignment(part):
                key, value = part.split("=", 1)
                env_pairs.append((key, value))
            else:
                prefix.append(part)

        return {
            "env_pairs": env_pairs,
            "prefix": prefix,
            "suffix": right_parts,
        }

    def _merge_winedlloverrides(self, existing_value: str, method: str) -> str:
        method = self._normalize_method(method)
        desired_entry = f"{method}=n,b"
        entries = [entry.strip() for entry in (existing_value or "").split(";") if entry.strip()]
        filtered = [entry for entry in entries if not entry.lower().startswith(f"{method.lower()}=")]
        filtered.append(desired_entry)
        return ";".join(filtered)

    def _is_managed_launch_options(self, raw_command: str) -> bool:
        if not raw_command or not raw_command.strip():
            return False

        parsed = self._parse_launch_option(raw_command)
        env_map = {key: value for key, value in parsed["env_pairs"]}
        if set(env_map.keys()) == {"WINEDLLOVERRIDES"} and not parsed["prefix"] and not parsed["suffix"]:
            value = env_map["WINEDLLOVERRIDES"].strip().lower()
            return value in {f"{method}=n,b" for method in SUPPORTED_METHODS}
        return False

    def _preserved_launch_options(self, current_launch_options: str, cleanup_original_launch_options: str = "") -> str:
        if cleanup_original_launch_options and not self._is_managed_launch_options(cleanup_original_launch_options):
            return cleanup_original_launch_options
        if self._is_managed_launch_options(current_launch_options):
            return ""
        return current_launch_options or ""

    def _build_managed_launch_options(self, original_launch_options: str, method: str) -> str:
        parsed = self._parse_launch_option(original_launch_options)
        env_pairs = list(parsed["env_pairs"])
        prefix = list(parsed["prefix"])
        suffix = list(parsed["suffix"])

        existing_winedlloverrides = ""
        other_env_pairs: list[tuple[str, str]] = []
        for key, value in env_pairs:
            if key == "WINEDLLOVERRIDES":
                existing_winedlloverrides = value
            elif key == "SteamDeck":
                continue
            else:
                other_env_pairs.append((key, value))

        merged_env_pairs = [
            ("WINEDLLOVERRIDES", self._merge_winedlloverrides(existing_winedlloverrides, method)),
            ("SteamDeck", "0"),
        ]
        merged_env_pairs.extend(other_env_pairs)

        parts = [f"{key}={value}" for key, value in merged_env_pairs]
        parts.extend(prefix)
        parts.append("%command%")
        parts.extend(suffix)
        return shlex.join(parts)

    def _is_game_running(self, game_info: dict) -> bool:
        install_root = Path(game_info["install_path"])
        candidates = self._candidate_executables(install_root)
        return self._best_running_executable(candidates) is not None

    async def list_installed_games(self) -> dict:
        try:
            games = []
            for game in self._find_installed_games():
                install_root = Path(game["install_path"])
                games.append(
                    {
                        "appid": str(game["appid"]),
                        "name": game["name"],
                        "prefix_exists": install_root.exists(),
                    }
                )
            return {"status": "success", "games": games}
        except Exception as exc:
            self._log(f"list_installed_games failed: {exc}")
            return {"status": "error", "message": str(exc), "games": []}

    async def get_game_status(self, appid: str) -> dict:
        try:
            self._log(f"get_game_status start: appid={appid}")
            game_info = self._game_record(str(appid))
            game_name = game_info["name"] if game_info else str(appid)
            if not game_info:
                return {
                    "status": "success",
                    "appid": str(appid),
                    "name": game_name,
                    "prefix_exists": False,
                    "patched": False,
                    "method": None,
                    "proxy_filename": None,
                    "message": "Game install path could not be resolved.",
                }

            install_root = Path(game_info["install_path"])
            if not install_root.exists():
                return {
                    "status": "success",
                    "appid": str(appid),
                    "name": game_name,
                    "prefix_exists": False,
                    "patched": False,
                    "method": None,
                    "proxy_filename": None,
                    "message": "Game install directory does not exist.",
                    "paths": {
                        "install_root": str(install_root),
                    },
                }

            target_dir, target_exe = self._guess_patch_target(game_info)
            markers = self._find_markers_under_install_root(install_root)
            if not markers:
                return {
                    "status": "success",
                    "appid": str(appid),
                    "name": game_name,
                    "prefix_exists": True,
                    "patched": False,
                    "method": None,
                    "proxy_filename": None,
                    "message": "This game is not currently patched.",
                    "paths": {
                        "install_root": str(install_root),
                        "target_dir": str(target_dir),
                        "target_exe": str(target_exe) if target_exe else "",
                    },
                }

            marker = markers[0]
            metadata = self._read_marker_metadata(marker)
            method = self._normalize_method(metadata.get("method") or "version")
            proxy_filename = f"{method}.dll"
            target_dir = marker.parent
            proxy_path = target_dir / proxy_filename
            patched = proxy_path.exists() or proxy_path.is_symlink()
            self._log(f"get_game_status marker metadata: {json.dumps(metadata, sort_keys=True)}")
            self._log_target_state("get_game_status", target_dir, method)

            message = f"Patched using {proxy_filename}." if patched else f"Managed marker found for {proxy_filename}, but the proxy DLL is missing."
            return {
                "status": "success",
                "appid": str(appid),
                "name": game_name,
                "prefix_exists": True,
                "patched": patched,
                "method": method,
                "proxy_filename": proxy_filename,
                "marker_name": marker.name,
                "message": message,
                "paths": {
                    "install_root": str(install_root),
                    "target_dir": str(target_dir),
                    "target_exe": str(metadata.get("target_exe") or ""),
                },
            }
        except Exception as exc:
            self._log(f"get_game_status failed for {appid}: {exc}")
            return {"status": "error", "message": str(exc)}

    async def patch_game(self, appid: str, method: str, current_launch_options: str = "") -> dict:
        try:
            normalized_method = self._normalize_method(method)
            self._log(
                f"patch_game start: appid={appid} method={normalized_method} current_launch_options={json.dumps(current_launch_options)}"
            )
            asset_path = self._verify_bundled_asset()
            game_info = self._game_record(str(appid))
            if not game_info:
                return {"status": "error", "message": "Game install path could not be resolved."}

            if self._is_game_running(game_info):
                return {"status": "error", "message": "Close the game before patching."}

            install_root = Path(game_info["install_path"])
            if not install_root.exists():
                return {"status": "error", "message": "Game install directory does not exist."}

            target_dir, target_exe = self._guess_patch_target(game_info)
            target_dir.mkdir(parents=True, exist_ok=True)
            self._log(
                f"patch_game target selection: install_root={install_root} target_dir={target_dir} target_exe={target_exe}"
            )
            self._log_target_state("patch before cleanup", target_dir, normalized_method)

            cleanup_result = self._cleanup_install_root(install_root)
            preserved_launch_options = self._preserved_launch_options(
                current_launch_options or "",
                str(cleanup_result.get("original_launch_options") or ""),
            )
            self._log(
                f"patch after cleanup: preserved_launch_options={json.dumps(preserved_launch_options)} cleanup_result={json.dumps(cleanup_result, sort_keys=True)}"
            )

            backup_created = self._prepare_target_proxy(target_dir, normalized_method)
            target_proxy_path = target_dir / f"{normalized_method}.dll"
            self._log(f"patch copy start: source={asset_path} target={target_proxy_path}")
            shutil.copy2(asset_path, target_proxy_path)
            self._log_target_state("patch after copy", target_dir, normalized_method)

            copied_hash = self._file_sha256(target_proxy_path)
            if copied_hash.lower() != BUNDLED_ASSET_SHA256.lower():
                raise RuntimeError(
                    f"Copied proxy hash mismatch for {target_proxy_path.name}: expected {BUNDLED_ASSET_SHA256}, got {copied_hash}"
                )

            marker_path = target_dir / self._marker_filename(normalized_method)
            self._write_marker_metadata(
                marker_path,
                appid=str(appid),
                game_name=game_info["name"],
                method=normalized_method,
                target_dir=target_dir,
                target_exe=target_exe,
                original_launch_options=preserved_launch_options,
                backup_created=backup_created,
            )
            self._log(f"patch wrote marker: {json.dumps(self._read_marker_metadata(marker_path), sort_keys=True)}")

            managed_launch_options = self._build_managed_launch_options(preserved_launch_options, normalized_method)
            self._log(f"patch managed launch options: {json.dumps(managed_launch_options)}")

            result = {
                "status": "success",
                "appid": str(appid),
                "name": game_info["name"],
                "method": normalized_method,
                "proxy_filename": f"{normalized_method}.dll",
                "marker_name": marker_path.name,
                "launch_options": managed_launch_options,
                "original_launch_options": preserved_launch_options,
                "message": f"Patched {game_info['name']} using {normalized_method}.dll in the game directory.",
                "paths": {
                    "install_root": str(install_root),
                    "target_dir": str(target_dir),
                    "target_exe": str(target_exe) if target_exe else "",
                    "proxy": str(target_proxy_path),
                    "marker": str(marker_path),
                },
            }
            self._log_target_state("patch success final state", target_dir, normalized_method)
            self._log(f"patch success: {json.dumps(result, sort_keys=True)}")
            return result
        except Exception as exc:
            decky.logger.error(f"[DLSS Enabler] patch_game failed for {appid}: {exc}")
            return {"status": "error", "message": str(exc)}

    async def unpatch_game(self, appid: str) -> dict:
        try:
            self._log(f"unpatch_game start: appid={appid}")
            game_info = self._game_record(str(appid))
            if not game_info:
                return {"status": "success", "appid": str(appid), "launch_options": "", "message": "Game install path could not be resolved."}

            if self._is_game_running(game_info):
                return {"status": "error", "message": "Close the game before unpatching."}

            install_root = Path(game_info["install_path"])
            if not install_root.exists():
                return {
                    "status": "success",
                    "appid": str(appid),
                    "name": game_info["name"],
                    "launch_options": "",
                    "message": "Game install directory does not exist.",
                }

            markers = self._find_markers_under_install_root(install_root)
            self._log(f"unpatch markers: {[marker.name for marker in markers]}")
            for marker in markers:
                marker_method = self._marker_method_from_name(marker.name)
                if marker_method:
                    self._log_target_state("unpatch before cleanup", marker.parent, marker_method)

            if not markers:
                return {
                    "status": "success",
                    "appid": str(appid),
                    "name": game_info["name"],
                    "launch_options": "",
                    "message": "No managed DLSS Enabler marker was found for this game.",
                    "paths": {
                        "install_root": str(install_root),
                    },
                }

            cleanup_result = self._cleanup_install_root(install_root)
            restored_launch_options = str(cleanup_result.get("original_launch_options") or "")
            if self._is_managed_launch_options(restored_launch_options):
                restored_launch_options = ""

            cleaned_methods = cleanup_result.get("cleaned_methods") or []
            methods_display = ", ".join(f"{method}.dll" for method in cleaned_methods) if cleaned_methods else "managed proxy"
            result = {
                "status": "success",
                "appid": str(appid),
                "name": game_info["name"],
                "launch_options": restored_launch_options,
                "message": f"Unpatched {game_info['name']} and restored {methods_display}.",
                "paths": {
                    "install_root": str(install_root),
                },
                "notes": cleanup_result.get("notes") or [],
            }
            self._log(f"unpatch success: {json.dumps(result, sort_keys=True)}")
            return result
        except Exception as exc:
            decky.logger.error(f"[DLSS Enabler] unpatch_game failed for {appid}: {exc}")
            return {"status": "error", "message": str(exc)}
