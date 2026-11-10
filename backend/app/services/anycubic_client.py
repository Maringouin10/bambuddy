"""Anycubic printer client (LAN Mode, MQTT over TLS).

The Kobra 3 Max (and its siblings on the same firmware family) has a "LAN
Mode" switch in its touchscreen settings. With it on, the printer runs an MQTT
broker on the local network and answers a small HTTP discovery service; with it
off (the default, "cloud" mode) it only talks to Anycubic's cloud and Bambuddy
cannot reach it.

This module gives such a printer the same face as ``BambuMQTTClient`` (and
``MoonrakerClient``) so ``PrinterManager``, the status route, the WebSocket
fan-out and the print-lifecycle callbacks work unchanged.

Protocol (LAN Mode, as implemented by Anycubic Slicer Next and documented by
the community projects that reverse-engineered it):

1. ``GET http://<ip>:18910/info`` returns a discovery document with a 32-char
   ``token``, a ``ctrlInfoUrl``, the ``modelId`` and ``ctrlType`` (``"lan"``,
   or ``"cloud"`` when LAN Mode is off).
2. ``POST ctrlInfoUrl?ts=..&nonce=..&sign=..&did=..`` where
   ``sign = md5(md5(token[:16]) + ts + nonce)`` returns broker credentials
   AES-128-CBC encrypted with key ``token[16:]`` and an IV taken from the
   reply's own ``data.token``. They rotate: they are kept in memory only and
   fetched again on every reconnect.
3. MQTT v3.1.1 over TLS (self-signed certificate, so not verified) on the
   broker the credentials name (port 9883). Reports arrive on
   ``anycubic/anycubicCloud/v1/printer/public/<modelId>/<deviceId>/#`` and
   commands go to ``anycubic/anycubicCloud/v1/web/printer/<modelId>/<deviceId>/<kind>``.
4. G-code files are uploaded with ``POST http://<ip>:18910/gcode_upload?s=<key>``
   (the URL the ``info`` report advertises) and started with a ``print/start``
   command.

There is no FTP, no HMS and no raw G-code channel: ``PrinterState.ftp_capable``
is False, so archives record name-and-timing entries for prints Bambuddy did
not send itself.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import posixpath
import secrets
import ssl
import string
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from backend.app.services.bambu_mqtt import MQTTLogEntry, PrinterState

logger = logging.getLogger(__name__)

PRINTER_TYPE_ANYCUBIC = "anycubic"

DISCOVERY_PORT = 18910
DEFAULT_BROKER_PORT = 9883
TOPIC_PREFIX = "anycubic/anycubicCloud/v1"

#: Anycubic model ids (``modelId`` of the discovery document).
ANYCUBIC_MODEL_IDS: dict[int, str] = {
    20024: "Kobra 3",
    20025: "Kobra S1",
    20026: "Kobra 3 Max",
    20027: "Kobra 3 V2",
    20030: "Kobra X",
}
#: The models Bambuddy supports (the ones it was written and tested for).
SUPPORTED_MODEL_IDS = frozenset({20026})

# ``last_connect_error`` slugs (the create route turns them into a message)
ERROR_UNREACHABLE = "unreachable"
ERROR_LAN_MODE_OFF = "lan_mode_off"
ERROR_NOT_ANYCUBIC = "not_anycubic"
ERROR_REJECTED = "rejected"
ERROR_UNAUTHORIZED = "unauthorized"

_TOKEN_LENGTH = 32
_NONCE_ALPHABET = string.ascii_letters + string.digits
_DID_ALPHABET = string.ascii_uppercase + string.digits
_TOPIC_UNSAFE = set("/+#\x00")
OK_CODES = (0, 200)

# Job ``state`` words (the firmware's own spellings, typos included).
JOB_OVER_STATES = frozenset({"finished", "stopped", "stoped", "failed", "canceled", "cancelled"})
JOB_PAUSED_STATES = frozenset({"pausing", "paused"})
JOB_RESUMING_STATES = frozenset({"resuming", "resumed"})
JOB_PREPARING_STATES = frozenset(
    {"downloading", "checking", "preheating", "slicing", "auto_leveling", "leveling", "levelling"}
)
#: ``print`` report states of a filament change: not job states.
FILAMENT_STEP_STATES = frozenset(
    {"extfilbox_filament_feeding", "ace_filament_feeding", "extfilbox_filament_unfeeding", "nozzle_purging"}
)
#: ``print_status``: 1 printing, 2 complete, 3 cancelled, 4 downloading, 5 checking,
#: 6 preheating, 7 slicing, 9 levelling. 0 / 8 / anything else: keep the previous one.
PRINT_STATUS_PREPARING = frozenset({4, 5, 6, 7, 9})
PRINT_STATUS_KNOWN = frozenset({1, 2, 3, 4, 5, 6, 7, 9})

#: Query action per report kind (``multiColorBox`` only answers ``getInfo``).
QUERY_KINDS = {"info": "query", "multiColorBox": "getInfo", "light": "query"}

DEFAULT_LIGHT_TYPE = 2  # the light a client may switch (chamber / head LED)
SLOT_STATUS_LOADED = 5
ACE_SLOT_EMPTY = 2  # edit_status: the slot holds nothing


# ---------------------------------------------------------------------------- handshake (pure)


def model_name_for(model_id: int | None) -> str:
    if model_id is None:
        return "Anycubic printer"
    return ANYCUBIC_MODEL_IDS.get(model_id, f"Anycubic printer (model {model_id})")


def build_signature(token: str, ts: int, nonce: str) -> str:
    """``md5_hex(md5_hex(token[:16]) + str(ts) + nonce)``."""
    keyed = hashlib.md5(token[:16].encode("ascii"), usedforsecurity=False).hexdigest()
    return hashlib.md5((keyed + str(ts) + nonce).encode("ascii"), usedforsecurity=False).hexdigest()


def control_url(token: str, ctrl_info_url: str, ts: int, nonce: str, did: str) -> str:
    separator = "&" if "?" in ctrl_info_url else "?"
    return f"{ctrl_info_url}{separator}ts={ts}&nonce={nonce}&sign={build_signature(token, ts, nonce)}&did={did}"


def build_iv(source: str) -> bytes:
    return source.encode("utf-8")[:16].ljust(16, b"\x00")


def decrypt_info(info_b64: str, iv_source: str, token: str) -> bytes:
    """AES-128-CBC + PKCS#7 decrypt of the base64 ``info`` with key ``token[16:32]``."""
    try:
        ciphertext = base64.b64decode(info_b64, validate=True)
        decryptor = Cipher(algorithms.AES(token[16:32].encode("ascii")), modes.CBC(build_iv(iv_source))).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except (ValueError, TypeError) as exc:
        raise AnycubicError(ERROR_NOT_ANYCUBIC, "could not decrypt the broker credentials") from exc


