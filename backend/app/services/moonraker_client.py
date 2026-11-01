"""Creality / Klipper printer client (Moonraker HTTP API).

The Ender-3 V3 KE, V3 CoreXZ, V3 Plus and the K1/K2 families run Klipper with
Moonraker in front of it. This module gives such a printer the same face as
``BambuMQTTClient`` so ``PrinterManager``, the status route, the WebSocket
fan-out and the print-lifecycle callbacks (archive on start/complete,
notifications, smart plugs ...) work unchanged.

Design notes
------------
* **Polling, not a socket.** One daemon thread polls
  ``/printer/objects/query`` every ``POLL_INTERVAL`` seconds. It mirrors the
  paho network thread of the Bambu client: callbacks fire from that thread and
  the manager hops back onto the event loop with ``run_coroutine_threadsafe``.
* **Same vocabulary.** Klipper's ``print_stats.state`` is translated to the
  Bambu ``gcode_state`` names the rest of Bambuddy keys on (``IDLE`` /
  ``RUNNING`` / ``PAUSE`` / ``FINISH`` / ``FAILED``), and temperatures use the
  ``nozzle`` / ``bed`` / ``*_target`` keys.
* **No Bambu-only features.** There is no AMS, no HMS, no K-profiles and no
  FTPS; ``PrinterState.ftp_capable`` is False so the archive code records a
  name-and-timing entry instead of sweeping for a 3MF.

Everything here is plain HTTP against Moonraker's documented API
(https://moonraker.readthedocs.io/en/latest/web_api/).
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from backend.app.services.bambu_mqtt import MQTTLogEntry, PrinterState

logger = logging.getLogger(__name__)

PRINTER_TYPE_CREALITY = "creality"
DEFAULT_MOONRAKER_PORT = 7125

# Reasons a probe/connection can fail; surfaced as ``last_connect_error`` so the
# add-printer flow can say something more useful than "could not connect".
ERROR_UNREACHABLE = "unreachable"
ERROR_UNAUTHORIZED = "unauthorized"
ERROR_NOT_MOONRAKER = "not_moonraker"
ERROR_KLIPPER_NOT_READY = "klipper_not_ready"
ERROR_HTTP = "http_error"

# Klipper print_stats.state -> Bambu gcode_state vocabulary used app-wide.
STATE_MAP = {
    "standby": "IDLE",
    "printing": "RUNNING",
    "paused": "PAUSE",
    "complete": "FINISH",
    "error": "FAILED",
    # Klipper keeps "cancelled" until the next job starts. Bambu reports an
    # aborted print as RUNNING -> IDLE, which on_print_complete turns into
    # status "aborted", so IDLE is the right equivalent.
    "cancelled": "IDLE",
}

# Bambu speed levels (1 silent .. 4 ludicrous) -> M220 percentages.
SPEED_LEVEL_PERCENT = {1: 50, 2: 100, 3: 124, 4: 166}
SPEED_PERCENT_LEVEL = [(75, 1), (112, 2), (145, 3)]  # <= threshold -> level, else 4

# Klipper objects polled on every tick. Optional ones (fan, display_status ...)
# are filtered against /printer/objects/list at connect so a stripped-down
# config does not make the whole query fail.
CORE_OBJECTS = ("print_stats", "virtual_sdcard", "extruder", "heater_bed")
OPTIONAL_OBJECTS = ("display_status", "fan", "gcode_move", "toolhead", "webhooks", "idle_timeout")


def _slug_name(filename: str | None) -> str | None:
    """``/gcodes/sub/benchy.gcode`` -> ``benchy`` (the display/subtask name)."""
    if not filename:
        return None
    name = filename.replace("\\", "/").split("/")[-1]
    for suffix in (".gcode", ".gco", ".g", ".3mf"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name or None


def speed_percent_to_level(percent: float) -> int:
    for threshold, level in SPEED_PERCENT_LEVEL:
        if percent <= threshold:
            return level
    return 4


class MoonrakerClient:
    """A Creality/Klipper printer reachable through Moonraker."""

    POLL_INTERVAL = 2.0  # seconds between status polls while connected
    RETRY_INTERVAL = 5.0  # seconds between attempts while unreachable
    HTTP_TIMEOUT = 5.0
    STALE_TIMEOUT = 20.0  # no successful poll for this long -> not connected
    FAILURES_BEFORE_DISCONNECT = 3

    printer_type = PRINTER_TYPE_CREALITY

    def __init__(
        self,
        ip_address: str,
        serial_number: str,
        access_code: str = "",
        model: str | None = None,
        port: int | None = None,
        on_state_change: Callable[[PrinterState], None] | None = None,
        on_print_start: Callable[[dict], None] | None = None,
        on_print_complete: Callable[[dict], None] | None = None,
        on_layer_change: Callable[[int], None] | None = None,
        on_print_progress: Callable[[int], None] | None = None,
        on_bed_temp_update: Callable[[float], None] | None = None,
        on_print_running_observed: Callable[[dict], None] | None = None,
        on_finish_photo_moment: Callable[[dict], None] | None = None,
        **_bambu_only_callbacks: object,
    ):
        # ``**_bambu_only_callbacks`` swallows the AMS / drying / assignment
        # callbacks PrinterManager wires for Bambu printers; a Moonraker
        # printer has none of those events.
        self.ip_address = ip_address
        self.serial_number = serial_number
        self.access_code = access_code  # unused: stock Creality Moonraker trusts the LAN
        self.model = model
        self.port = int(port) if port else DEFAULT_MOONRAKER_PORT
        self.on_state_change = on_state_change
        self.on_print_start = on_print_start
        self.on_print_complete = on_print_complete
        self.on_layer_change = on_layer_change
        self.on_print_progress = on_print_progress
        self.on_bed_temp_update = on_bed_temp_update
        self.on_print_running_observed = on_print_running_observed
        self.on_finish_photo_moment = on_finish_photo_moment

        self.state = PrinterState(ftp_capable=False)
        self.state.raw_data = {"printer_type": PRINTER_TYPE_CREALITY}
        self.last_connect_error: str | None = None
        self.last_dispatch_subtask_id: str | None = None  # parity with BambuMQTTClient
        self._drying_targets: dict[int, dict] = {}  # parity: PrinterManager reads it
        self._last_message_time: float = 0.0
        self._report_messages_since_connect: int = 0

        self._base_url = f"http://{ip_address}:{self.port}"
        self._http: httpx.Client | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._consecutive_failures = 0
        self._handshake_done = False
        self._query_objects: list[str] = list(CORE_OBJECTS)
        self._light_pin: str | None = None
        self._state_before_power_off: str | None = None

        # print lifecycle tracking (mirrors BambuMQTTClient's bookkeeping)
        self._previous_state: str | None = None
        self._previous_filename: str | None = None
        self._was_running = False
        self._completion_triggered = False
        self._first_report_seen = False
        self._last_layer = 0
        self._last_progress_int = -1
        self._last_valid_progress = 0.0
        self._last_valid_layer = 0
        self._file_meta: dict = {}
        self._file_meta_for: str | None = None

        self._message_log: deque[MQTTLogEntry] = deque(maxlen=100)
        self._logging_enabled = False

    # ------------------------------------------------------------------ lifecycle

    def connect(self, loop=None) -> None:  # noqa: ARG002 - loop kept for interface parity
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._http = httpx.Client(timeout=self.HTTP_TIMEOUT, follow_redirects=False)
        self._thread = threading.Thread(target=self._run, name=f"moonraker-{self.ip_address}", daemon=True)
        self._thread.start()

    def disconnect(self, timeout: float = 0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and timeout and thread is not threading.current_thread():
            thread.join(timeout)
        http, self._http = self._http, None
        if http is not None:
            try:
                http.close()
            except Exception:  # noqa: BLE001 - best effort teardown
                pass
        self.state.connected = False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._handshake_done:
                    self._handshake()
                self._poll_once()
                self._consecutive_failures = 0
            except _ConnectError as exc:
                self._register_failure(exc.slug, str(exc))
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                self._register_failure(ERROR_UNREACHABLE, f"{type(exc).__name__}: {exc}")
            delay = self.POLL_INTERVAL if self.state.connected else self.RETRY_INTERVAL
            self._wake.wait(delay)
            self._wake.clear()

    def _register_failure(self, slug: str, detail: str) -> None:
        self._consecutive_failures += 1
        self.last_connect_error = slug
        if self._consecutive_failures == 1 or self._consecutive_failures % 30 == 0:
            logger.warning("[%s] Moonraker %s: %s", self.ip_address, slug, detail)
        if self.state.connected and self._consecutive_failures >= self.FAILURES_BEFORE_DISCONNECT:
            self.state.connected = False
            self._handshake_done = False  # re-discover objects after an outage
            self._emit_state()

    # ------------------------------------------------------------------ HTTP

    def _request(self, method: str, path: str, **kwargs) -> dict:
        http = self._http
        if http is None:
            raise _ConnectError(ERROR_UNREACHABLE, "client closed")
        try:
            response = http.request(method, f"{self._base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise _ConnectError(ERROR_UNREACHABLE, str(exc) or type(exc).__name__) from exc
        if response.status_code in (401, 403):
            raise _ConnectError(ERROR_UNAUTHORIZED, f"HTTP {response.status_code} on {path}")
        if response.status_code >= 400:
            raise _ConnectError(ERROR_HTTP, f"HTTP {response.status_code} on {path}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise _ConnectError(ERROR_NOT_MOONRAKER, f"non-JSON reply on {path}") from exc
        if not isinstance(payload, dict) or "result" not in payload:
            raise _ConnectError(ERROR_NOT_MOONRAKER, f"unexpected reply on {path}")
        return payload["result"]

    def _handshake(self) -> None:
        info = self._request("GET", "/printer/info")
        if info.get("state") not in ("ready", "startup"):
            # "error" / "shutdown": Moonraker is up but Klipper is not usable.
            self.state.raw_data["klipper_state_message"] = info.get("state_message")
            raise _ConnectError(ERROR_KLIPPER_NOT_READY, str(info.get("state_message") or info.get("state")))
        self.state.firmware_version = info.get("software_version")
        self.state.raw_data["hostname"] = info.get("hostname")
        self.state.raw_data["klipper_state"] = info.get("state")

        listed = self._request("GET", "/printer/objects/list").get("objects", [])
        objects = list(CORE_OBJECTS) + [o for o in OPTIONAL_OBJECTS if o in listed]
        # Chamber sensor, if this model has one (K1 / K2 yes, Ender-3 V3 KE no).
        for name in listed:
            if name.startswith(("temperature_sensor ", "heater_generic ")) and "chamber" in name.lower():
                objects.append(name)
                self._chamber_object = name
                break
        self._query_objects = objects
        self._light_pin = next(
            (
                n.split(" ", 1)[1]
                for n in listed
                if n.startswith("output_pin ") and any(k in n.lower() for k in ("light", "led"))
            ),
            None,
        )
        self._handshake_done = True

    def _poll_once(self) -> None:
        query = "&".join(quote(name, safe="") for name in self._query_objects)
        result = self._request("GET", f"/printer/objects/query?{query}")
        status = result.get("status") or {}
        self._last_message_time = time.time()
        self._report_messages_since_connect += 1
        self.last_connect_error = None
        was_connected = self.state.connected
        self.state.connected = True
        self._restore_state_after_false_power_off()
        if self._logging_enabled:
            self._message_log.append(
                MQTTLogEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    topic="moonraker/objects/query",
                    direction="in",
                    payload=status,
                )
            )
        self._apply_status(status)
        if not was_connected:
            logger.info("[%s] Connected to Moonraker (%s)", self.ip_address, self.state.firmware_version)

    # ------------------------------------------------------------------ state

    _chamber_object: str | None = None

    def _apply_status(self, st: dict) -> None:
        stats = st.get("print_stats") or {}
        sdcard = st.get("virtual_sdcard") or {}
        display = st.get("display_status") or {}
        webhooks = st.get("webhooks") or {}

        klipper_state = stats.get("state", "standby")
        new_state = STATE_MAP.get(klipper_state, "IDLE")
        if webhooks.get("state") in ("shutdown", "error"):
            # Klipper halted (MCU lost, thermal runaway ...). A running print is over.
            new_state = "FAILED" if self._was_running or self._previous_state in ("RUNNING", "PAUSE") else "IDLE"
            self.state.raw_data["klipper_state_message"] = webhooks.get("state_message")

        filename = stats.get("filename") or None
        if new_state == "IDLE" and klipper_state == "standby":
            filename = None

        self._apply_temperatures(st)
        self._apply_fans_and_speed(st)
        self._apply_progress(new_state, filename, stats, sdcard, display, st)

        self.state.state = new_state
        self.state.gcode_file = filename
        self.state.current_print = _slug_name(filename)
        self.state.subtask_name = _slug_name(filename)
        self.state.raw_data.update(
            {
                "gcode_state": new_state,
                "klipper_print_state": klipper_state,
                "gcode_file": filename,
                "subtask_name": self.state.subtask_name,
                "mc_percent": self.state.progress,
                "mc_remaining_time": self.state.remaining_time,
                "layer_num": self.state.layer_num,
                "total_layer_num": self.state.total_layers,
                "filament_used_mm": stats.get("filament_used"),
                "print_duration": stats.get("print_duration"),
                "total_duration": stats.get("total_duration"),
            }
        )

        self._track_lifecycle(new_state, filename)
        self._emit_state()

    def _apply_temperatures(self, st: dict) -> None:
        temps = self.state.temperatures
        ext = st.get("extruder") or {}
        bed = st.get("heater_bed") or {}
        if "temperature" in ext:
            temps["nozzle"] = round(float(ext["temperature"]), 1)
            temps["nozzle_target"] = round(float(ext.get("target") or 0.0), 1)
            temps["nozzle_heating"] = temps["nozzle_target"] > 0 and temps["nozzle"] < temps["nozzle_target"] - 2
        if "temperature" in bed:
            temps["bed"] = round(float(bed["temperature"]), 1)
            temps["bed_target"] = round(float(bed.get("target") or 0.0), 1)
            temps["bed_heating"] = temps["bed_target"] > 0 and temps["bed"] < temps["bed_target"] - 2
            if self.on_bed_temp_update:
                self.on_bed_temp_update(temps["bed"])
        if self._chamber_object:
            ch = st.get(self._chamber_object) or {}
            if "temperature" in ch:
                temps["chamber"] = round(float(ch["temperature"]), 1)
                temps["chamber_target"] = round(float(ch.get("target") or 0.0), 1)

    def _apply_fans_and_speed(self, st: dict) -> None:
        fan = st.get("fan") or {}
        if "speed" in fan:
            self.state.cooling_fan_speed = int(round(float(fan["speed"]) * 100))
        gm = st.get("gcode_move") or {}
        if "speed_factor" in gm:
            self.state.speed_level = speed_percent_to_level(float(gm["speed_factor"]) * 100)

    def _apply_progress(self, new_state: str, filename, stats: dict, sdcard: dict, display: dict, st: dict) -> None:
        if filename != self._file_meta_for:
            self._file_meta = self._fetch_file_metadata(filename) if filename else {}
            self._file_meta_for = filename

        fraction = display.get("progress")
        if fraction is None:
            fraction = sdcard.get("progress")
        fraction = max(0.0, min(1.0, float(fraction or 0.0)))

        if new_state == "FINISH":
            progress = 100.0
        elif new_state == "IDLE":
            progress = 0.0
        else:
            progress = round(fraction * 100, 1)
        self.state.progress = progress
        if new_state in ("RUNNING", "PAUSE") and progress > 0:
            self._last_valid_progress = progress

        # Time left: trust the slicer's estimate early on, the observed pace later.
        remaining_s = 0.0
        if new_state in ("RUNNING", "PAUSE"):
            elapsed = float(stats.get("print_duration") or 0.0)
            estimated = float(self._file_meta.get("estimated_time") or 0.0)
            if fraction >= 0.05 and elapsed > 60:
                remaining_s = elapsed * (1.0 - fraction) / fraction
            elif estimated > 0:
                remaining_s = estimated * (1.0 - fraction)
        self.state.remaining_time = int(math.ceil(remaining_s / 60.0)) if remaining_s > 0 else 0

        info = stats.get("info") or {}
        total = info.get("total_layer") or self._file_meta.get("layer_count") or 0
        if new_state == "IDLE":
            total = 0  # nothing loaded: don't keep showing the last job's layer count
        current = info.get("current_layer") or 0
        if not current and new_state in ("RUNNING", "PAUSE") and total:
            current = self._layer_from_z(st)
        self.state.total_layers = int(total or 0)
        self.state.layer_num = int(current or 0) if new_state in ("RUNNING", "PAUSE") else 0
        if self.state.layer_num > 0:
            self._last_valid_layer = self.state.layer_num

        if self.on_layer_change and self.state.layer_num > 0 and self.state.layer_num != self._last_layer:
            self.on_layer_change(self.state.layer_num)
        self._last_layer = self.state.layer_num
        progress_int = int(progress)
        if self.on_print_progress and new_state == "RUNNING" and progress_int > self._last_progress_int >= 0:
            self.on_print_progress(progress_int)
        self._last_progress_int = progress_int if new_state in ("RUNNING", "PAUSE") else -1

    def _layer_from_z(self, st: dict) -> int:
        """Estimate the layer from the toolhead height when the slicer sends no layer info."""
        height = self._file_meta.get("layer_height")
        first = self._file_meta.get("first_layer_height") or height
        position = (st.get("gcode_move") or {}).get("gcode_position") or (st.get("toolhead") or {}).get("position")
        if not height or not position or len(position) < 3:
            return 0
        layer = int(round((float(position[2]) - float(first)) / float(height))) + 1
        return max(0, min(layer, int(self._file_meta.get("layer_count") or layer)))

    def _fetch_file_metadata(self, filename: str) -> dict:
        try:
            return self._request("GET", f"/server/files/metadata?filename={quote(filename, safe='/')}")
        except _ConnectError as exc:
            logger.debug("[%s] No metadata for %s: %s", self.ip_address, filename, exc)
            return {}

    # ------------------------------------------------------------------ print lifecycle

    def _track_lifecycle(self, new_state: str, filename: str | None) -> None:
        prev = self._previous_state
        first_report = not self._first_report_seen
        self._first_report_seen = True

        is_new_print = new_state == "RUNNING" and prev not in ("RUNNING", "PAUSE") and not first_report
        is_file_change = (
            new_state in ("RUNNING", "PAUSE")
            and filename
            and self._previous_filename
            and filename != self._previous_filename
        )
        running_first_observed = first_report and new_state in ("RUNNING", "PAUSE")

        if is_new_print or is_file_change:
            self._completion_triggered = False
            self._was_running = True
            self._last_valid_progress = 0.0
            self._last_valid_layer = 0
            self._last_layer = 0
            logger.info("[%s] PRINT START detected - file: %s", self.ip_address, filename)
            if self.on_print_start:
                self.on_print_start(self._lifecycle_payload(filename))
        elif running_first_observed:
            self._was_running = True
            self._completion_triggered = False
            logger.info(
                "[%s] RUNNING observed without PRINT START (restart-recovery) - file: %s", self.ip_address, filename
            )
            if self.on_print_running_observed:
                self.on_print_running_observed(self._lifecycle_payload(filename))

        ended = (
            self._was_running
            and not self._completion_triggered
            and new_state in ("FINISH", "FAILED", "IDLE")
            and prev != new_state
        )
        if ended:
            status = {"FINISH": "completed", "FAILED": "failed"}.get(new_state, "aborted")
            name = self._previous_filename or filename
            logger.info("[%s] PRINT COMPLETE detected - status: %s, file: %s", self.ip_address, status, name)
            self._completion_triggered = True
            self._was_running = False
            if status == "completed" and self.on_finish_photo_moment:
                self.on_finish_photo_moment(
                    {
                        "trigger": "finish_state",
                        "filename": name,
                        "subtask_name": _slug_name(name),
                        "timelapse_was_active": False,
                    }
                )
            if self.on_print_complete:
                self.on_print_complete(
                    {
                        "status": status,
                        "filename": name,
                        "subtask_name": _slug_name(name),
                        "raw_data": dict(self.state.raw_data),
                        "timelapse_was_active": False,
                        "hms_errors": [],
                        "ams_mapping": None,
                        "last_progress": self._last_valid_progress,
                        "last_layer_num": self._last_valid_layer,
                    }
                )

        self._previous_state = new_state
        if filename:
            self._previous_filename = filename

    def _lifecycle_payload(self, filename: str | None) -> dict:
        return {
            "filename": filename,
            "subtask_name": _slug_name(filename),
            "remaining_time": self.state.remaining_time * 60 if self.state.remaining_time > 0 else None,
            "raw_data": dict(self.state.raw_data),
            "ams_mapping": None,
        }

    def _emit_state(self) -> None:
        if self.on_state_change:
            self.on_state_change(self.state)

    # ------------------------------------------------------------------ staleness / power

    def check_staleness(self) -> bool:
        """Mark the printer disconnected when polling has silently stopped."""
        if self.state.connected and self._last_message_time:
            if time.time() - self._last_message_time > self.STALE_TIMEOUT:
                logger.warning(
                    "[%s] Moonraker stale - no successful poll for %.0fs", self.ip_address, self.STALE_TIMEOUT
                )
                self.state.connected = False
                self._emit_state()
        return self.state.connected

    def is_stale(self) -> bool:
        return bool(self._last_message_time) and time.time() - self._last_message_time > self.STALE_TIMEOUT

    def force_reconnect_stale_session(self, reason: str) -> None:
        logger.info("[%s] Forcing Moonraker re-poll: %s", self.ip_address, reason)
        self._handshake_done = False
        self._wake.set()

    @property
    def report_messages_since_connect(self) -> int:
        return self._report_messages_since_connect

    def mark_power_off(self) -> bool:
        """Presume the printer lost power (smart plug off); undone by the next good poll."""
        if not self.state.connected:
            return False
        previous = self.state.state
        self.state.connected = False
        self.state.state = "unknown"
        if self._state_before_power_off is None and previous not in ("", "unknown"):
            self._state_before_power_off = previous
        return True

    def _restore_state_after_false_power_off(self) -> None:
        # A successful poll proves the printer is powered; _apply_status writes the
        # real state right after this, so only the bookkeeping needs clearing.
        self._state_before_power_off = None

    def request_status_update(self) -> bool:
        if not self.state.connected:
            return False
        self._wake.set()
        return True

    # ------------------------------------------------------------------ commands

    def _post(self, path: str, **kwargs) -> bool:
        if not self.state.connected:
            logger.warning("[%s] Cannot send %s: not connected", self.ip_address, path)
            return False
        try:
            self._request("POST", path, **kwargs)
            return True
        except _ConnectError as exc:
            logger.warning("[%s] Moonraker command %s failed: %s", self.ip_address, path, exc)
            return False

    def send_gcode(self, gcode: str) -> bool:
        if self._logging_enabled:
            self._message_log.append(
                MQTTLogEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    topic="moonraker/gcode/script",
                    direction="out",
                    payload={"script": gcode},
                )
            )
        return self._post("/printer/gcode/script", json={"script": gcode})

    def pause_print(self) -> bool:
        return self._post("/printer/print/pause")

    def resume_print(self) -> bool:
        return self._post("/printer/print/resume")

    def stop_print(self) -> bool:
        return self._post("/printer/print/cancel")

    def start_print(self, filename: str, plate_id: int = 1, **_bambu_options: object) -> bool:  # noqa: ARG002
        """Start a G-code file that is already on the printer (root ``gcodes``)."""
        return self._post(f"/printer/print/start?filename={quote(filename, safe='/')}")

    def upload_and_print(self, local_path: str, remote_name: str | None = None, start: bool = True) -> bool:
        """Upload a sliced ``.gcode`` to the printer and optionally start it."""
        if not self.state.connected:
            return False
        name = remote_name or os.path.basename(local_path)
        try:
            with open(local_path, "rb") as fh:
                self._request(
                    "POST",
                    "/server/files/upload",
                    data={"root": "gcodes", "print": "true" if start else "false"},
                    files={"file": (name, fh, "application/octet-stream")},
                    timeout=300.0,
                )
            return True
        except (_ConnectError, OSError) as exc:
            logger.warning("[%s] Upload of %s failed: %s", self.ip_address, name, exc)
            return False

    def list_gcode_files(self) -> list[dict]:
        """Files in the printer's ``gcodes`` root: ``[{path, modified, size}]``."""
        try:
            result = self._request("GET", "/server/files/list?root=gcodes")
        except _ConnectError:
            return []
        return result if isinstance(result, list) else []

    def set_bed_temperature(self, target: int) -> bool:
        return self.send_gcode(f"M140 S{int(max(0, target))}")

    def set_nozzle_temperature(self, target: int, nozzle: int = 0) -> bool:  # noqa: ARG002
        return self.send_gcode(f"M104 S{int(max(0, target))}")

    def set_chamber_temperature(self, target: int) -> bool:  # noqa: ARG002
        return False  # Ender-3 V3 has no chamber heater

    def set_print_speed(self, mode: int) -> bool:
        percent = SPEED_LEVEL_PERCENT.get(mode)
        return self.send_gcode(f"M220 S{percent}") if percent else False

    def set_fan_speed(self, fan: int, speed: int) -> bool:
        if fan != 1:
            return False  # only the part-cooling fan is addressable on stock Klipper
        return self.send_gcode(f"M106 S{max(0, min(255, int(speed)))}")

    def set_part_fan(self, speed: int) -> bool:
        return self.set_fan_speed(1, speed)

    def set_airduct_mode(self, mode: str) -> bool:  # noqa: ARG002
        return False

    def set_chamber_light(self, on: bool) -> bool:
        if not self._light_pin:
            return False
        ok = self.send_gcode(f"SET_PIN PIN={self._light_pin} VALUE={1 if on else 0}")
        if ok:
            self.state.chamber_light = bool(on)
        return ok

    def home_axes(self, axes: str = "XYZ") -> bool:  # noqa: ARG002
        return self.send_gcode("G28")

    def move_axis(self, axis: str, distance: float, speed: int = 3000) -> bool:
        axis = axis.upper()
        if axis not in ("X", "Y", "Z"):
            return False
        return self.send_gcode(f"G91\nG1 {axis}{distance:.2f} F{int(speed)}\nG90")

    def disable_motors(self) -> bool:
        return self.send_gcode("M84")

    def enable_motors(self) -> bool:
        return False  # Klipper has no portable "enable steppers" G-code

    def clear_hms_errors(self) -> bool:
        return False

    def skip_objects(self, object_ids: list[int]) -> bool:  # noqa: ARG002
        return False

    # ------------------------------------------------------------------ camera

    def get_webcam_urls(self) -> dict | None:
        """First webcam Moonraker knows about, as absolute URLs, or None."""
        try:
            result = self._request("GET", "/server/webcams/list")
        except _ConnectError:
            return None
        for cam in result.get("webcams", []) if isinstance(result, dict) else []:
            if cam.get("enabled") is False:
                continue
            return {
                "stream_url": self._absolute(cam.get("stream_url")),
                "snapshot_url": self._absolute(cam.get("snapshot_url")),
            }
        return None

    def _absolute(self, url: str | None) -> str | None:
        if not url:
            return None
        if url.startswith(("http://", "https://", "rtsp://")):
            return url
        # Creality serves relative paths such as "/webcam/?action=stream" on the
        # web-UI port (80), not on the Moonraker API port.
        return f"http://{self.ip_address}{url if url.startswith('/') else '/' + url}"

    # ------------------------------------------------------------------ debug log

    def enable_logging(self, enabled: bool = True) -> None:
        self._logging_enabled = enabled

    def get_logs(self) -> list[MQTTLogEntry]:
        return list(self._message_log)

    def clear_logs(self) -> None:
        self._message_log.clear()

    @property
    def logging_enabled(self) -> bool:
        return self._logging_enabled


class _ConnectError(Exception):
    """A failed Moonraker exchange, carrying the ``last_connect_error`` slug."""

    def __init__(self, slug: str, detail: str):
        super().__init__(detail)
        self.slug = slug
