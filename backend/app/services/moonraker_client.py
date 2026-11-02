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
import posixpath
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from backend.app.services.bambu_mqtt import MQTTLogEntry, PrinterState

logger = logging.getLogger(__name__)

PRINTER_TYPE_CREALITY = "creality"
DEFAULT_MOONRAKER_PORT = 7125
GO2RTC_PORT = 1984

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


@dataclass(frozen=True)
class FanRef:
    """A fan Klipper exposes, and how to read/drive it."""

    kind: str  # "fan", "fan_generic", "output_pin", "heater_fan", "controller_fan", "temperature_fan"
    obj: str  # full Klipper object name, e.g. "output_pin fan0"
    name: str  # short name ("" for the plain [fan] section)


_FAN_KINDS = ("fan_generic", "output_pin", "heater_fan", "controller_fan", "temperature_fan")


def discover_fans(listed: list[str]) -> dict[str, FanRef]:
    """Map Bambuddy's fan roles (part / aux / chamber / heatbreak) onto Klipper objects.

    Printers name their fans differently: stock Klipper has one ``[fan]``, while
    Creality firmware usually uses ``fan0`` (model), ``fan1`` (side/aux) and
    ``fan2`` (case), as ``output_pin`` or ``fan_generic`` objects. Rather than
    assume one layout, look at what the printer says it has. A role the printer
    lacks is simply absent, and the card hides that badge.
    """
    candidates: list[FanRef] = []
    for obj in listed:
        kind, _, name = obj.partition(" ")
        if obj == "fan":
            candidates.append(FanRef("fan", obj, ""))
        elif kind in _FAN_KINDS:
            if kind == "output_pin" and "fan" not in name.lower():
                continue  # an output_pin that is not a fan (LED, beeper ...)
            candidates.append(FanRef(kind, obj, name))

    used: set[str] = set()

    def pick(pred) -> FanRef | None:
        for ref in candidates:
            if ref.obj not in used and pred(ref):
                used.add(ref.obj)
                return ref
        return None

    def named(*words: str):
        return lambda r: any(w in r.name.lower() for w in words)

    manual = ("fan_generic", "output_pin")
    roles: dict[str, FanRef] = {}
    part = (
        pick(lambda r: r.kind == "fan")
        or pick(lambda r: r.kind in manual and r.name.lower() == "fan0")
        or pick(lambda r: r.kind in manual and named("part", "model", "cooling")(r))
    )
    aux = pick(lambda r: r.kind in manual and (r.name.lower() == "fan1" or named("aux", "side")(r)))
    chamber = pick(
        lambda r: r.kind in manual and (r.name.lower() == "fan2" or named("case", "chamber", "exhaust", "cabinet")(r))
    ) or pick(lambda r: (r.kind == "temperature_fan" and named("chamber")(r)) or r.kind == "controller_fan")
    heatbreak = pick(lambda r: r.kind == "heater_fan")
    for role, ref in (("part", part), ("aux", aux), ("chamber", chamber), ("heatbreak", heatbreak)):
        if ref:
            roles[role] = ref
    return roles


def fan_percent(ref: FanRef, status: dict) -> int | None:
    """0-100 for a polled fan object, or None when the printer did not report it."""
    key = "value" if ref.kind == "output_pin" else "speed"
    if key not in status or status[key] is None:
        return None
    value = float(status[key])
    if value > 1.0:  # an output_pin with a 0-255 scale
        value /= 255.0
    return max(0, min(100, int(round(value * 100))))


# Creality filament codes ("1XXXXX") seen on the K2's CFS. The printer carries the full table in
# its own material_database.json; these are the ones documented so far. An unknown code is shown
# by its number rather than guessed at.
CFS_MATERIALS = {
    "100001": ("PLA", "Generic PLA"),
    "101001": ("PLA", "Hyper PLA"),
    "102001": ("PLA-CF", "Hyper PLA-CF"),
    "100005": ("TPU", "Generic TPU"),
}
_CFS_NONE = {"", "none", "unknown", "-1", "null"}
_CFS_SLOT_LETTERS = "ABCD"


