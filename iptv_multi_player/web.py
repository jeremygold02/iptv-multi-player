from __future__ import annotations

from pathlib import Path
import threading
from typing import Any
import urllib.error

from flask import Flask, jsonify, render_template, request, send_file

from . import config
from .config import API_SPORTS_KEY_NAME, APP_DIR, ASSET_DIR, MAX_QUEUE_ITEMS, QUEUE_EXPORT_PATH
from .players import launch_player, normalize_player_id, normalize_player_settings, player_label, public_players, sanitize_players
from .playlists import (
    fetch_playlist_url,
    remove_source_file,
    replace_source_channels,
    source_content,
    upsert_file_source,
    upsert_url_source,
)
from .recording import (
    FfmpegInstallError,
    FfmpegMissingError,
    RecordingError,
    install_ffmpeg,
    public_recording_config,
    recording_manager,
    sanitize_clip_seconds,
    sanitize_recording_quality,
)
from .sports import channels_with_pending_games, enrich_channels_with_games
from .state import api_sports_key, default_state, now_ts, read_state, write_env_value, write_state
from .updates import UpdateError, can_install_updates, check_for_update, install_update, update_progress
from .version import APP_NAME, APP_VERSION, GITHUB_REPO_URL

app = Flask(
    __name__,
    template_folder=str(ASSET_DIR / "templates"),
    static_folder=str(ASSET_DIR / "static"),
)
state_lock = threading.RLock()
sports_refresh_lock = threading.Lock()


def app_icon_path() -> Path | None:
    for icon_path in (APP_DIR / "icon.ico", ASSET_DIR / "icon.ico"):
        if icon_path.is_file():
            return icon_path
    return None


@app.get("/icon.ico")
def app_icon():
    icon_path = app_icon_path()
    if not icon_path:
        return "", 404
    return send_file(icon_path, mimetype="image/x-icon")


def public_state(state: dict[str, Any] | None = None, enrich_games: bool = False) -> dict[str, Any]:
    state = state or read_state()
    settings = state.get("settings", {})
    sports_key = api_sports_key()
    players = public_players(settings)
    pinned_categories = {
        str(category).strip()
        for category in settings.get("pinned_categories", [])
        if str(category).strip()
    }
    categories = sorted(
        {channel.get("group") or "Ungrouped" for channel in state["channels"].values()}
        | pinned_categories
    )
    channel_values = list(state["channels"].values())
    if enrich_games:
        channels, sports_meta = enrich_channels_with_games(channel_values)
    else:
        channels, sports_meta = channels_with_pending_games(channel_values)
    return {
        "sources": state["sources"],
        "channels": channels,
        "favorites": state["favorites"],
        "queue": state["queue"],
        "categories": categories,
        "sports": sports_meta,
        "selected_source_id": state.get("selected_source_id", "all"),
        "settings": settings,
        "players": players,
        "api_sports": {
            "key": sports_key,
            "configured": bool(sports_key),
        },
        "app": {
            "name": APP_NAME,
            "version": APP_VERSION,
            "repo_url": GITHUB_REPO_URL,
        },
        "recording": {
            **public_recording_config(settings),
            "status": recording_manager.status(),
        },
        "runtime": {
            "desktop": config.DESKTOP_MODE,
            "can_install_updates": can_install_updates(),
        },
    }


def json_error(message: str, status: int = 400):
    return jsonify({"success": False, "error": message}), status