def encrypt_info(plaintext: bytes, iv_source: str, token: str) -> str:
    """Inverse of :func:`decrypt_info` (used by the tests' fake printer)."""
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(token[16:32].encode("ascii")), modes.CBC(build_iv(iv_source))).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def parse_discovery(document: object) -> dict:
    """Validate the ``/info`` discovery document; raise :class:`AnycubicError` when unusable."""
    if not isinstance(document, dict):
        raise AnycubicError(ERROR_NOT_ANYCUBIC, "discovery document is not a JSON object")
    if document.get("ctrlType") == "cloud":
        raise AnycubicError(ERROR_LAN_MODE_OFF, "LAN Mode is switched off on the printer")
    token = document.get("token")
    url = document.get("ctrlInfoUrl")
    model_id = document.get("modelId")
    if isinstance(model_id, str) and model_id.isdigit():
        model_id = int(model_id)
    if (
        not isinstance(token, str)
        or len(token) != _TOKEN_LENGTH
        or not token.isascii()
        or not isinstance(url, str)
        or not url.startswith(("http://", "https://"))
        or not isinstance(model_id, int)
        or isinstance(model_id, bool)
    ):
        raise AnycubicError(ERROR_NOT_ANYCUBIC, "discovery document lacks a usable token, ctrlInfoUrl or modelId")
    if document.get("ctrlType") != "lan":
        raise AnycubicError(ERROR_NOT_ANYCUBIC, f"unexpected ctrlType {document.get('ctrlType')!r}")
    return {
        "token": token,
        "ctrl_info_url": url,
        "model_id": model_id,
        "serial": document.get("cn") if isinstance(document.get("cn"), str) else None,
        "model_name": document.get("modelName") if isinstance(document.get("modelName"), str) else None,
    }


def parse_control_response(document: object, token: str) -> dict:
    """Validate the control reply and decrypt the broker credentials."""
    if not isinstance(document, dict):
        raise AnycubicError(ERROR_NOT_ANYCUBIC, "control reply is not a JSON object")
    if document.get("code") != 200:
        raise AnycubicError(ERROR_REJECTED, f"printer refused the control request (code {document.get('code')!r})")
    data = document.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("info"), str) or not isinstance(data.get("token"), str):
        raise AnycubicError(ERROR_NOT_ANYCUBIC, "control reply lacks data.info / data.token")
    try:
        creds = json.loads(decrypt_info(data["info"], data["token"], token).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AnycubicError(ERROR_NOT_ANYCUBIC, "decrypted credentials are not JSON") from exc
    if not isinstance(creds, dict) or not all(
        isinstance(creds.get(k), str) and creds.get(k) for k in ("broker", "username", "password", "deviceId")
    ):
        raise AnycubicError(ERROR_NOT_ANYCUBIC, "decrypted credentials lack a field")
    if _TOPIC_UNSAFE & set(creds["deviceId"]):
        raise AnycubicError(ERROR_NOT_ANYCUBIC, "deviceId contains MQTT topic characters")
    parts = urlsplit(creds["broker"])
    if parts.scheme not in ("mqtt", "mqtts") or not parts.hostname:
        raise AnycubicError(ERROR_NOT_ANYCUBIC, "broker is not an mqtt(s) URL")
    return {
        "host": parts.hostname,
        "port": parts.port or DEFAULT_BROKER_PORT,
        "username": creds["username"],
        "password": creds["password"],
        "device_id": creds["deviceId"],
    }


def _json_reply(response: httpx.Response) -> object:
    if response.status_code != 200:
        raise AnycubicError(ERROR_NOT_ANYCUBIC, f"HTTP {response.status_code}")
    try:
        return json.loads(response.content)  # the printer sends no JSON content type
    except ValueError as exc:
        raise AnycubicError(ERROR_NOT_ANYCUBIC, "reply is not JSON") from exc


def fetch_discovery(http: httpx.Client, ip_address: str) -> dict:
    try:
        response = http.get(f"http://{ip_address}:{DISCOVERY_PORT}/info")
    except httpx.HTTPError as exc:
        raise AnycubicError(ERROR_UNREACHABLE, str(exc) or type(exc).__name__) from exc
    return parse_discovery(_json_reply(response))


def handshake(http: httpx.Client, ip_address: str, now_ms: int | None = None) -> tuple[dict, dict]:
    """Discovery + signed control request: ``(discovery, broker credentials)``."""
    discovery = fetch_discovery(http, ip_address)
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    nonce = "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(6))
    did = "".join(secrets.choice(_DID_ALPHABET) for _ in range(32))
    url = control_url(discovery["token"], discovery["ctrl_info_url"], ts, nonce, did)
    try:
        response = http.post(url)
    except httpx.HTTPError as exc:
        raise AnycubicError(ERROR_UNREACHABLE, str(exc) or type(exc).__name__) from exc
    return discovery, parse_control_response(_json_reply(response), discovery["token"])


# ---------------------------------------------------------------------------- reports (pure)


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _whole(value: object) -> int | None:
    number = _num(value)
    return int(number) if number is not None and float(number).is_integer() else None


def job_task_id(job: dict | None) -> str | None:
    if not job:
        return None
    for key in ("task_id", "taskid"):
        value = job.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def job_gcode_state(job: dict | None, printer_state: str | None) -> str:
    """The Bambu ``gcode_state`` word for an Anycubic job (``None`` = no job)."""
    if not job:
        return "PREPARE" if printer_state == "busy" else "IDLE"
    state = str(job.get("state") or "").lower()
    status = _whole(job.get("print_status"))
    if state == "finished" or status == 2:
        return "FINISH"
    if state == "failed":
        return "FAILED"
    if state in JOB_OVER_STATES or status == 3:
        return "IDLE"  # stopped / cancelled: the lifecycle records it as "aborted"
    if state in JOB_RESUMING_STATES:
        return "RUNNING"
    if state in JOB_PAUSED_STATES or job.get("pause") in (1, True, "1"):
        return "PAUSE"
    if state in JOB_PREPARING_STATES or status in PRINT_STATUS_PREPARING:
        return "PREPARE"
    return "RUNNING"


def _rgb_hex(value: object) -> str | None:
    if isinstance(value, list) and len(value) >= 3:
        parts = [_whole(c) for c in value[:3]]
        if all(p is not None and 0 <= p <= 255 for p in parts):
            return "".join(f"{p:02X}" for p in parts) + "FF"
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) == 6 and all(c in string.hexdigits for c in text):
            return text.upper() + "FF"
    return None