def _cfs_color(value: object) -> str | None:
    """``"0RRGGBB"`` (stock) or ``"#RRGGBB"`` (flat) -> ``"RRGGBBFF"``; None for empty / unknown."""
    text = str(value or "").strip().lstrip("#")
    if text.lower() in _CFS_NONE:
        return None
    if len(text) == 7 and text[0] == "0":
        text = text[1:]
    if len(text) == 6 and all(c in "0123456789abcdefABCDEF" for c in text):
        return text.upper() + "FF"
    return None


def _cfs_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _cfs_rfid_length_m(vender: object) -> int | None:
    """Full spool length in metres from a 40-character Creality RFID record, when plausible."""
    text = str(vender or "")
    if len(text) != 40:
        return None
    digits = text[24:28]
    if digits.isdigit() and 50 <= int(digits) <= 2000:
        return int(digits)
    return None


def parse_cfs_stock(box: dict) -> list[dict]:
    """AMS-shaped units from the stock ``box`` object (``T1``..``T4``, arrays per slot A-D)."""
    units: list[dict] = []
    for number in range(1, 5):
        unit = box.get(f"T{number}")
        if not isinstance(unit, dict) or str(unit.get("state", "")).lower() != "connect":
            continue

        def column(name: str, unit=unit) -> list:
            value = unit.get(name)
            return list(value) if isinstance(value, list) else []

        venders, colors = column("vender"), column("color_value")
        materials, remains = column("material_type"), column("remain_len")
        trays = []
        for i, letter in enumerate(_CFS_SLOT_LETTERS):
            vender = venders[i] if i < len(venders) else "none"
            present = str(vender).strip().lower() != "none"
            code = str(materials[i]).strip() if i < len(materials) else ""
            known = CFS_MATERIALS.get(code)
            meters = _cfs_float(remains[i]) if i < len(remains) else None
            total = _cfs_rfid_length_m(vender)
            remain = -1
            if present and meters is not None and meters >= 0 and total:
                remain = max(0, min(100, round(meters / total * 100)))
            trays.append(
                {
                    "id": i,
                    "tray_color": _cfs_color(colors[i]) if present and i < len(colors) else None,
                    "tray_type": known[0] if present and known else None,
                    "tray_sub_brands": (
                        known[1] if known else (f"Material {code}" if code.lower() not in _CFS_NONE else None)
                    )
                    if present
                    else None,
                    "remain": remain,
                    "remain_m": meters if present and meters is not None and meters >= 0 else None,
                    "state": 10 if present else 9,
                    "exists": present,
                    "cfs_slot": f"T{number}{letter}",
                }
            )
        humidity = _cfs_float(unit.get("dry_and_humidity"))
        temperature = _cfs_float(unit.get("temperature"))
        units.append(
            {
                "id": number - 1,
                "humidity_raw": humidity,
                "temp": temperature,
                "sn": str(unit.get("sn") or ""),
                "sw_ver": str(unit.get("version") or ""),
                "module_type": "cfs",
                "tray": trays,
            }
        )
    return units