def recording_settings_from_payload(settings: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    overrides = payload.get("recording_options")
    if not isinstance(overrides, dict):
        overrides = {}

    next_settings = dict(settings)
    if "ffmpeg_path" in overrides:
        next_settings["ffmpeg_path"] = str(overrides.get("ffmpeg_path") or "").strip()
    if "recording_dir" in overrides:
        next_settings["recording_dir"] = str(overrides.get("recording_dir") or "").strip()
    if "recording_default_quality" in overrides:
        next_settings["recording_default_quality"] = sanitize_recording_quality(overrides.get("recording_default_quality"))
    if "recording_clip_seconds" in overrides:
        next_settings["recording_clip_seconds"] = sanitize_clip_seconds(overrides.get("recording_clip_seconds"))
    return next_settings


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    with state_lock:
        return jsonify({"success": True, "data": public_state()})


@app.post("/api/sports/refresh")
def api_sports_refresh():
    try:
        with sports_refresh_lock:
            with state_lock:
                state = read_state()
            return jsonify({"success": True, "data": public_state(state=state, enrich_games=True)})
    except Exception as exc:  # noqa: BLE001 - game enrichment should not crash the app shell
        return json_error(f"Could not refresh game data: {exc}", 502)


@app.get("/api/update/check")
@app.get("/api/update-check")
def api_update_check():
    try:
        return jsonify({"success": True, "data": check_for_update()})
    except Exception as exc:  # noqa: BLE001 - UI should get a concise failure message
        return json_error(f"Could not check for updates: {exc}", 502)


@app.post("/api/update/install")
def api_update_install():
    try:
        return jsonify({"success": True, "data": install_update()})
    except UpdateError as exc:
        return json_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return json_error(f"Could not install update: {exc}", 502)


@app.get("/api/update/progress")
def api_update_progress():
    return jsonify({"success": True, "data": update_progress()})


@app.post("/api/import-file")
def api_import_file():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return json_error("Choose an M3U file to import.")

    content = uploaded.read().decode("utf-8-sig", errors="replace")
    name = request.form.get("name") or Path(uploaded.filename).stem or "Local Playlist"
    with state_lock:
        source = upsert_file_source(name, content)
        return jsonify({"success": True, "data": {"source": source, "state": public_state()}})


@app.post("/api/import-url")
def api_import_url():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    name = str(payload.get("name", "")).strip()
    if not url.lower().startswith(("http://", "https://")):
        return json_error("Enter a valid http or https playlist URL.")

    try:
        content = fetch_playlist_url(url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return json_error(f"Could not fetch playlist: {exc}")

    with state_lock:
        source = upsert_url_source(name, url, content)
        return jsonify({"success": True, "data": {"source": source, "state": public_state()}})


@app.post("/api/refresh")
def api_refresh_all():
    with state_lock:
        state = read_state()
        refreshed = []
        errors = []
        for source in state["sources"]:
            try:
                content = source_content(source)
                count = replace_source_channels(state, source, content)
                refreshed.append({"id": source["id"], "name": source["name"], "count": count})
            except Exception as exc:  # noqa: BLE001 - endpoint should report per-source failures
                errors.append({"id": source.get("id"), "name": source.get("name"), "error": str(exc)})
        write_state(state)
        return jsonify({"success": True, "data": {"refreshed": refreshed, "errors": errors, "state": public_state()}})


@app.post("/api/sources/<source_id>/refresh")
def api_refresh_source(source_id: str):
    with state_lock:
        state = read_state()
        source = next((item for item in state["sources"] if item["id"] == source_id), None)
        if source is None:
            return json_error("Source not found.", 404)
        try:
            content = source_content(source)
            count = replace_source_channels(state, source, content)
            write_state(state)
        except Exception as exc:  # noqa: BLE001
            return json_error(f"Could not refresh source: {exc}")
        return jsonify({"success": True, "data": {"count": count, "state": public_state()}})


@app.patch("/api/sources/<source_id>")
def api_update_source(source_id: str):
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    if not name:
        return json_error("Enter a playlist name.")

    with state_lock:
        state = read_state()
        source = next((item for item in state["sources"] if item["id"] == source_id), None)
        if source is None:
            return json_error("Source not found.", 404)

        source["name"] = name
        source["updated_at"] = now_ts()
        write_state(state)
        return jsonify({"success": True, "data": {"source": source, "state": public_state()}})


@app.delete("/api/sources/<source_id>")
def api_delete_source(source_id: str):
    with state_lock:
        state = read_state()
        source = next((item for item in state["sources"] if item["id"] == source_id), None)
        if source is None:
            return json_error("Source not found.", 404)
        state["sources"] = [item for item in state["sources"] if item["id"] != source_id]
        state["channels"] = {
            channel_id: channel
            for channel_id, channel in state["channels"].items()
            if channel.get("source_id") != source_id
        }
        state["queue"] = [channel_id for channel_id in state["queue"] if channel_id in state["channels"]]
        state["favorites"] = [channel_id for channel_id in state["favorites"] if channel_id in state["channels"]]
        if state.get("selected_source_id") == source_id:
            state["selected_source_id"] = "all"
        write_state(state)
        remove_source_file(source)
        return jsonify({"success": True, "data": {"state": public_state()}})


@app.post("/api/channels/<channel_id>/favorite")
def api_toggle_favorite(channel_id: str):
    with state_lock:
        state = read_state()
        if channel_id not in state["channels"]:
            return json_error("Channel not found.", 404)
        favorites = set(state.get("favorites", []))
        if channel_id in favorites:
            favorites.remove(channel_id)
            state["channels"][channel_id]["favorite"] = False
        else:
            favorites.add(channel_id)
            state["channels"][channel_id]["favorite"] = True
        state["favorites"] = sorted(favorites)
        write_state(state)
        return jsonify({"success": True, "data": {"state": public_state()}})


@app.post("/api/queue/add")
def api_queue_add():
    payload = request.get_json(silent=True) or {}
    channel_id = str(payload.get("channel_id", "")).strip()
    with state_lock:
        state = read_state()
        if channel_id not in state["channels"]:
            return json_error("Channel not found.", 404)
        queue = state.get("queue", [])
        if channel_id not in queue:
            if len(queue) >= MAX_QUEUE_ITEMS:
                return json_error(f"Queue is limited to {MAX_QUEUE_ITEMS} streams.")
            queue.append(channel_id)
        state["queue"] = queue
        write_state(state)
        return jsonify({"success": True, "data": {"state": public_state()}})


@app.post("/api/queue/remove")
def api_queue_remove():
    payload = request.get_json(silent=True) or {}
    channel_id = str(payload.get("channel_id", "")).strip()
    with state_lock:
        state = read_state()
        state["queue"] = [item for item in state.get("queue", []) if item != channel_id]
        write_state(state)
        return jsonify({"success": True, "data": {"state": public_state()}})


@app.post("/api/queue/reorder")
def api_queue_reorder():
    payload = request.get_json(silent=True) or {}
    channel_id = str(payload.get("channel_id", "")).strip()
    direction = str(payload.get("direction", "")).strip()
    with state_lock:
        state = read_state()
        queue = state.get("queue", [])
        if channel_id not in queue:
            return json_error("Channel not in queue.", 404)
        index = queue.index(channel_id)
        target = index - 1 if direction == "up" else index + 1
        if 0 <= target < len(queue):
            queue[index], queue[target] = queue[target], queue[index]
        state["queue"] = queue
        write_state(state)
        return jsonify({"success": True, "data": {"state": public_state()}})


@app.post("/api/queue/clear")
def api_queue_clear():
    with state_lock:
        state = read_state()
        state["queue"] = []
        write_state(state)
        return jsonify({"success": True, "data": {"state": public_state()}})


@app.post("/api/open")
def api_open_channel():
    payload = request.get_json(silent=True) or {}
    channel_id = str(payload.get("channel_id", "")).strip()
    with state_lock:
        state = read_state()
        channel = state["channels"].get(channel_id)
        if channel is None:
            return json_error("Channel not found.", 404)
        try:
            player, executable = launch_player([channel["url"]], state["settings"], payload.get("player"))
        except Exception as exc:  # noqa: BLE001
            return json_error(str(exc))
        return jsonify({"success": True, "data": {"path": str(executable), "player": player, "label": player_label(player, state["settings"])}})


@app.get("/api/recording/status")
def api_recording_status():
    return jsonify({"success": True, "data": recording_manager.status()})


@app.post("/api/recording/prepare")
def api_recording_prepare():
    payload = request.get_json(silent=True) or {}
    channel_id = str(payload.get("channel_id", "")).strip()
    with state_lock:
        state = read_state()
        channel = state["channels"].get(channel_id)
        if channel is None:
            return json_error("Channel not found.", 404)
        settings = recording_settings_from_payload(state["settings"], payload)
    try:
        data = recording_manager.prepare(channel, settings)
        return jsonify({"success": True, "data": data})
    except RecordingError as exc:
        return json_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return json_error(f"Could not prepare recording: {exc}", 502)


@app.post("/api/recording/start")
def api_recording_start():
    payload = request.get_json(silent=True) or {}
    channel_id = str(payload.get("channel_id", "")).strip()
    quality_id = str(payload.get("quality_id", "") or "").strip() or None
    with state_lock:
        state = read_state()
        channel = state["channels"].get(channel_id)
        if channel is None:
            return json_error("Channel not found.", 404)
        settings = recording_settings_from_payload(state["settings"], payload)
    clip_seconds = (
        sanitize_clip_seconds(payload.get("clip_seconds") or settings.get("recording_clip_seconds"))
        if payload.get("clip_enabled")
        else 0
    )
    try:
        return jsonify({"success": True, "data": recording_manager.start(channel, settings, quality_id, clip_seconds)})
    except FfmpegMissingError as exc:
        return json_error(str(exc), 409)
    except RecordingError as exc:
        return json_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return json_error(f"Could not start recording: {exc}", 502)


@app.post("/api/recording/stop")
def api_recording_stop():
    payload = request.get_json(silent=True) or {}
    channel_id = str(payload.get("channel_id", "") or "").strip() or None
    try:
        return jsonify({"success": True, "data": recording_manager.stop(channel_id)})
    except Exception as exc:  # noqa: BLE001
        return json_error(f"Could not stop recording: {exc}", 502)


@app.post("/api/recording/clip/save")
def api_recording_save_clip():
    payload = request.get_json(silent=True) or {}
    channel_id = str(payload.get("channel_id", "") or "").strip() or None
    with state_lock:
        state = read_state()
        settings = state["settings"]
    try:
        return jsonify({"success": True, "data": recording_manager.save_clip(settings, channel_id)})
    except RecordingError as exc:
        return json_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return json_error(f"Could not save clip: {exc}", 502)


@app.post("/api/recording/open")
def api_recording_open():
    payload = request.get_json(silent=True) or {}
    channel_id = str(payload.get("channel_id", "") or "").strip() or None
    with state_lock:
        state = read_state()
        settings = state["settings"]
    try:
        return jsonify({"success": True, "data": recording_manager.open_recording(settings, payload.get("player"), channel_id)})
    except Exception as exc:  # noqa: BLE001
        return json_error(str(exc))


@app.post("/api/recording/reveal")
def api_recording_reveal():
    payload = request.get_json(silent=True) or {}
    channel_id = str(payload.get("channel_id", "") or "").strip() or None
    try:
        return jsonify({"success": True, "data": recording_manager.reveal_recording(channel_id)})
    except Exception as exc:  # noqa: BLE001
        return json_error(str(exc))


@app.post("/api/open-queue")
def api_open_queue():
    with state_lock:
        state = read_state()
        urls = [
            state["channels"][channel_id]["url"]
            for channel_id in state.get("queue", [])
            if channel_id in state["channels"]
        ]
        try:
            player, executable = launch_player(urls, state["settings"])
        except Exception as exc:  # noqa: BLE001
            return json_error(str(exc))
        return jsonify({"success": True, "data": {"path": str(executable), "player": player, "label": player_label(player, state["settings"]), "count": len(urls)}})


@app.get("/api/export-queue")
def api_export_queue():
    with state_lock:
        state = read_state()
        lines = ["#EXTM3U"]
        for channel_id in state.get("queue", []):
            channel = state["channels"].get(channel_id)
            if not channel:
                continue
            lines.append(f'#EXTINF:-1 tvg-name="{channel["name"]}" group-title="{channel["group"]}",{channel["name"]}')
            lines.append(channel["url"])
        QUEUE_EXPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return send_file(QUEUE_EXPORT_PATH, as_attachment=True, download_name="iptv-multi-player-queue.m3u")


@app.post("/api/select-player-executable")
def api_select_player_executable():
    payload = request.get_json(silent=True) or {}
    player = str(payload.get("player", "")).strip().lower()
    if not config.DESKTOP_MODE:
        return json_error("The file picker is available in the desktop app.")

    try:
        import webview
    except ImportError:
        return json_error("The file picker is unavailable because pywebview is not installed.")

    windows = getattr(webview, "windows", [])
    if not windows:
        return json_error("The app window is not ready.")

    try:
        file_dialog = getattr(webview, "FileDialog", None)
        dialog_type = file_dialog.OPEN if file_dialog else webview.OPEN_DIALOG
        selection = windows[0].create_file_dialog(
            dialog_type,
            allow_multiple=False,
            file_types=("Executable files (*.exe)", "All files (*.*)"),
        )
    except Exception as exc:  # noqa: BLE001 - native dialog failures should stay user-facing
        return json_error(f"Could not open file picker: {exc}")

    if not selection:
        return jsonify({"success": True, "data": {"path": ""}})

    selected_path = Path(selection[0])
    if selected_path.suffix.lower() != ".exe":
        return json_error("Choose an .exe file.")
    if not selected_path.is_file():
        return json_error("Selected file was not found.")

    return jsonify({"success": True, "data": {"path": str(selected_path), "player": player}})


@app.post("/api/select-ffmpeg-executable")
def api_select_ffmpeg_executable():
    if not config.DESKTOP_MODE:
        return json_error("The file picker is available in the desktop app.")

    try:
        import webview
    except ImportError:
        return json_error("The file picker is unavailable because pywebview is not installed.")

    windows = getattr(webview, "windows", [])
    if not windows:
        return json_error("The app window is not ready.")

    try:
        file_dialog = getattr(webview, "FileDialog", None)
        dialog_type = file_dialog.OPEN if file_dialog else webview.OPEN_DIALOG
        selection = windows[0].create_file_dialog(
            dialog_type,
            allow_multiple=False,
            file_types=("Executable files (*.exe)", "All files (*.*)"),
        )
    except Exception as exc:  # noqa: BLE001
        return json_error(f"Could not open file picker: {exc}")

    if not selection:
        return jsonify({"success": True, "data": {"path": ""}})

    selected_path = Path(selection[0])
    if selected_path.suffix.lower() != ".exe":
        return json_error("Choose an .exe file.")
    if not selected_path.is_file():
        return json_error("Selected file was not found.")

    return jsonify({"success": True, "data": {"path": str(selected_path)}})


@app.post("/api/select-recording-directory")
def api_select_recording_directory():
    if not config.DESKTOP_MODE:
        return json_error("The folder picker is available in the desktop app.")

    try:
        import webview
    except ImportError:
        return json_error("The folder picker is unavailable because pywebview is not installed.")

    windows = getattr(webview, "windows", [])
    if not windows:
        return json_error("The app window is not ready.")

    try:
        file_dialog = getattr(webview, "FileDialog", None)
        dialog_type = getattr(file_dialog, "FOLDER", None) if file_dialog else getattr(webview, "FOLDER_DIALOG", None)
        if dialog_type is None:
            return json_error("The folder picker is unavailable in this pywebview version.")
        selection = windows[0].create_file_dialog(dialog_type, allow_multiple=False)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"Could not open folder picker: {exc}")

    if not selection:
        return jsonify({"success": True, "data": {"path": ""}})

    selected_path = Path(selection[0])
    if not selected_path.is_dir():
        return json_error("Selected folder was not found.")

    return jsonify({"success": True, "data": {"path": str(selected_path)}})


@app.post("/api/ffmpeg/install")
def api_ffmpeg_install():
    try:
        info = install_ffmpeg()
    except FfmpegInstallError as exc:
        return json_error(str(exc), 502)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"Could not install FFmpeg: {exc}", 502)

    with state_lock:
        state = read_state()
        settings = state.setdefault("settings", default_state()["settings"])
        settings["ffmpeg_path"] = info.ffmpeg_path
        write_state(state)
        return jsonify({"success": True, "data": {"ffmpeg": info.__dict__, "state": public_state()}})