def ace_units(boxes: list[dict]) -> tuple[list[dict], int]:
    """ACE boxes as AMS-shaped units, and the global tray index feeding the printer (-1 = none)."""
    units: list[dict] = []
    loaded = -1
    for position, box in enumerate(boxes):
        box_id = _whole(box.get("id"))
        unit_id = box_id if box_id is not None and box_id >= 0 else position
        slots = [s for s in box.get("slots") or [] if isinstance(s, dict)]
        loaded_slot = _whole(box.get("loaded_slot"))
        trays = []
        for i, slot in enumerate(slots):
            index = _whole(slot.get("index"))
            index = index if index is not None else i
            material = str(slot.get("type") or "").strip() or None
            present = _whole(slot.get("edit_status")) != ACE_SLOT_EMPTY and material is not None
            if (loaded_slot is None or loaded_slot < 0) and _whole(slot.get("status")) == SLOT_STATUS_LOADED:
                loaded_slot = index
            trays.append(
                {
                    "id": index,
                    "tray_color": _rgb_hex(slot.get("color")) if present else None,
                    "tray_type": material if present else None,
                    "tray_sub_brands": material if present else None,
                    "remain": -1,
                    "state": 10 if present else 9,
                    "exists": present,
                }
            )
        if loaded_slot is not None and loaded_slot >= 0 and loaded < 0:
            loaded = unit_id * 4 + loaded_slot
        humidity = _num(box.get("humidity"))
        units.append(
            {
                "id": unit_id,
                "humidity_raw": humidity if humidity else None,  # 0 = no sensor
                "temp": _num(box.get("temp")),
                "sn": "",
                "sw_ver": "",
                "module_type": "ace",
                "tray": trays,
            }
        )
    return units, loaded


def _slug_name(filename: str | None) -> str | None:
    if not filename:
        return None
    name = filename.replace("\\", "/").split("/")[-1]
    for suffix in (".gcode", ".gco", ".g", ".3mf"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name or None


def _tls_context() -> ssl.SSLContext:
    # The broker's certificate is self-signed for an address that varies per
    # unit: there is nothing to verify it against. The link is still encrypted.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _default_mqtt_factory(client_id: str):
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion

    client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv311)
    client.tls_set_context(_tls_context())
    client.tls_insecure_set(True)
    return client


# ---------------------------------------------------------------------------- client