def parse_cfs_flat(box: dict) -> tuple[list[dict], int]:
    """AMS-shaped units and the loaded slot (-1 if none) from the flat ``box`` variant (``slots[]``)."""
    slots = [x for x in box.get("slots", []) if isinstance(x, dict) and not x.get("external")]
    loaded = box.get("loaded_slot")
    loaded = loaded if isinstance(loaded, int) else -1
    by_unit: dict[int, list[dict]] = {}
    for slot in slots:
        index = slot.get("index")
        if not isinstance(index, int) or index < 0:
            continue
        present = bool(slot.get("present"))
        material = str(slot.get("material") or "").strip()
        percent = slot.get("rfid_percent")
        by_unit.setdefault(index // 4, []).append(
            {
                "id": index % 4,
                "tray_color": _cfs_color(slot.get("color")) if present else None,
                "tray_type": material or None if present else None,
                "tray_sub_brands": (str(slot.get("name") or material) or None) if present else None,
                "remain": max(0, min(100, round(percent))) if isinstance(percent, (int, float)) else -1,
                "state": 11 if index == loaded else (10 if present else 9),
                "exists": present,
            }
        )
    units = []
    for unit_id in sorted(by_unit):
        trays = sorted(by_unit[unit_id], key=lambda t: t["id"])
        units.append(
            {
                "id": unit_id,
                "humidity_raw": _cfs_float(box.get("humidity_pct")),
                "temp": _cfs_float(box.get("temp_c")),
                "module_type": "cfs",
                "tray": trays,
            }
        )
    return units, loaded


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
        self.last_error: str | None = None  # why the last upload_and_print failed
        self.last_dispatch_subtask_id: str | None = None  # parity with BambuMQTTClient
        self._drying_targets: dict[int, dict] = {}  # parity: PrinterManager reads it
        self._last_message_time: float = 0.0
        self._report_messages_since_connect: int = 0

        self._base_url = f"http://{ip_address}:{self.port}"
        self._http: httpx.Client | None = None
        self._probe_http: httpx.Client | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._consecutive_failures = 0
        self._handshake_done = False
        self._query_objects: list[str] = list(CORE_OBJECTS)
        self._light_pin: str | None = None
        self._fans: dict[str, FanRef] = {}
        self._has_box = False
        self._has_exclude = False
        self._object_names: dict[int, str] = {}
        self._filament_sensors: list[str] = []
        self._unknown_cfs_logged: set[str] = set()
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
        probe, self._probe_http = self._probe_http, None
        for client in (http, probe):
            if client is not None:
                try:
                    client.close()
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
        # Chamber: a heater_generic (K2 series: chamber_heater, has a target and can be
        # driven) is preferred over a bare temperature_sensor (K1: read-only).
        chamber_names = [n for n in listed if "chamber" in n.lower()]
        heater = next((n for n in chamber_names if n.startswith("heater_generic ")), None)
        sensor = next((n for n in chamber_names if n.startswith("temperature_sensor ")), None)
        self._chamber_object = heater or sensor
        self._chamber_heater = heater.split(" ", 1)[1] if heater else None
        if self._chamber_object:
            objects.append(self._chamber_object)
        # Filament runout / motion sensors: a pause with no filament in front of one is a runout.
        self._filament_sensors = [
            n for n in listed if n.startswith(("filament_switch_sensor ", "filament_motion_sensor "))
        ]
        for name in self._filament_sensors:
            if name not in objects:
                objects.append(name)
        # Object cancellation: Klipper's exclude_object lists the printed objects and can drop one.
        self._has_exclude = "exclude_object" in listed
        if self._has_exclude:
            objects.append("exclude_object")
        # CFS (K2 series): the multi-filament module is the Klipper `box` object.
        self._has_box = "box" in listed
        if self._has_box:
            objects.append("box")
        # Fans: query exactly the ones this printer has.
        self._fans = discover_fans(listed)
        for ref in self._fans.values():
            if ref.obj not in objects:
                objects.append(ref.obj)
        self.state.raw_data["fans"] = {role: ref.obj for role, ref in self._fans.items()}
        logger.info(
            "[%s] Moonraker fans: %s",
            self.ip_address,
            ", ".join(f"{role}={ref.obj}" for role, ref in self._fans.items()) or "none found",
        )
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
    _chamber_heater: str | None = None  # heater_generic name when the chamber can be heated

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
        self._apply_cfs(st.get("box"))
        self._apply_objects(st.get("exclude_object"), new_state)
        self._apply_progress(new_state, filename, stats, sdcard, display, st)

        self.state.printer_error = self._printer_error(st, new_state, webhooks, stats)
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

    def _printer_error(self, st: dict, new_state: str, webhooks: dict, stats: dict) -> str | None:
        """The fault the printer is reporting, in its own words, or None when healthy.

        In order of severity: a halted Klipper (MCU lost, thermal runaway ...), the message a failed
        job carries, and a paused job with a filament sensor that sees no filament (a runout).
        """
        if webhooks.get("state") in ("shutdown", "error"):
            text = str(webhooks.get("state_message") or "Klipper has shut down").strip()
            return text.splitlines()[0][:300] if text else "Klipper has shut down"
        if new_state == "FAILED":
            message = str(stats.get("message") or "").strip()
            if message:
                return message.splitlines()[0][:300]
        if new_state == "PAUSE":
            for name in self._filament_sensors:
                sensor = st.get(name) or {}
                if sensor.get("enabled", True) and sensor.get("filament_detected") is False:
                    return f"Filament runout ({name.split(' ', 1)[1]})"
        return None

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
                temps["chamber_heating"] = (
                    temps["chamber_target"] > 0 and temps["chamber"] < temps["chamber_target"] - 2
                )

    def _apply_objects(self, exclude: object, new_state: str) -> None:
        """Publish the objects of the running print so single ones can be cancelled (EXCLUDE_OBJECT)."""
        if not self._has_exclude or new_state not in ("RUNNING", "PAUSE") or not isinstance(exclude, dict):
            self.state.printable_objects = {}
            self.state.skipped_objects = []
            self._object_names = {}
            return
        listed = [o for o in exclude.get("objects") or [] if isinstance(o, dict) and o.get("name")]
        excluded = {str(name) for name in exclude.get("excluded_objects") or []}
        printable: dict[int, dict] = {}
        names: dict[int, str] = {}
        skipped: list[int] = []
        for index, obj in enumerate(listed, start=1):
            raw = str(obj["name"])
            names[index] = raw
            center = obj.get("center") if isinstance(obj.get("center"), (list, tuple)) else None
            printable[index] = {
                # slicers append "_id_<n>" to a name: it is noise on the card
                "name": re.sub(r"_id_\d+$", "", raw) or raw,
                "x": float(center[0]) if center and len(center) >= 2 else None,
                "y": float(center[1]) if center and len(center) >= 2 else None,
            }
            if raw in excluded:
                skipped.append(index)
        self.state.printable_objects = printable
        self.state.skipped_objects = skipped
        self._object_names = names

    def _apply_cfs(self, box: object) -> None:
        """Present the CFS as AMS units (colour, material, remaining, humidity) in ``raw_data``."""
        if not self._has_box or not isinstance(box, dict):
            self.state.raw_data.pop("ams", None)
            return
        if "slots" in box:
            units, loaded = parse_cfs_flat(box)
            self.state.tray_now = loaded if loaded >= 0 else 255
        else:
            units, loaded = parse_cfs_stock(box), -1
        if units:
            self.state.raw_data["ams"] = units
        else:
            self.state.raw_data.pop("ams", None)
        for unit in units:
            for tray in unit["tray"]:
                name = tray.get("tray_sub_brands") or ""
                if name.startswith("Material ") and name not in self._unknown_cfs_logged:
                    self._unknown_cfs_logged.add(name)
                    logger.info(
                        "[%s] CFS slot %s holds an unrecognised filament code (%s)",
                        self.ip_address,
                        tray.get("cfs_slot"),
                        name,
                    )

    def _apply_fans_and_speed(self, st: dict) -> None:
        for role, attr in (
            ("part", "cooling_fan_speed"),
            ("aux", "big_fan1_speed"),
            ("chamber", "big_fan2_speed"),
            ("heatbreak", "heatbreak_fan_speed"),
        ):
            ref = self._fans.get(role)
            if ref is not None:
                percent = fan_percent(ref, st.get(ref.obj) or {})
                if percent is not None:
                    setattr(self.state, attr, percent)
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
                        "printer_error": self.state.printer_error,
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
        """Upload a sliced ``.gcode`` to the printer's ``gcodes`` root and optionally start it.

        Moonraker answers an upload with ``{"item": {...}, "print_started": bool, ...}``
        (not the ``{"result": ...}`` envelope its other endpoints use). On failure
        ``last_error`` says why, in words fit for a queue item's error message.
        """
        self.last_error = None
        if not self.state.connected or self._http is None:
            self.last_error = "Printer not connected"
            return False
        name = remote_name or os.path.basename(local_path)
        try:
            with open(local_path, "rb") as fh:
                response = self._http.post(
                    f"{self._base_url}/server/files/upload",
                    data={"root": "gcodes", "print": "true" if start else "false"},
                    files={"file": (name, fh, "application/octet-stream")},
                    timeout=300.0,
                )
        except OSError as exc:
            self.last_error = f"Could not read the file to send: {exc}"
        except httpx.HTTPError as exc:
            self.last_error = f"Could not reach the printer to send the file: {exc or type(exc).__name__}"
        else:
            if response.status_code not in (200, 201):
                detail = ""
                try:
                    detail = (response.json().get("error") or {}).get("message", "")
                except (ValueError, AttributeError):
                    pass
                self.last_error = (
                    f"The printer refused the file (HTTP {response.status_code}{': ' + detail if detail else ''})"
                )
            else:
                try:
                    started = bool(response.json().get("print_started"))
                except (ValueError, AttributeError):
                    started = False
                if start and not started:
                    self.last_error = (
                        "The file reached the printer but it did not start printing (is it busy or not ready?)"
                    )
                else:
                    return True
        logger.warning("[%s] Upload of %s failed: %s", self.ip_address, name, self.last_error)
        return False

    def get_file_metadata(self, filename: str) -> dict:
        """What Moonraker parsed from a file's slicer comments (time, filament, layers, thumbnails ...)."""
        return self._fetch_file_metadata(filename) if self._http is not None else {}

    def download_gcode(self, filename: str, dest: os.PathLike, max_bytes: int = 300 * 1024 * 1024) -> int | None:
        """Stream a file from the printer's ``gcodes`` root to ``dest``.

        Returns the bytes written, or None when the file is missing, the printer is unreachable or it is
        larger than ``max_bytes`` (the partial file is removed).
        """
        http = self._http
        if http is None:
            return None
        url = f"{self._base_url}/server/files/gcodes/{quote(filename, safe='/')}"
        written = 0
        try:
            with http.stream("GET", url, timeout=120.0) as response:
                if response.status_code != 200:
                    return None
                declared = int(response.headers.get("content-length") or 0)
                if declared > max_bytes:
                    return None
                with open(dest, "wb") as out:
                    for chunk in response.iter_bytes(65536):
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError("file larger than the allowed size")
                        out.write(chunk)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.info("[%s] Could not download %s: %s", self.ip_address, filename, exc)
            try:
                os.unlink(dest)
            except OSError:
                pass
            return None
        return written

    def download_thumbnail(self, filename: str, relative_path: str, max_bytes: int = 5 * 1024 * 1024) -> bytes | None:
        """A thumbnail Moonraker lists in a file's metadata (its path is relative to the file's folder)."""
        http = self._http
        if http is None:
            return None
        directory = posixpath.dirname(filename)
        path = posixpath.join(directory, relative_path) if directory else relative_path
        try:
            response = http.get(f"{self._base_url}/server/files/gcodes/{quote(path, safe='/')}", timeout=15.0)
        except httpx.HTTPError:
            return None
        if response.status_code != 200 or len(response.content) > max_bytes:
            return None
        return response.content

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

    def set_chamber_temperature(self, target: int) -> bool:
        """Heat the chamber (K2 series). Printers without a chamber heater return False."""
        if not self._chamber_heater:
            return False
        return self.send_gcode(f"SET_HEATER_TEMPERATURE HEATER={self._chamber_heater} TARGET={int(max(0, target))}")

    def set_print_speed(self, mode: int) -> bool:
        percent = SPEED_LEVEL_PERCENT.get(mode)
        return self.send_gcode(f"M220 S{percent}") if percent else False

    def set_fan_speed(self, fan: int, speed: int) -> bool:
        """Fan index as in Bambu's API: 1 part cooling, 2 auxiliary, 3 chamber/case. Speed 0-255."""
        ref = self._fans.get({1: "part", 2: "aux", 3: "chamber"}.get(fan, ""))
        if ref is None:
            return False  # this printer has no such fan
        speed = max(0, min(255, int(speed)))
        fraction = round(speed / 255, 3)
        if ref.kind == "fan":
            return self.send_gcode(f"M106 S{speed}")
        if ref.kind == "fan_generic":
            return self.send_gcode(f"SET_FAN_SPEED FAN={ref.name} SPEED={fraction}")
        if ref.kind == "output_pin":
            return self.send_gcode(f"SET_PIN PIN={ref.name} VALUE={fraction}")
        return False  # heater_fan / controller_fan switch themselves

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

    # The Bambu AMS / calibration / HMS control surface. The CFS is displayed read-only (loading and
    # unloading it means Creality's BOX_* / M8200 commands, which are not driven from here), so these
    # report "not supported" and the route says so, instead of failing on a missing attribute.
    def _unsupported(self, *_args: object, **_kwargs: object) -> bool:
        return False

    ams_load_filament = ams_unload_filament = ams_control = ams_set_filament_setting = _unsupported
    reset_ams_slot = extrusion_cali_sel = extrusion_cali_set = start_calibration = _unsupported
    set_xcam_option = set_ams_filament_backup = select_extruder = execute_hms_action = _unsupported
    send_drying_command = _unsupported

    def register_assignment_verification(self, *_args: object, **_kwargs: object) -> None:
        return None

    def skip_objects(self, object_ids: list[int]) -> bool:
        """Cancel objects of the running print with Klipper's EXCLUDE_OBJECT (it cannot be undone)."""
        names = [self._object_names.get(oid) for oid in object_ids]
        if not names or any(name is None for name in names):
            return False
        ok = True
        for name in names:
            quoted = f'"{name}"' if re.search(r"\s", name) else name
            ok = self.send_gcode(f"EXCLUDE_OBJECT NAME={quoted}") and ok
        return ok

    # ------------------------------------------------------------------ camera

    def _advertised_webcams(self) -> list[tuple[str | None, str | None]]:
        """(stream, snapshot) pairs from Moonraker's webcam list, URLs possibly relative."""
        try:
            result = self._request("GET", "/server/webcams/list")
        except _ConnectError:
            return []
        cams = result.get("webcams", []) if isinstance(result, dict) else []
        return [(c.get("stream_url"), c.get("snapshot_url")) for c in cams if c.get("enabled") is not False]

    def _camera_candidates(self, current: tuple[str | None, str | None] | None) -> list[tuple[str, str | None]]:
        """Stream/snapshot URL pairs to try, best first, without duplicates."""
        host = self.ip_address
        pairs: list[tuple[str | None, str | None]] = []
        if current and current[0]:
            pairs.append(current)
        for stream, snapshot in self._advertised_webcams():
            for stream_path, snapshot_path in ((stream, snapshot),):
                for base in self._camera_bases(stream_path):
                    pairs.append((base(stream_path), base(snapshot_path) if snapshot_path else None))
        # A go2rtc bridge (the Creality Helper Script installs one on K2s, whose own camera is WebRTC
        # and cannot be shown directly) serves any stream it knows as MJPEG and as single frames.
        for name in self._go2rtc_streams():
            quoted = quote(name, safe="")
            pairs.append(
                (
                    f"http://{host}:{GO2RTC_PORT}/api/stream.mjpeg?src={quoted}",
                    f"http://{host}:{GO2RTC_PORT}/api/frame.jpeg?src={quoted}",
                )
            )
        # Common places a Creality / Klipper printer serves its camera
        # (mjpg-streamer on 8080, or nginx in front of Fluidd / Mainsail).
        for prefix in (":8080/", "/webcam/", ":4408/webcam/", ":4408/webcam2/", ":8080/webcam/"):
            pairs.append((f"http://{host}{prefix}?action=stream", f"http://{host}{prefix}?action=snapshot"))
        seen: set[str] = set()
        out: list[tuple[str, str | None]] = []
        for stream, snapshot in pairs:
            if stream and stream not in seen:
                seen.add(stream)
                out.append((stream, snapshot))
        return out

    def _go2rtc_streams(self) -> list[str]:
        """Names of the streams a go2rtc bridge on this printer exposes (empty when there is none)."""
        http = self._probe_http
        if http is None:
            http = self._probe_http = httpx.Client(timeout=3.0, follow_redirects=True)
        try:
            response = http.get(f"http://{self.ip_address}:{GO2RTC_PORT}/api/streams")
            data = response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return []
        return [str(name) for name in data] if isinstance(data, dict) else []

    def _camera_bases(self, url: str | None):
        """Absolute URL(s) for an advertised path: as given, or on each web-UI port."""
        host = self.ip_address
        if not url or url.startswith(("http://", "https://", "rtsp://")):
            return [lambda u: u]
        return [
            (lambda u, port=port: f"http://{host}{port}{u if u.startswith('/') else '/' + u}")
            for port in ("", ":4408", ":8080")
        ]

    def _responds_with_image(self, url: str, timeout: float = 3.0) -> bool:
        """True if *url* answers 200 with an image or an MJPEG stream (headers only)."""
        http = self._probe_http
        if http is None:
            http = self._probe_http = httpx.Client(timeout=timeout, follow_redirects=True)
        try:
            with http.stream("GET", url) as response:
                ctype = response.headers.get("content-type", "").lower()
                return response.status_code == 200 and (
                    ctype.startswith("image/") or "multipart/x-mixed-replace" in ctype
                )
        except httpx.HTTPError:
            return False

    def find_camera(self, current_stream: str | None = None, current_snapshot: str | None = None) -> dict:
        """Find a working camera URL.

        Tries the one already configured, then what Moonraker advertises, then the
        usual Creality locations, and returns the first that really serves an image:
        ``{"stream_url", "snapshot_url", "verified": True, "tried": [...]}``. When
        nothing answers, returns the advertised pair unverified (``verified``
        False), or ``None`` URLs if Moonraker advertises none.
        """
        tried: list[str] = []
        candidates = self._camera_candidates((current_stream, current_snapshot) if current_stream else None)
        for stream, snapshot in candidates:
            probe = snapshot or stream
            tried.append(probe)
            if self._responds_with_image(probe):
                return {"stream_url": stream, "snapshot_url": snapshot, "verified": True, "tried": tried}
        advertised = self._advertised_webcams()
        if advertised:
            stream, snapshot = advertised[0]
            return {
                "stream_url": next(iter(b(stream) for b in self._camera_bases(stream)), None),
                "snapshot_url": next(iter(b(snapshot) for b in self._camera_bases(snapshot)), None)
                if snapshot
                else None,
                "verified": False,
                "tried": tried,
            }
        return {"stream_url": None, "snapshot_url": None, "verified": False, "tried": tried}

    def get_webcam_urls(self) -> dict | None:
        """The camera for a new printer: a verified one, else Moonraker's advertised pair, else None."""
        found = self.find_camera()
        return found if found["stream_url"] else None

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