@app.post("/api/settings")
def api_settings():
    payload = request.get_json(silent=True) or {}
    allowed = {
        "grid_size",
        "selected_player",
        "players",
        "ffmpeg_path",
        "recording_dir",
        "recording_default_quality",
        "recording_clip_seconds",
        "auto_open_queue",
        "ui_zoom",
        "ui_sidebar_width",
        "pinned_categories",
        "custom_filters",
    }
    with state_lock:
        state = read_state()
        settings = state.setdefault("settings", default_state()["settings"])
        if "api_sports_key" in payload:
            write_env_value(API_SPORTS_KEY_NAME, str(payload.get("api_sports_key") or ""))
        if "players" in payload:
            settings["players"] = sanitize_players(payload.get("players"))
        for key in allowed:
            if key in payload:
                if key == "players":
                    continue
                if key == "selected_player":
                    settings[key] = normalize_player_id(payload[key], settings)
                elif key == "recording_default_quality":
                    settings[key] = sanitize_recording_quality(payload[key])
                elif key == "recording_clip_seconds":
                    settings[key] = sanitize_clip_seconds(payload[key])
                elif key.endswith("_path"):
                    settings[key] = str(payload[key] or "").strip()
                elif key.endswith("_flags"):
                    settings[key] = str(payload[key] or "").strip()[:2000]
                elif key == "recording_dir":
                    settings[key] = str(payload[key] or "").strip()
                elif key == "pinned_categories":
                    categories = payload.get(key) if isinstance(payload.get(key), list) else []
                    pinned_categories = []
                    seen_categories = set()
                    for category in categories:
                        category_name = str(category).strip()
                        if category_name and category_name not in seen_categories:
                            pinned_categories.append(category_name)
                            seen_categories.add(category_name)
                    settings[key] = pinned_categories
                elif key == "custom_filters":
                    filters = payload.get(key) if isinstance(payload.get(key), list) else []
                    custom_filters = []
                    seen_filter_ids = set()
                    for item in filters:
                        if not isinstance(item, dict):
                            continue
                        filter_id = "".join(
                            char
                            for char in str(item.get("id", "")).strip()
                            if char.isalnum() or char in {"-", "_"}
                        )
                        name = str(item.get("name", "")).strip()
                        category = str(item.get("category", "all")).strip() or "all"
                        terms = []
                        seen_terms = set()
                        raw_terms = item.get("terms") if isinstance(item.get("terms"), list) else []
                        for term in raw_terms:
                            value = str(term).strip()
                            normalized = value.lower()
                            if value and normalized not in seen_terms:
                                terms.append(value)
                                seen_terms.add(normalized)
                        if filter_id and name and terms and filter_id not in seen_filter_ids:
                            custom_filters.append({
                                "id": filter_id,
                                "name": name,
                                "category": category,
                                "terms": terms,
                            })
                            seen_filter_ids.add(filter_id)
                    settings[key] = custom_filters
                else:
                    settings[key] = payload[key]
        state["settings"] = normalize_player_settings(settings)
        write_state(state)
        return jsonify({"success": True, "data": {"state": public_state()}})