class AnycubicClient:
    """An Anycubic printer in LAN Mode."""

    QUERY_INTERVAL = 5.0  # seconds between info queries while connected
    SLOW_QUERY_EVERY = 6  # ACE / light queries every N info queries
    RETRY_INTERVAL = 10.0
    CONNECT_TIMEOUT = 15.0
    STALE_TIMEOUT = 30.0
    HTTP_TIMEOUT = 5.0
    UPLOAD_TIMEOUT = 600.0
    START_CONFIRM_TIMEOUT = 10.0

    printer_type = PRINTER_TYPE_ANYCUBIC

    def __init__(
        self,
        ip_address: str,
        serial_number: str,
        access_code: str = "",
        model: str | None = None,
        on_state_change: Callable[[PrinterState], None] | None = None,
        on_print_start: Callable[[dict], None] | None = None,
        on_print_complete: Callable[[dict], None] | None = None,
        on_layer_change: Callable[[int], None] | None = None,
        on_print_progress: Callable[[int], None] | None = None,
        on_bed_temp_update: Callable[[float], None] | None = None,
        on_print_running_observed: Callable[[dict], None] | None = None,
        on_finish_photo_moment: Callable[[dict], None] | None = None,
        mqtt_factory: Callable[[str], object] | None = None,
        http_transport: httpx.BaseTransport | None = None,
        **_bambu_only_callbacks: object,
    ):
        self.ip_address = ip_address
        self.serial_number = serial_number
        self.access_code = access_code  # unused: LAN Mode needs no code
        self.model = model
        self.on_state_change = on_state_change
        self.on_print_start = on_print_start
        self.on_print_complete = on_print_complete
        self.on_layer_change = on_layer_change
        self.on_print_progress = on_print_progress
        self.on_bed_temp_update = on_bed_temp_update
        self.on_print_running_observed = on_print_running_observed
        self.on_finish_photo_moment = on_finish_photo_moment

        self.state = PrinterState(ftp_capable=False)
        self.state.raw_data = {"printer_type": PRINTER_TYPE_ANYCUBIC}
        self.last_connect_error: str | None = None
        self.last_error: str | None = None
        self.last_dispatch_subtask_id: str | None = None
        self._drying_targets: dict[int, dict] = {}
        self._last_message_time: float = 0.0
        self._report_messages_since_connect = 0

        self._mqtt_factory = mqtt_factory or _default_mqtt_factory
        self._http_transport = http_transport
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._connack = threading.Event()
        self._connack_rc: int | None = None
        self._mqtt = None
        self._mqtt_up = False
        self._consecutive_failures = 0
        self._query_count = 0

        self.discovery: dict | None = None
        self._creds: dict | None = None
        self._info: dict = {}
        self._job: dict | None = None
        self._last_job: dict | None = None
        self._boxes: list[dict] = []
        self._light_type: int = DEFAULT_LIGHT_TYPE
        self._state_before_power_off: str | None = None
        self._start_waiter: dict | None = None

        self._previous_state: str | None = None
        self._previous_filename: str | None = None
        self._was_running = False
        self._completion_triggered = False
        self._first_report_seen = False
        self._last_layer = 0
        self._last_progress_int = -1
        self._last_valid_progress = 0.0
        self._last_valid_layer = 0

        self._message_log: deque[MQTTLogEntry] = deque(maxlen=100)
        self._logging_enabled = False

    # ------------------------------------------------------------------ lifecycle

    def _http_client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(
            timeout=timeout or self.HTTP_TIMEOUT, follow_redirects=False, transport=self._http_transport
        )

    def connect(self, loop=None) -> None:  # noqa: ARG002 - loop kept for interface parity
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"anycubic-{self.ip_address}", daemon=True)
        self._thread.start()

    def disconnect(self, timeout: float = 0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and timeout and thread is not threading.current_thread():
            thread.join(timeout)
        self._teardown_mqtt()
        self.state.connected = False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._mqtt is None or not self._mqtt_up:
                    self._teardown_mqtt()
                    self.establish()
                self.send_queries()
                self._consecutive_failures = 0
            except AnycubicError as exc:
                self._register_failure(exc.slug, str(exc))
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                self._register_failure(ERROR_UNREACHABLE, f"{type(exc).__name__}: {exc}")
            if self.state.connected and self.is_stale():
                logger.warning("[%s] Anycubic: no report for %.0fs, reconnecting", self.ip_address, self.STALE_TIMEOUT)
                self._teardown_mqtt()
                self._mark_disconnected()
            delay = self.QUERY_INTERVAL if self._mqtt_up else self.RETRY_INTERVAL
            self._wake.wait(delay)
            self._wake.clear()

    def _register_failure(self, slug: str, detail: str) -> None:
        self._consecutive_failures += 1
        self.last_connect_error = slug
        if self._consecutive_failures == 1 or self._consecutive_failures % 30 == 0:
            logger.warning("[%s] Anycubic %s: %s", self.ip_address, slug, detail)
        self._teardown_mqtt()
        self._mark_disconnected()

    def _mark_disconnected(self) -> None:
        if self.state.connected:
            self.state.connected = False
            self._emit_state()

    def establish(self) -> None:
        """Handshake and connect the MQTT session (blocking, raises :class:`AnycubicError`)."""
        with self._http_client() as http:
            discovery, creds = handshake(http, self.ip_address)
        self.discovery, self._creds = discovery, creds
        self.state.raw_data["model_id"] = discovery["model_id"]
        self.state.raw_data["model_name"] = model_name_for(discovery["model_id"])
        if discovery["model_id"] not in SUPPORTED_MODEL_IDS:
            logger.info(
                "[%s] Anycubic model %s is not one Bambuddy was tested with",
                self.ip_address,
                model_name_for(discovery["model_id"]),
            )
        client = self._mqtt_factory(f"bambuddy-{secrets.token_hex(6)}")
        client.username_pw_set(creds["username"], creds["password"])
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._connack.clear()
        self._connack_rc = None
        self._mqtt = client
        try:
            client.connect(creds["host"], creds["port"], 60)
        except OSError as exc:
            raise AnycubicError(ERROR_UNREACHABLE, f"MQTT broker unreachable: {exc}") from exc
        client.loop_start()
        if not self._connack.wait(self.CONNECT_TIMEOUT):
            raise AnycubicError(ERROR_UNREACHABLE, "MQTT broker did not answer")
        if self._connack_rc:
            raise AnycubicError(ERROR_UNAUTHORIZED, f"MQTT broker refused the session (rc {self._connack_rc})")
        self.last_connect_error = None
        self._query_count = 0

    def _teardown_mqtt(self) -> None:
        client, self._mqtt = self._mqtt, None
        self._mqtt_up = False
        if client is None:
            return
        for step in ("disconnect", "loop_stop"):
            try:
                getattr(client, step)()
            except Exception:  # noqa: BLE001 - best effort teardown
                pass

    # ------------------------------------------------------------------ MQTT callbacks

    @property
    def report_topic(self) -> str:
        return f"{TOPIC_PREFIX}/printer/public/{self.discovery['model_id']}/{self._creds['device_id']}/#"

    def command_topic(self, kind: str) -> str:
        return f"{TOPIC_PREFIX}/web/printer/{self.discovery['model_id']}/{self._creds['device_id']}/{kind}"

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        failed = reason_code.is_failure if hasattr(reason_code, "is_failure") else bool(reason_code)
        self._connack_rc = int(getattr(reason_code, "value", reason_code) or 1) if failed else 0
        if not self._connack_rc:
            client.subscribe(self.report_topic)
            self._mqtt_up = True
            logger.info(
                "[%s] Connected to Anycubic %s (LAN Mode)",
                self.ip_address,
                self.state.raw_data.get("model_name"),
            )
        self._connack.set()

    def _on_disconnect(self, _client, _userdata, *_args) -> None:
        if self._mqtt_up:
            logger.info("[%s] Anycubic MQTT session closed", self.ip_address)
        self._mqtt_up = False
        self._wake.set()

    def _on_message(self, _client, _userdata, message) -> None:
        try:
            payload = json.loads(bytes(message.payload))
        except ValueError:
            return
        if isinstance(payload, dict):
            self.handle_report(payload, topic=getattr(message, "topic", ""))

    # ------------------------------------------------------------------ queries / commands

    def _publish(self, kind: str, payload: dict) -> bool:
        client = self._mqtt
        if client is None or not self._mqtt_up:
            logger.warning("[%s] Cannot send %s/%s: not connected", self.ip_address, kind, payload.get("action"))
            return False
        if self._logging_enabled:
            self._message_log.append(
                MQTTLogEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    topic=self.command_topic(kind),
                    direction="out",
                    payload=payload,
                )
            )
        result = client.publish(self.command_topic(kind), json.dumps(payload, separators=(",", ":")))
        return getattr(result, "rc", 0) == 0

    def send_queries(self) -> None:
        self._publish("info", {"type": "info", "action": "query", "data": {}})
        if self._query_count % self.SLOW_QUERY_EVERY == 0:
            self._publish("multiColorBox", {"type": "multiColorBox", "action": "getInfo", "data": {}})
            self._publish("light", {"type": "light", "action": "query", "data": {}})
        self._query_count += 1

    def command(self, kind: str, action: str, data: dict | None = None) -> bool:
        return self._publish(
            kind,
            {
                "type": kind,
                "action": action,
                "timestamp": int(time.time() * 1000),
                "msgid": str(uuid.uuid4()),
                "data": data or {},
            },
        )

    # ------------------------------------------------------------------ reports

    def handle_report(self, message: dict, topic: str = "") -> None:
        kind = str(message.get("type") or (topic.rsplit("/", 2)[-2] if topic.count("/") >= 2 else ""))
        action = str(message.get("action") or "")
        state = str(message.get("state") or "")
        code = message.get("code")
        data = message.get("data")
        self._last_message_time = time.time()
        self._report_messages_since_connect += 1
        if not self.state.connected:
            self.state.connected = True
        self._state_before_power_off = None
        if self._logging_enabled:
            self._message_log.append(
                MQTTLogEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    topic=topic or kind,
                    direction="in",
                    payload=message,
                )
            )
        refused = state == "failed" or (isinstance(code, int) and not isinstance(code, bool) and code not in OK_CODES)
        with self._lock:
            if kind == "info" and isinstance(data, dict):
                self._apply_info(data)
            elif kind == "tempature" and isinstance(data, dict):
                self._apply_temps(data)
            elif kind == "fan" and isinstance(data, dict):
                self._apply_fans(data)
            elif kind == "light" and isinstance(data, dict):
                self._apply_light(data)
            elif kind == "multiColorBox" and not refused:
                self._apply_boxes(data, full=action == "getInfo")
            elif kind == "print":
                self._apply_print(action, state, data if isinstance(data, dict) else {}, refused, message)
            self._refresh()

    def _apply_info(self, data: dict) -> None:
        self._info.update({k: v for k, v in data.items() if k not in ("project", "last_project")})
        self.state.firmware_version = data.get("version") or self.state.firmware_version
        if isinstance(data.get("temp"), dict):
            self._apply_temps(data["temp"])
        self._apply_fans(data)
        mode = _whole(data.get("print_speed_mode"))
        if mode in (1, 2, 3):
            self.state.speed_level = mode
        if "project" in data:
            project = data.get("project")
            self._job = dict(project) if isinstance(project, dict) else None
        if isinstance(data.get("last_project"), dict):
            self._last_job = dict(data["last_project"])
        urls = data.get("urls") if isinstance(data.get("urls"), dict) else {}
        if urls.get("rtspUrl"):
            self.state.raw_data["camera_url"] = urls.get("rtspUrl")

    def _apply_temps(self, temp: dict) -> None:
        temps = self.state.temperatures
        for src, dst in (
            ("curr_nozzle_temp", "nozzle"),
            ("target_nozzle_temp", "nozzle_target"),
            ("curr_hotbed_temp", "bed"),
            ("target_hotbed_temp", "bed_target"),
            ("curr_chamber_temp", "chamber"),
            ("target_chamber_temp", "chamber_target"),
        ):
            value = _num(temp.get(src))
            if value is not None:
                temps[dst] = round(value, 1)
        for name in ("nozzle", "bed"):
            if name in temps:
                target = temps.get(f"{name}_target", 0.0)
                temps[f"{name}_heating"] = target > 0 and temps[name] < target - 2
        if "curr_hotbed_temp" in temp and self.on_bed_temp_update:
            self.on_bed_temp_update(temps["bed"])

    def _apply_fans(self, data: dict) -> None:
        fans = self.state.raw_data.setdefault("fans", {})
        for src, attr, role in (
            ("fan_speed_pct", "cooling_fan_speed", "part"),
            ("aux_fan_speed_pct", "big_fan1_speed", "aux"),
        ):
            value = _num(data.get(src))
            if value is not None:
                setattr(self.state, attr, max(0, min(100, int(round(value)))))
                fans[role] = src

    def _apply_light(self, data: dict) -> None:
        lights = data.get("lights") if isinstance(data.get("lights"), list) else [data]
        for light in lights:
            if not isinstance(light, dict) or _whole(light.get("type")) is None:
                continue
            light_type = _whole(light.get("type"))
            if light_type == DEFAULT_LIGHT_TYPE or len(lights) == 1:
                self._light_type = light_type
                if light.get("status") is not None:
                    self.state.chamber_light = _whole(light.get("status")) == 1

    def _apply_boxes(self, data: object, full: bool) -> None:
        raw = data.get("multi_color_box") if isinstance(data, dict) else data
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            return
        boxes = [b for b in raw if isinstance(b, dict)]
        if full:
            self._boxes = boxes
        else:  # partial report: merge by box id, then slots by index
            by_id = {_whole(b.get("id")): b for b in self._boxes}
            for box in boxes:
                old = by_id.get(_whole(box.get("id")))
                if old is None:
                    self._boxes.append(box)
                    continue
                slots = {_whole(s.get("index")): s for s in old.get("slots") or [] if isinstance(s, dict)}
                for slot in box.get("slots") or []:
                    if isinstance(slot, dict):
                        slots[_whole(slot.get("index"))] = {**slots.get(_whole(slot.get("index")), {}), **slot}
                old.update({k: v for k, v in box.items() if k != "slots"})
                old["slots"] = list(slots.values())
        units, loaded = ace_units(self._boxes)
        if units:
            self.state.raw_data["ams"] = units
            self.state.tray_now = loaded if loaded >= 0 else 255
        else:
            self.state.raw_data.pop("ams", None)

    def _apply_print(self, action: str, state: str, data: dict, refused: bool, message: dict) -> None:
        if refused:
            text = str(message.get("msg") or f"code {message.get('code')}")
            logger.warning("[%s] Anycubic refused print/%s: %s", self.ip_address, action, text)
            if action == "start" and self._start_waiter is not None:
                self._start_waiter["error"] = text
                self._start_waiter["event"].set()
            return
        if state in FILAMENT_STEP_STATES:
            return
        job_fields = {k: v for k, v in data.items() if k not in ("settings", "task_settings", "ams_settings")}
        if not job_fields:
            return
        if self._job is None:
            self._job = {}
        self._job.update(job_fields)
        if state and state not in ("updated", "success", "done", "start"):
            self._job["state"] = state
        if action == "start" and self._start_waiter is not None:
            self._start_waiter["event"].set()

    # ------------------------------------------------------------------ state

    def _refresh(self) -> None:
        job = self._job
        new_state = job_gcode_state(job, self._info.get("state"))
        filename = (job or {}).get("filename") or None
        if job is None and self._was_running and self._last_job:
            # The job vanished from ``info``: its last state says how it ended.
            last_state = job_gcode_state(self._last_job, None)
            if last_state in ("FINISH", "FAILED"):
                new_state = last_state
            filename = self._last_job.get("filename") or self._previous_filename

        active = new_state in ("RUNNING", "PAUSE", "PREPARE")
        progress = _num((job or {}).get("progress")) or 0.0
        if new_state == "FINISH":
            progress = 100.0
        elif not active:
            progress = 0.0
        self.state.progress = max(0.0, min(100.0, round(progress, 1)))
        if new_state in ("RUNNING", "PAUSE") and progress > 0:
            self._last_valid_progress = self.state.progress
        remaining = _num((job or {}).get("remain_time")) if active else None
        self.state.remaining_time = int(remaining) if remaining and remaining > 0 else 0
        total = _whole((job or {}).get("total_layers")) if active else None
        layer = _whole((job or {}).get("curr_layer")) if new_state in ("RUNNING", "PAUSE") else None
        self.state.total_layers = max(0, total or 0)
        self.state.layer_num = max(0, layer or 0)
        if self.state.layer_num > 0:
            self._last_valid_layer = self.state.layer_num
        if self.on_layer_change and self.state.layer_num > 0 and self.state.layer_num != self._last_layer:
            self.on_layer_change(self.state.layer_num)
        self._last_layer = self.state.layer_num
        progress_int = int(self.state.progress)
        if self.on_print_progress and new_state == "RUNNING" and progress_int > self._last_progress_int >= 0:
            self.on_print_progress(progress_int)
        self._last_progress_int = progress_int if new_state in ("RUNNING", "PAUSE") else -1

        self.state.printer_error = (
            "The print failed on the printer" if new_state == "FAILED" and self._was_running else None
        )
        shown = filename if active or new_state in ("FINISH", "FAILED") else None
        self.state.state = new_state
        self.state.gcode_file = shown
        self.state.current_print = _slug_name(shown)
        self.state.subtask_name = _slug_name(shown)
        self.state.raw_data.update(
            {
                "gcode_state": new_state,
                "anycubic_job_state": (job or {}).get("state"),
                "anycubic_printer_state": self._info.get("state"),
                "task_id": job_task_id(job),
                "gcode_file": shown,
                "subtask_name": self.state.subtask_name,
                "mc_percent": self.state.progress,
                "mc_remaining_time": self.state.remaining_time,
                "layer_num": self.state.layer_num,
                "total_layer_num": self.state.total_layers,
                "filament_used": (job or {}).get("supplies_usage"),
            }
        )
        self._track_lifecycle(new_state, filename)
        self._emit_state()

    def _track_lifecycle(self, new_state: str, filename: str | None) -> None:
        prev = self._previous_state
        first_report = not self._first_report_seen
        self._first_report_seen = True
        running = new_state in ("RUNNING", "PAUSE")
        is_new_print = new_state == "RUNNING" and prev not in ("RUNNING", "PAUSE") and not first_report
        is_file_change = (
            running
            and filename
            and self._previous_filename
            and filename != self._previous_filename
            and self._was_running
        )
        if (is_new_print and not self._was_running) or is_file_change:
            self._completion_triggered = False
            self._was_running = True
            self._last_valid_progress = 0.0
            self._last_valid_layer = 0
            logger.info("[%s] PRINT START detected - file: %s", self.ip_address, filename)
            if self.on_print_start:
                self.on_print_start(self._lifecycle_payload(filename))
        elif first_report and running:
            self._was_running = True
            self._completion_triggered = False
            if self.on_print_running_observed:
                self.on_print_running_observed(self._lifecycle_payload(filename))

        if (
            self._was_running
            and not self._completion_triggered
            and new_state in ("FINISH", "FAILED", "IDLE")
            and prev != new_state
        ):
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
        if self.state.connected and self.is_stale():
            self.state.connected = False
            self._wake.set()
            self._emit_state()
        return self.state.connected

    def is_stale(self) -> bool:
        return bool(self._last_message_time) and time.time() - self._last_message_time > self.STALE_TIMEOUT

    def force_reconnect_stale_session(self, reason: str) -> None:
        logger.info("[%s] Forcing Anycubic reconnect: %s", self.ip_address, reason)
        self._teardown_mqtt()
        self._wake.set()

    @property
    def report_messages_since_connect(self) -> int:
        return self._report_messages_since_connect

    def mark_power_off(self) -> bool:
        if not self.state.connected:
            return False
        previous = self.state.state
        self.state.connected = False
        self.state.state = "unknown"
        if self._state_before_power_off is None and previous not in ("", "unknown"):
            self._state_before_power_off = previous
        return True

    def request_status_update(self) -> bool:
        if not self._mqtt_up:
            return False
        return self._publish("info", {"type": "info", "action": "query", "data": {}})

    # ------------------------------------------------------------------ job commands

    def _task_id(self) -> str:
        return job_task_id(self._job) or "-1"

    def pause_print(self) -> bool:
        return self.command("print", "pause", {"taskid": self._task_id()})

    def resume_print(self) -> bool:
        return self.command("print", "resume", {"taskid": self._task_id()})

    def stop_print(self) -> bool:
        return self.command("print", "stop", {"taskid": self._task_id()})

    def ams_mapping_for_start(self) -> dict:
        """``ams_settings`` of a start command: the slot that feeds the printer, if an ACE is fitted."""
        units, loaded = ace_units(self._boxes)
        if not units:
            return {"use_ams": False, "ams_box_mapping": []}
        trays = [(u["id"] * 4 + t["id"], t) for u in units for t in u["tray"] if t["exists"]]
        if not trays:
            return {"use_ams": False, "ams_box_mapping": []}
        index, tray = next(((i, t) for i, t in trays if i == loaded), trays[0])
        color = tray.get("tray_color") or "FFFFFFFF"
        rgb = [int(color[i : i + 2], 16) for i in (0, 2, 4)]
        return {
            "use_ams": True,
            "ams_box_mapping": [
                {
                    "paint_index": 0,
                    "ams_index": index,
                    "paint_color": rgb,
                    "ams_color": rgb,
                    "material_type": tray.get("tray_type") or "PLA",
                }
            ],
        }

    def start_print(self, filename: str, plate_id: int = 1, **_bambu_options: object) -> bool:  # noqa: ARG002
        """Start a G-code file already stored on the printer, waiting briefly for a refusal."""
        data = {
            "taskid": "-1",
            "filename": filename,
            "filepath": "/",
            "filetype": 1,
            "file_key": "",
            "file_name": "",
            "project_type": 1,
            "task_settings": {"ai_detect": 0, "camera_timelapse": 0},
            "ams_settings": self.ams_mapping_for_start(),
        }
        waiter = {"event": threading.Event(), "error": None}
        self._start_waiter = waiter
        try:
            if not self.command("print", "start", data):
                self.last_error = "not connected to the printer"
                return False
            waiter["event"].wait(self.START_CONFIRM_TIMEOUT)
        finally:
            self._start_waiter = None
        if waiter["error"]:
            self.last_error = f"the printer refused to start the print ({waiter['error']})"
            return False
        return True

    def upload_url(self) -> str | None:
        """The ``gcode_upload`` URL the printer advertises (it carries the upload key)."""
        url = str(
            ((self._info.get("urls") or {}) if isinstance(self._info.get("urls"), dict) else {}).get("fileUploadurl")
            or ""
        )
        if not url:
            return None
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            path = url if url.startswith("/") else f"/{url}"
            return f"http://{self.ip_address}:{DISCOVERY_PORT}{path}"
        if parts.hostname != self.ip_address:
            # Never send a file to a host other than the printer.
            return f"http://{self.ip_address}:{parts.port or DISCOVERY_PORT}{parts.path}?{parts.query}"
        return url

    def upload_file(self, local_path: str, remote_name: str) -> bool:
        url = self.upload_url()
        if not url:
            self.last_error = "the printer did not advertise an upload address (is LAN Mode on?)"
            return False
        size = os.path.getsize(local_path)
        try:
            with self._http_client(self.UPLOAD_TIMEOUT) as http, open(local_path, "rb") as fh:
                response = http.post(
                    url,
                    data={"filename": remote_name},
                    files={"gcode": (remote_name, fh, "application/octet-stream")},
                    headers={"X-File-Length": str(size)},
                )
        except (httpx.HTTPError, OSError) as exc:
            self.last_error = f"upload failed: {exc or type(exc).__name__}"
            return False
        if response.status_code != 200:
            self.last_error = f"upload failed: HTTP {response.status_code}"
            return False
        try:
            reply = json.loads(response.content)
        except ValueError:
            reply = None
        if isinstance(reply, dict) and _whole(reply.get("code")) not in (None, *OK_CODES):
            self.last_error = f"upload refused: {reply.get('message') or reply.get('msg') or reply.get('code')}"
            return False
        return True

    def upload_and_print(self, local_path: str, remote_name: str | None = None, start: bool = True) -> bool:
        self.last_error = None
        remote_name = posixpath.basename((remote_name or os.path.basename(local_path)).replace("\\", "/"))
        if not self._mqtt_up:
            self.last_error = "not connected to the printer"
            return False
        if not self.upload_file(local_path, remote_name):
            logger.warning("[%s] Anycubic upload of %s failed: %s", self.ip_address, remote_name, self.last_error)
            return False
        return self.start_print(remote_name) if start else True

    # ------------------------------------------------------------------ controls

    def set_nozzle_temperature(self, target: int, nozzle: int = 0) -> bool:  # noqa: ARG002
        return self.command(
            "tempature", "set", {"type": 0, "target_nozzle_temp": int(max(0, target)), "target_hotbed_temp": 0}
        )

    def set_bed_temperature(self, target: int) -> bool:
        return self.command(
            "tempature", "set", {"type": 1, "target_nozzle_temp": 0, "target_hotbed_temp": int(max(0, target))}
        )

    def set_chamber_temperature(self, target: int) -> bool:  # noqa: ARG002
        return False

    def set_print_speed(self, mode: int) -> bool:
        if mode not in (1, 2, 3, 4):
            return False
        mode = min(mode, 3)  # Silent / Standard / Sport; no "ludicrous"
        return self.command("print", "update", {"taskid": self._task_id(), "settings": {"print_speed_mode": mode}})

    def set_fan_speed(self, fan: int, speed: int) -> bool:
        """Bambu fan index (1 part, 2 auxiliary), speed 0-255."""
        key = {1: "fan_speed_pct", 2: "aux_fan_speed_pct"}.get(fan)
        if key is None:
            return False
        return self.command("fan", "setSpeed", {key: int(round(max(0, min(255, int(speed))) / 255 * 100))})

    def set_part_fan(self, speed: int) -> bool:
        return self.set_fan_speed(1, speed)

    def set_airduct_mode(self, mode: str) -> bool:  # noqa: ARG002
        return False

    def set_chamber_light(self, on: bool) -> bool:
        ok = self.command(
            "light", "control", {"type": self._light_type, "status": 1 if on else 0, "brightness": 100 if on else 0}
        )
        if ok:
            self.state.chamber_light = bool(on)
        return ok

    def send_gcode(self, gcode: str) -> bool:  # noqa: ARG002 - LAN Mode has no G-code channel
        return False

    def _unsupported(self, *_args: object, **_kwargs: object) -> bool:
        return False

    home_axes = move_axis = disable_motors = enable_motors = clear_hms_errors = skip_objects = _unsupported
    ams_load_filament = ams_unload_filament = ams_control = ams_set_filament_setting = _unsupported
    reset_ams_slot = extrusion_cali_sel = extrusion_cali_set = start_calibration = _unsupported
    set_xcam_option = set_ams_filament_backup = select_extruder = execute_hms_action = _unsupported
    send_drying_command = _unsupported

    def register_assignment_verification(self, *_args: object, **_kwargs: object) -> None:
        return None

    def get_webcam_urls(self) -> dict | None:
        # The printer's own camera is an HTTP-FLV stream Bambuddy cannot display.
        return None

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


def probe(ip_address: str, transport: httpx.BaseTransport | None = None, timeout: float = 5.0) -> dict:
    """Connection test for the add-printer dialog: handshake only (no MQTT)."""
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as http:
        discovery, _creds = handshake(http, ip_address)
    return {
        "model_id": discovery["model_id"],
        "model": model_name_for(discovery["model_id"]),
        "serial": discovery.get("serial"),
        "supported": discovery["model_id"] in SUPPORTED_MODEL_IDS,
    }


class AnycubicError(Exception):
    """A failed LAN Mode exchange, carrying the ``last_connect_error`` slug."""

    def __init__(self, slug: str, detail: str):
        super().__init__(detail)
        self.slug = slug
