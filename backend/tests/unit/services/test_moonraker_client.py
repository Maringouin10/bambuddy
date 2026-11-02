"""Tests for the Creality / Klipper (Moonraker) printer client.

The client is driven through ``_handshake()`` / ``_poll_once()`` against an
``httpx.MockTransport`` so no thread, socket or real printer is involved.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from backend.app.schemas.printer import PrinterCreate
from backend.app.services.moonraker_client import (
    ERROR_NOT_MOONRAKER,
    ERROR_UNAUTHORIZED,
    FanRef,
    MoonrakerClient,
    _ConnectError,
    _slug_name,
    discover_fans,
    fan_percent,
    parse_cfs_flat,
    parse_cfs_stock,
    speed_percent_to_level,
)
from backend.app.services.print_storage import REASON_NO_FTP_SERVICE, print_file_reachable_over_ftp

OBJECTS = [
    "print_stats",
    "virtual_sdcard",
    "extruder",
    "heater_bed",
    "display_status",
    "fan",
    "gcode_move",
    "toolhead",
    "webhooks",
    "output_pin LED",
]


class FakeMoonraker:
    """A tiny in-memory Moonraker: serves /printer/info, objects/list, query, files."""

    def __init__(self):
        self.objects = list(OBJECTS)
        self.uploads: list[tuple[str, bytes]] = []
        self.files: dict[str, bytes] = {}
        self.upload_result = (201, {"item": {"path": "x.gcode", "root": "gcodes"}, "print_started": True})
        self.klipper_state = "ready"
        self.status = {
            "print_stats": {"state": "standby", "filename": "", "print_duration": 0.0, "info": {}},
            "virtual_sdcard": {"progress": 0.0},
            "display_status": {"progress": 0.0},
            "extruder": {"temperature": 24.6, "target": 0.0},
            "heater_bed": {"temperature": 23.1, "target": 0.0},
            "fan": {"speed": 0.0},
            "gcode_move": {"speed_factor": 1.0, "gcode_position": [0, 0, 0, 0]},
            "webhooks": {"state": "ready"},
        }
        self.metadata = {"estimated_time": 3600, "layer_count": 100, "layer_height": 0.2, "first_layer_height": 0.2}
        self.posts: list[tuple[str, dict | None, str]] = []
        self.status_code = 200

    def set_print(self, state, filename="benchy.gcode", progress=0.0, duration=0.0, **info):
        self.status["print_stats"] = {
            "state": state,
            "filename": filename,
            "print_duration": duration,
            "info": info,
        }
        self.status["virtual_sdcard"]["progress"] = progress
        self.status["display_status"]["progress"] = progress

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"error": "nope"})
        if request.method == "POST" and path == "/server/files/upload":
            self.uploads.append((request.headers.get("content-type", ""), request.content))
            status, payload = self.upload_result
            return httpx.Response(status, json=payload)
        if request.method == "POST":
            body = (
                json.loads(request.content)
                if request.content and request.headers.get("content-type", "").startswith("application/json")
                else None
            )
            self.posts.append((path, body, request.url.query.decode()))
            return httpx.Response(200, json={"result": "ok"})
        if path == "/printer/info":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "state": self.klipper_state,
                        "state_message": "Printer is ready" if self.klipper_state == "ready" else "MCU shutdown",
                        "hostname": "ender3v3",
                        "software_version": "v0.11.0-creality",
                    }
                },
            )
        if path == "/printer/objects/list":
            return httpx.Response(200, json={"result": {"objects": self.objects}})
        if path == "/printer/objects/query":
            return httpx.Response(200, json={"result": {"status": self.status, "eventtime": 1.0}})
        if path.startswith("/server/files/gcodes/"):
            from urllib.parse import unquote

            key = unquote(path[len("/server/files/gcodes/") :])
            if key in self.files:
                return httpx.Response(
                    200, content=self.files[key], headers={"content-length": str(len(self.files[key]))}
                )
            return httpx.Response(404)
        if path == "/server/files/metadata":
            return httpx.Response(200, json={"result": self.metadata})
        if path == "/server/webcams/list":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "webcams": [
                            {
                                "enabled": True,
                                "stream_url": "/webcam/?action=stream",
                                "snapshot_url": "/webcam/?action=snapshot",
                            }
                        ]
                    }
                },
            )
        return httpx.Response(404, json={"error": "not found"})


def make_client(fake: FakeMoonraker, **callbacks) -> MoonrakerClient:
    client = MoonrakerClient("192.168.1.50", "CREALITY-192.168.1.50", **callbacks)
    client._http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return client


def tick(client: MoonrakerClient) -> None:
    if not client._handshake_done:
        client._handshake()
    client._poll_once()


def test_poll_populates_state_in_bambu_vocabulary():
    fake = FakeMoonraker()
    fake.status["extruder"] = {"temperature": 200.04, "target": 210.0}
    fake.status["heater_bed"] = {"temperature": 59.9, "target": 60.0}
    fake.status["fan"]["speed"] = 0.5
    fake.status["gcode_move"]["speed_factor"] = 1.24
    fake.set_print("printing", "benchy.gcode", progress=0.5, duration=1200.0, current_layer=50, total_layer=100)
    client = make_client(fake)

    tick(client)

    s = client.state
    assert s.connected is True
    assert s.state == "RUNNING"
    assert s.gcode_file == "benchy.gcode"
    assert s.subtask_name == "benchy"
    assert s.progress == 50.0
    assert s.layer_num == 50 and s.total_layers == 100
    assert s.temperatures["nozzle"] == 200.0 and s.temperatures["nozzle_target"] == 210.0
    assert s.temperatures["bed"] == 59.9 and s.temperatures["bed_target"] == 60.0
    assert s.temperatures["nozzle_heating"] is True
    assert s.cooling_fan_speed == 50
    assert s.speed_level == 3
    assert s.firmware_version == "v0.11.0-creality"
    assert s.ftp_capable is False
    # 50% after 1200s of printing -> 1200s left -> 20 minutes
    assert s.remaining_time == 20


def test_remaining_time_uses_slicer_estimate_early_in_the_print():
    fake = FakeMoonraker()
    fake.set_print("printing", progress=0.01, duration=30.0)
    client = make_client(fake)
    tick(client)
    # 1% done, only 30s elapsed -> falls back to estimated_time * 0.99 = 3564s -> 60 min
    assert client.state.remaining_time == 60


def test_layer_is_derived_from_z_when_slicer_sends_no_layer_info():
    fake = FakeMoonraker()
    fake.set_print("printing", progress=0.3, duration=500.0)
    fake.status["gcode_move"]["gcode_position"] = [10, 10, 1.0, 0]  # (1.0-0.2)/0.2 + 1 = 5
    client = make_client(fake)
    tick(client)
    assert client.state.layer_num == 5
    assert client.state.total_layers == 100  # from file metadata


@pytest.mark.parametrize(
    ("klipper", "expected"),
    [
        ("standby", "IDLE"),
        ("printing", "RUNNING"),
        ("paused", "PAUSE"),
        ("complete", "FINISH"),
        ("error", "FAILED"),
        ("cancelled", "IDLE"),
    ],
)
def test_state_mapping(klipper, expected):
    fake = FakeMoonraker()
    fake.set_print(klipper, "x.gcode" if klipper != "standby" else "")
    client = make_client(fake)
    tick(client)
    assert client.state.state == expected


def test_idle_has_no_file_and_zero_progress():
    fake = FakeMoonraker()
    client = make_client(fake)
    tick(client)
    assert client.state.gcode_file is None
    assert client.state.progress == 0.0
    assert client.state.remaining_time == 0


def test_print_start_and_completion_callbacks():
    fake = FakeMoonraker()
    starts, completes, finish_photos = [], [], []
    client = make_client(
        fake,
        on_print_start=starts.append,
        on_print_complete=completes.append,
        on_finish_photo_moment=finish_photos.append,
    )
    tick(client)  # idle first report
    assert not starts

    fake.set_print("printing", "benchy.gcode", progress=0.2, duration=100.0)
    tick(client)
    assert len(starts) == 1
    assert starts[0]["filename"] == "benchy.gcode"
    assert starts[0]["subtask_name"] == "benchy"
    assert starts[0]["ams_mapping"] is None

    tick(client)  # still printing -> no second start
    assert len(starts) == 1

    fake.set_print("complete", "benchy.gcode", progress=1.0, duration=3600.0)
    tick(client)
    assert len(completes) == 1
    assert completes[0]["status"] == "completed"
    assert completes[0]["filename"] == "benchy.gcode"
    assert completes[0]["last_progress"] == 20.0
    assert len(finish_photos) == 1
    assert client.state.progress == 100.0

    tick(client)  # still FINISH -> no duplicate completion
    assert len(completes) == 1


def test_pause_does_not_complete_or_restart_the_print():
    fake = FakeMoonraker()
    starts, completes = [], []
    client = make_client(fake, on_print_start=starts.append, on_print_complete=completes.append)
    tick(client)
    fake.set_print("printing", progress=0.1, duration=60.0)
    tick(client)
    fake.set_print("paused", progress=0.1, duration=60.0)
    tick(client)
    fake.set_print("printing", progress=0.1, duration=61.0)
    tick(client)
    assert len(starts) == 1
    assert completes == []


@pytest.mark.parametrize(
    ("final", "expected"),
    [("cancelled", "aborted"), ("error", "failed"), ("complete", "completed")],
)
def test_completion_status_per_end_state(final, expected):
    fake = FakeMoonraker()
    completes = []
    client = make_client(fake, on_print_complete=completes.append)
    tick(client)
    fake.set_print("printing", progress=0.3, duration=100.0)
    tick(client)
    fake.set_print(final, progress=0.3, duration=100.0)
    tick(client)
    assert [c["status"] for c in completes] == [expected]


def test_restart_during_a_print_reports_running_observed_not_start():
    fake = FakeMoonraker()
    fake.set_print("printing", "benchy.gcode", progress=0.4, duration=900.0)
    starts, observed, completes = [], [], []
    client = make_client(
        fake,
        on_print_start=starts.append,
        on_print_running_observed=observed.append,
        on_print_complete=completes.append,
    )
    tick(client)
    assert starts == []
    assert len(observed) == 1

    fake.set_print("complete", "benchy.gcode", progress=1.0, duration=2000.0)
    tick(client)
    assert [c["status"] for c in completes] == ["completed"]


def test_stale_finish_on_first_report_does_not_trigger_completion():
    fake = FakeMoonraker()
    fake.set_print("complete", "old.gcode", progress=1.0, duration=10.0)
    completes = []
    client = make_client(fake, on_print_complete=completes.append)
    tick(client)
    assert completes == []


def test_klipper_shutdown_during_print_is_a_failure():
    fake = FakeMoonraker()
    completes = []
    client = make_client(fake, on_print_complete=completes.append)
    tick(client)
    fake.set_print("printing", progress=0.5, duration=500.0)
    tick(client)
    fake.status["webhooks"] = {"state": "shutdown", "state_message": "MCU 'mcu' shutdown"}
    tick(client)
    assert [c["status"] for c in completes] == ["failed"]


def test_layer_and_progress_callbacks():
    fake = FakeMoonraker()
    layers, progress = [], []
    client = make_client(fake, on_layer_change=layers.append, on_print_progress=progress.append)
    tick(client)
    fake.set_print("printing", progress=0.10, duration=100.0, current_layer=3, total_layer=100)
    tick(client)
    fake.set_print("printing", progress=0.11, duration=110.0, current_layer=4, total_layer=100)
    tick(client)
    assert layers == [3, 4]
    assert progress == [11]


def test_bed_temperature_callback_fires_every_poll():
    fake = FakeMoonraker()
    seen = []
    client = make_client(fake, on_bed_temp_update=seen.append)
    tick(client)
    tick(client)
    assert seen == [23.1, 23.1]


# ---------------------------------------------------------------- connection errors


def test_unreachable_printer_reports_unreachable():
    def boom(request):
        raise httpx.ConnectError("no route to host")

    client = MoonrakerClient("10.0.0.9", "X")
    client._http = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(_ConnectError) as exc:
        client._handshake()
    assert exc.value.slug == "unreachable"


def test_non_moonraker_host_is_detected():
    client = MoonrakerClient("10.0.0.9", "X")
    client._http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>router</html>"))
    )
    with pytest.raises(_ConnectError) as exc:
        client._handshake()
    assert exc.value.slug == ERROR_NOT_MOONRAKER


def test_unauthorized_is_reported():
    fake = FakeMoonraker()
    fake.status_code = 401
    client = make_client(fake)
    with pytest.raises(_ConnectError) as exc:
        client._handshake()
    assert exc.value.slug == ERROR_UNAUTHORIZED


def test_klipper_not_ready_is_reported():
    fake = FakeMoonraker()
    fake.klipper_state = "shutdown"
    client = make_client(fake)
    with pytest.raises(_ConnectError) as exc:
        client._handshake()
    assert exc.value.slug == "klipper_not_ready"


def test_repeated_failures_flip_to_disconnected_and_recover():
    fake = FakeMoonraker()
    changes = []
    client = make_client(fake, on_state_change=lambda s: changes.append(s.connected))
    tick(client)
    assert client.state.connected
    for _ in range(client.FAILURES_BEFORE_DISCONNECT):
        client._register_failure("unreachable", "x")
    assert client.state.connected is False
    assert client._handshake_done is False  # objects are re-discovered after an outage
    tick(client)
    assert client.state.connected is True


def test_check_staleness_marks_disconnected():
    fake = FakeMoonraker()
    client = make_client(fake)
    tick(client)
    assert client.check_staleness() is True
    client._last_message_time -= client.STALE_TIMEOUT + 1
    assert client.check_staleness() is False


def test_power_off_presumption_is_undone_by_the_next_poll():
    fake = FakeMoonraker()
    client = make_client(fake)
    tick(client)
    assert client.mark_power_off() is True
    assert client.state.connected is False and client.state.state == "unknown"
    tick(client)
    assert client.state.connected is True and client.state.state == "IDLE"


# ---------------------------------------------------------------- commands


def test_commands_hit_the_documented_moonraker_endpoints():
    fake = FakeMoonraker()
    client = make_client(fake)
    tick(client)

    assert client.pause_print()
    assert client.resume_print()
    assert client.stop_print()
    assert client.start_print("my dir/a b.gcode", 1, timelapse=False, use_ams=True)
    assert client.send_gcode("G28")
    assert client.set_bed_temperature(60)
    assert client.set_nozzle_temperature(210)
    assert client.set_print_speed(3)
    assert client.set_fan_speed(1, 300)
    assert client.move_axis("z", -5.0, 600)
    assert client.disable_motors()

    paths = [p for p, _, _ in fake.posts]
    assert paths[:3] == ["/printer/print/pause", "/printer/print/resume", "/printer/print/cancel"]
    start = fake.posts[3]
    assert start[0] == "/printer/print/start" and "filename=my%20dir/a%20b.gcode" in start[2]
    scripts = [b["script"] for p, b, _ in fake.posts if p == "/printer/gcode/script"]
    assert scripts == [
        "G28",
        "M140 S60",
        "M104 S210",
        "M220 S124",
        "M106 S255",  # clamped
        "G91\nG1 Z-5.00 F600\nG90",
        "M84",
    ]


def test_unsupported_bambu_features_return_false_instead_of_raising():
    fake = FakeMoonraker()
    client = make_client(fake)
    tick(client)
    assert client.set_chamber_temperature(40) is False
    assert client.set_airduct_mode("heating") is False
    assert client.set_fan_speed(2, 100) is False  # no aux fan on stock Klipper
    assert client.set_print_speed(9) is False
    assert client.move_axis("E", 1) is False
    assert fake.posts == []


def test_commands_refuse_when_not_connected():
    client = MoonrakerClient("10.0.0.9", "X")
    assert client.send_gcode("G28") is False
    assert client.pause_print() is False


def test_chamber_light_uses_discovered_output_pin():
    fake = FakeMoonraker()
    client = make_client(fake)
    tick(client)
    assert client.set_chamber_light(True) is True
    assert client.state.chamber_light is True
    assert fake.posts[-1][1] == {"script": "SET_PIN PIN=LED VALUE=1"}


def image_server(*serving: str):
    """A transport that answers 200 image/jpeg for the given URL prefixes and 404 otherwise."""

    def handler(request: httpx.Request) -> httpx.Response:
        if any(str(request.url).startswith(u) for u in serving):
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8jpeg")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_camera_is_found_where_it_really_answers_not_where_it_is_advertised():
    """Moonraker advertises a relative path; the image lives behind Fluidd's port 4408."""
    fake = FakeMoonraker()
    client = make_client(fake)
    client._probe_http = httpx.Client(transport=image_server("http://192.168.1.50:4408/webcam/?action=snapshot"))

    found = client.find_camera()

    assert found["verified"] is True
    assert found["stream_url"] == "http://192.168.1.50:4408/webcam/?action=stream"
    assert found["snapshot_url"] == "http://192.168.1.50:4408/webcam/?action=snapshot"


def test_camera_falls_back_to_the_mjpg_streamer_port_when_moonraker_advertises_nothing():
    fake = FakeMoonraker()
    client = make_client(fake)
    client._advertised_webcams = lambda: []
    client._probe_http = httpx.Client(transport=image_server("http://192.168.1.50:8080/?action=snapshot"))

    found = client.find_camera()

    assert found["verified"] is True
    assert found["stream_url"] == "http://192.168.1.50:8080/?action=stream"


def test_a_working_configured_camera_is_kept_over_other_candidates():
    fake = FakeMoonraker()
    client = make_client(fake)
    client._probe_http = httpx.Client(transport=image_server("http://cam.lan/snap", "http://192.168.1.50:8080/"))

    found = client.find_camera("http://cam.lan/stream", "http://cam.lan/snap")

    assert found["stream_url"] == "http://cam.lan/stream"
    assert found["verified"] is True


def test_unreachable_camera_returns_the_advertised_pair_unverified_and_lists_what_was_tried():
    fake = FakeMoonraker()
    client = make_client(fake)
    client._probe_http = httpx.Client(transport=image_server())  # nothing answers

    found = client.find_camera()

    assert found["verified"] is False
    assert found["stream_url"] == "http://192.168.1.50/webcam/?action=stream"
    assert "http://192.168.1.50:8080/?action=snapshot" in found["tried"]


def test_no_camera_anywhere_gives_none():
    fake = FakeMoonraker()
    client = make_client(fake)
    client._advertised_webcams = lambda: []
    client._probe_http = httpx.Client(transport=image_server())
    assert client.get_webcam_urls() is None
    assert client.find_camera()["stream_url"] is None


def test_a_mjpeg_stream_content_type_counts_as_a_camera():
    fake = FakeMoonraker()
    client = make_client(fake)

    def handler(request):
        return httpx.Response(200, headers={"content-type": "multipart/x-mixed-replace;boundary=x"}, content=b"--x")

    client._probe_http = httpx.Client(transport=httpx.MockTransport(handler))
    assert client._responds_with_image("http://192.168.1.50:8080/?action=stream") is True


def test_debug_log_records_traffic_when_enabled():
    fake = FakeMoonraker()
    client = make_client(fake)
    client.enable_logging(True)
    tick(client)
    client.send_gcode("M105")
    directions = [e.direction for e in client.get_logs()]
    assert directions == ["in", "out"]
    client.clear_logs()
    assert client.get_logs() == []


# ---------------------------------------------------------------- helpers / glue


def test_slug_name():
    assert _slug_name("/gcodes/sub/Benchy_0.2.gcode") == "Benchy_0.2"
    assert _slug_name("a.3mf") == "a"
    assert _slug_name(None) is None
    assert _slug_name("") is None


def test_speed_percent_to_level():
    assert [speed_percent_to_level(p) for p in (50, 100, 124, 166)] == [1, 2, 3, 4]


def test_idle_printer_shows_no_layer_count():
    fake = FakeMoonraker()
    fake.status["print_stats"]["info"] = {"total_layer": 120}
    client = make_client(fake)
    tick(client)
    assert client.state.state == "IDLE"
    assert client.state.total_layers == 0


def test_moonraker_printers_report_no_external_storage_to_scan():
    from backend.app.services.print_storage import external_storage_present

    assert external_storage_present(SimpleNamespace(ftp_capable=False)) is False
    # Bambu printers keep the old behaviour: present unless the slot was reported empty.
    assert external_storage_present(SimpleNamespace()) is True
    assert external_storage_present(SimpleNamespace(sdcard_reported=True, sdcard=False)) is False
    assert external_storage_present(None) is True


def test_manager_has_ftp_is_false_only_for_moonraker_clients():
    from backend.app.services.printer_manager import PrinterManager

    manager = PrinterManager()
    manager._clients[1] = MoonrakerClient("10.0.0.1", "X")
    manager._clients[2] = MagicMock(spec=["state"])  # a Bambu-like client: no printer_type attribute
    assert manager.has_ftp(1) is False
    assert manager.has_ftp(2) is True
    assert manager.has_ftp(99) is True  # unknown / disconnected reads as Bambu


def test_non_bambu_printers_skip_the_ftps_sweep():
    verdict = print_file_reachable_over_ftp(SimpleNamespace(ftp_capable=False))
    assert verdict.reachable is False
    assert verdict.reason == REASON_NO_FTP_SERVICE
    # Bambu printers (and unknown states) are untouched.
    assert print_file_reachable_over_ftp(SimpleNamespace()).reachable is True
    assert print_file_reachable_over_ftp(None).reachable is True


def test_schema_creality_needs_no_serial_or_access_code():
    p = PrinterCreate(name="Ender", ip_address="192.168.1.50", printer_type="creality")
    assert p.serial_number == "CREALITY-192.168.1.50"
    assert p.access_code == ""
    assert p.api_port is None


def test_schema_bambu_still_requires_access_code_and_serial():
    with pytest.raises(ValueError):
        PrinterCreate(name="X1", ip_address="192.168.1.2", serial_number="01P00A000000000")
    with pytest.raises(ValueError):
        PrinterCreate(name="X1", ip_address="192.168.1.2", access_code="12345678")
    ok = PrinterCreate(name="X1", ip_address="192.168.1.2", serial_number="01p00a000000000", access_code="12345678")
    assert ok.printer_type == "bambu" and ok.serial_number == "01P00A000000000"


def test_schema_rejects_unknown_printer_type_and_bad_port():
    with pytest.raises(ValueError):
        PrinterCreate(name="x", ip_address="1.2.3.4", printer_type="prusa")
    with pytest.raises(ValueError):
        PrinterCreate(name="x", ip_address="1.2.3.4", printer_type="creality", api_port=70000)


@pytest.mark.asyncio
async def test_manager_builds_a_moonraker_client_for_creality_printers():
    from backend.app.services.printer_manager import PrinterManager

    manager = PrinterManager()
    printer = SimpleNamespace(
        id=7,
        name="Ender 3 V3 KE",
        serial_number="CREALITY-192.168.1.50",
        ip_address="192.168.1.50",
        access_code="",
        model="Ender-3 V3 KE",
        printer_type="creality",
        api_port=4408,
    )
    with patch.object(MoonrakerClient, "connect", MagicMock()) as connect, patch("asyncio.sleep", new=_instant):
        await manager.connect_printer(printer)

    client = manager.get_client(7)
    assert isinstance(client, MoonrakerClient)
    assert client.port == 4408
    connect.assert_called_once()
    assert manager.get_model(7) == "Ender-3 V3 KE"
    assert manager.get_status(7) is client.state
    manager.disconnect_printer(7)
    assert manager.get_client(7) is None


@pytest.mark.asyncio
async def test_manager_probe_returns_reason_and_webcam():
    from backend.app.services.printer_manager import PrinterManager

    fake = FakeMoonraker()
    real_init = MoonrakerClient.__init__

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        self._http = None

    def connect(self, loop=None):
        # drive one synchronous handshake+poll instead of starting the thread
        self._http = httpx.Client(transport=httpx.MockTransport(fake.handler))
        self._probe_http = httpx.Client(transport=image_server())  # no camera answers
        tick(self)

    with patch.object(MoonrakerClient, "__init__", init), patch.object(MoonrakerClient, "connect", connect):
        result = await PrinterManager().test_connection("192.168.1.50", "", "", printer_type="creality")

    assert result["success"] is True
    assert result["state"] == "IDLE"
    assert result["firmware_version"] == "v0.11.0-creality"
    assert result["webcam"]["stream_url"] == "http://192.168.1.50/webcam/?action=stream"


@pytest.mark.asyncio
async def test_manager_probe_failure_carries_reason():
    from backend.app.services.printer_manager import PrinterManager

    manager = PrinterManager()
    manager.PROBE_TIMEOUT_SECONDS = 0.2
    manager.PROBE_POLL_INTERVAL_SECONDS = 0.05

    def connect(self, loop=None):
        self.last_connect_error = "unreachable"

    with patch.object(MoonrakerClient, "connect", connect):
        result = await manager.test_connection("192.168.1.99", "", "", printer_type="creality")

    assert result["success"] is False
    assert result["reason"] == "unreachable"


async def _instant(*_a, **_kw):
    return None


# ---------------------------------------------------------------- fans


def test_discover_fans_stock_klipper_layout():
    roles = discover_fans(["extruder", "fan", "heater_fan hotend_fan", "controller_fan board_fan", "output_pin LED"])
    assert roles["part"] == FanRef("fan", "fan", "")
    assert roles["heatbreak"].obj == "heater_fan hotend_fan"
    assert roles["chamber"].obj == "controller_fan board_fan"
    assert "aux" not in roles


@pytest.mark.parametrize("kind", ["output_pin", "fan_generic"])
def test_discover_fans_creality_style_fan0_fan1_fan2(kind):
    roles = discover_fans(
        ["extruder", f"{kind} fan2", f"{kind} fan0", f"{kind} fan1", "output_pin LED", "output_pin beeper"]
    )
    assert roles["part"].obj == f"{kind} fan0"
    assert roles["aux"].obj == f"{kind} fan1"
    assert roles["chamber"].obj == f"{kind} fan2"


def test_discover_fans_ignores_non_fan_output_pins_and_missing_roles():
    assert discover_fans(["output_pin LED", "output_pin beeper", "extruder"]) == {}


def test_discover_fans_never_assigns_one_object_to_two_roles():
    roles = discover_fans(["fan", "fan_generic fan0"])
    assert roles["part"].obj == "fan"
    assert len({r.obj for r in roles.values()}) == len(roles)


def test_fan_percent_handles_both_object_shapes_and_scales():
    assert fan_percent(FanRef("fan", "fan", ""), {"speed": 0.4}) == 40
    assert fan_percent(FanRef("output_pin", "output_pin fan0", "fan0"), {"value": 1.0}) == 100
    assert fan_percent(FanRef("output_pin", "output_pin fan0", "fan0"), {"value": 128}) == 50  # 0-255 scale
    assert fan_percent(FanRef("fan", "fan", ""), {}) is None
    assert fan_percent(FanRef("fan", "fan", ""), {"speed": None}) is None


def make_creality_fan_fake(kind="output_pin"):
    fake = FakeMoonraker()
    fake.objects = [o for o in OBJECTS if o != "fan"] + [f"{kind} fan0", f"{kind} fan1", f"{kind} fan2"]
    del fake.status["fan"]
    value_key = "value" if kind == "output_pin" else "speed"
    fake.status[f"{kind} fan0"] = {value_key: 1.0}
    fake.status[f"{kind} fan1"] = {value_key: 0.5}
    fake.status[f"{kind} fan2"] = {value_key: 0.0}
    return fake


def test_fans_without_a_plain_fan_object_are_read_and_stay_current():
    """Regression: a printer with fan0/fan1/fan2 instead of [fan] showed fans stuck at 0."""
    fake = make_creality_fan_fake()
    client = make_client(fake)
    tick(client)

    s = client.state
    assert (s.cooling_fan_speed, s.big_fan1_speed, s.big_fan2_speed) == (100, 50, 0)
    assert s.raw_data["fans"] == {"part": "output_pin fan0", "aux": "output_pin fan1", "chamber": "output_pin fan2"}

    fake.status["output_pin fan0"]["value"] = 0.3
    fake.status["output_pin fan2"]["value"] = 1.0
    tick(client)
    assert (s.cooling_fan_speed, s.big_fan1_speed, s.big_fan2_speed) == (30, 50, 100)


def test_fan_commands_follow_the_object_kind_found():
    fake = make_creality_fan_fake("fan_generic")
    client = make_client(fake)
    tick(client)
    assert client.set_fan_speed(1, 255) and client.set_fan_speed(2, 128) and client.set_fan_speed(3, 0)
    assert [b["script"] for p, b, _ in fake.posts] == [
        "SET_FAN_SPEED FAN=fan0 SPEED=1.0",
        "SET_FAN_SPEED FAN=fan1 SPEED=0.502",
        "SET_FAN_SPEED FAN=fan2 SPEED=0.0",
    ]

    pins = make_creality_fan_fake("output_pin")
    c2 = make_client(pins)
    tick(c2)
    assert c2.set_fan_speed(2, 255)
    assert pins.posts[-1][1] == {"script": "SET_PIN PIN=fan1 VALUE=1.0"}


def test_a_printer_with_no_fans_reports_none_and_refuses_fan_commands():
    fake = FakeMoonraker()
    fake.objects = [o for o in OBJECTS if o != "fan"]
    del fake.status["fan"]
    client = make_client(fake)
    tick(client)
    assert client.state.cooling_fan_speed is None
    assert client.set_fan_speed(1, 100) is False
    assert fake.posts == []


# ---------------------------------------------------------------- K2 series (heated chamber)

K2_OBJECTS = [
    "print_stats",
    "virtual_sdcard",
    "extruder",
    "heater_bed",
    "fan",
    "gcode_move",
    "webhooks",
    "temperature_sensor chamber_temp",
    "heater_generic chamber_heater",
    "temperature_fan chamber_fan",
    "output_pin ptc_power",
    "box",
]


def make_k2_fake():
    fake = FakeMoonraker()
    fake.objects = list(K2_OBJECTS)
    fake.status["temperature_sensor chamber_temp"] = {"temperature": 31.0}
    fake.status["heater_generic chamber_heater"] = {"temperature": 38.4, "target": 55.0}
    fake.status["temperature_fan chamber_fan"] = {"speed": 0.7, "target": 0.0}
    return fake


def test_k2_chamber_heater_is_preferred_over_the_bare_sensor_and_read_with_its_target():
    client = make_client(make_k2_fake())
    tick(client)
    t = client.state.temperatures
    assert (t["chamber"], t["chamber_target"]) == (38.4, 55.0)
    assert t["chamber_heating"] is True


def test_k2_chamber_temperature_command_targets_the_chamber_heater_only():
    fake = make_k2_fake()
    client = make_client(fake)
    tick(client)
    assert client.set_chamber_temperature(60) is True
    assert client.set_chamber_temperature(0) is True
    assert [b["script"] for p, b, _ in fake.posts] == [
        "SET_HEATER_TEMPERATURE HEATER=chamber_heater TARGET=60",
        "SET_HEATER_TEMPERATURE HEATER=chamber_heater TARGET=0",
    ]
    # the PTC power pin and the CFS "box" object are never touched
    assert all("ptc_power" not in b["script"] for _, b, _ in fake.posts)


def test_k2_chamber_fan_is_a_temperature_fan_and_is_read_but_not_driven():
    fake = make_k2_fake()
    client = make_client(fake)
    tick(client)
    assert client.state.big_fan2_speed == 70
    assert client.set_fan_speed(3, 255) is False  # it regulates itself from the chamber temperature
    assert fake.posts == []


def test_a_chamber_with_only_a_sensor_is_readable_but_not_controllable():
    fake = FakeMoonraker()
    fake.objects = [*OBJECTS, "temperature_sensor chamber_temp"]
    fake.status["temperature_sensor chamber_temp"] = {"temperature": 29.5}
    client = make_client(fake)
    tick(client)
    assert client.state.temperatures["chamber"] == 29.5
    assert client.set_chamber_temperature(40) is False


def test_k2_models_are_known_to_have_a_heated_chamber():
    from backend.app.services.printer_manager import supports_chamber_heater, supports_chamber_temp

    for model in ("K2 Plus", "K2 Pro", " k2 plus "):
        assert supports_chamber_temp(model) is True
        assert supports_chamber_heater(model) is True
    assert supports_chamber_heater("Ender-3 V3 KE") is False
    assert supports_chamber_temp("Ender-3 V3 KE") is False


# ---------------------------------------------------------------- upload and print


def test_upload_and_print_sends_the_file_to_the_gcodes_root_and_asks_for_it_to_start(tmp_path):
    fake = FakeMoonraker()
    client = make_client(fake)
    tick(client)
    gcode = tmp_path / "local name.gcode"
    gcode.write_text("G28\nG1 X10\n")

    assert client.upload_and_print(str(gcode), "benchy.gcode") is True

    content_type, body = fake.uploads[0]
    assert content_type.startswith("multipart/form-data")
    assert b'name="root"' in body and b"gcodes" in body
    assert b'name="print"' in body and b"true" in body
    assert b'filename="benchy.gcode"' in body and b"G1 X10" in body
    assert client.last_error is None


def test_upload_and_print_can_upload_without_starting(tmp_path):
    fake = FakeMoonraker()
    fake.upload_result = (201, {"item": {}, "print_started": False})
    client = make_client(fake)
    tick(client)
    gcode = tmp_path / "a.gcode"
    gcode.write_text("G28")
    assert client.upload_and_print(str(gcode), start=False) is True
    assert b"false" in fake.uploads[0][1]


def test_upload_that_does_not_start_is_reported_as_a_failure(tmp_path):
    fake = FakeMoonraker()
    fake.upload_result = (201, {"item": {}, "print_started": False})
    client = make_client(fake)
    tick(client)
    gcode = tmp_path / "a.gcode"
    gcode.write_text("G28")

    assert client.upload_and_print(str(gcode), "a.gcode") is False
    assert "did not start printing" in client.last_error


def test_upload_refused_by_the_printer_carries_its_reason(tmp_path):
    fake = FakeMoonraker()
    fake.upload_result = (409, {"error": {"code": 409, "message": "File is loaded, upload not permitted"}})
    client = make_client(fake)
    tick(client)
    gcode = tmp_path / "a.gcode"
    gcode.write_text("G28")

    assert client.upload_and_print(str(gcode), "a.gcode") is False
    assert "HTTP 409" in client.last_error and "upload not permitted" in client.last_error


def test_upload_when_disconnected_or_file_missing_fails_with_a_reason(tmp_path):
    client = MoonrakerClient("10.0.0.9", "X")
    assert client.upload_and_print(str(tmp_path / "a.gcode")) is False
    assert client.last_error == "Printer not connected"

    fake = FakeMoonraker()
    connected = make_client(fake)
    tick(connected)
    assert connected.upload_and_print(str(tmp_path / "missing.gcode")) is False
    assert "Could not read the file" in connected.last_error


# ---------------------------------------------------------------- CFS (K2 multi-filament module)

# 40-char Creality RFID record: date(5) supplier(4) batch(2) material(6) colour(7) length(4) serial(6) reserved(6)
TAGGED = "24031" + "0276" + "01" + "101001" + "0C12E1F" + "0330" + "000001" + "000000"

BOX_STOCK = {
    "state": "connect",
    "filament": 1,
    "T1": {
        "state": "connect",
        "temperature": "27",
        "dry_and_humidity": "48",
        "version": "1.1.3",
        "sn": "CFS-0001",
        "vender": [TAGGED, "unknown", "none", "none"],
        "color_value": ["0C12E1F", "0FFFFFF", "-1", "-1"],
        "material_type": ["101001", "100001", "-1", "-1"],
        "remain_len": ["99", "57", "-1", "-1"],
    },
    "T2": {"state": "None", "vender": ["None"] * 4, "color_value": ["-1"] * 4, "material_type": ["-1"] * 4},
}

BOX_FLAT = {
    "api_version": 1,
    "loaded_slot": 0,
    "temp_c": 31.0,
    "humidity_pct": 40,
    "slots": [
        {"index": 0, "name": "Creality PETG", "material": "PETG", "color": "#FF0000", "present": True, "loaded": True},
        {"index": 1, "name": "PLA", "material": "PLA", "color": "#00FF00", "present": True, "loaded": False},
        {"index": 2, "present": False, "material": "None", "color": "None"},
        {"index": 3, "present": False},
        {"index": 4, "name": "TPU", "material": "TPU", "color": "#0000FF", "present": True, "rfid_percent": 62},
        {"index": 16, "external": True, "present": True, "material": "PLA", "color": "#ABCDEF"},
    ],
}


def test_stock_cfs_becomes_ams_units_with_colour_material_and_remaining():
    units = parse_cfs_stock(BOX_STOCK)

    assert [u["id"] for u in units] == [0]  # the disconnected T2 is not shown
    unit = units[0]
    assert (unit["humidity_raw"], unit["temp"], unit["sn"], unit["module_type"]) == (48.0, 27.0, "CFS-0001", "cfs")
    a, b, c, d = unit["tray"]
    assert (a["tray_color"], a["tray_type"], a["tray_sub_brands"]) == ("C12E1FFF", "PLA", "Hyper PLA")
    assert a["remain"] == 30  # 99 m left of the 330 m the RFID record states
    assert a["exists"] is True and a["state"] == 10 and a["cfs_slot"] == "T1A"
    # an untagged spool: colour and type are still read, only the percentage is unknowable
    assert (b["tray_color"], b["tray_type"], b["remain"], b["exists"]) == ("FFFFFFFF", "PLA", -1, True)
    # empty bays
    assert (c["exists"], c["tray_color"], c["tray_type"], c["state"]) == (False, None, None, 9)
    assert d["exists"] is False


def test_an_unknown_material_code_is_shown_by_its_number_not_guessed():
    box = {"T1": {**BOX_STOCK["T1"], "material_type": ["199999", "100001", "-1", "-1"]}}
    tray = parse_cfs_stock(box)[0]["tray"][0]
    assert tray["tray_type"] is None
    assert tray["tray_sub_brands"] == "Material 199999"


def test_flat_cfs_groups_slots_into_units_of_four_and_marks_the_loaded_one():
    units, loaded = parse_cfs_flat(BOX_FLAT)

    assert loaded == 0
    assert [u["id"] for u in units] == [0, 1]
    first = units[0]["tray"]
    assert [t["id"] for t in first] == [0, 1, 2, 3]  # a unit always has four bays
    assert [t["exists"] for t in first] == [True, True, False, False]
    assert (first[0]["tray_color"], first[0]["tray_type"], first[0]["state"]) == ("FF0000FF", "PETG", 11)
    assert first[1]["state"] == 10
    assert units[1]["tray"][0]["remain"] == 62
    assert units[0]["humidity_raw"] == 40.0
    # the external holder (index 16) is not a CFS slot
    assert all(t["id"] != 16 for u in units for t in u["tray"])


def test_polling_a_k2_publishes_the_cfs_through_raw_data_ams():
    fake = FakeMoonraker()
    fake.objects = [*OBJECTS, "box"]
    fake.status["box"] = BOX_STOCK
    client = make_client(fake)
    tick(client)

    ams = client.state.raw_data["ams"]
    assert len(ams) == 1 and ams[0]["tray"][0]["tray_sub_brands"] == "Hyper PLA"

    # the CFS disappearing (a printer without one) leaves no stale units behind
    fake.status["box"] = {"state": "None"}
    tick(client)
    assert "ams" not in client.state.raw_data


def test_a_printer_without_a_box_object_never_shows_ams():
    client = make_client(FakeMoonraker())
    tick(client)
    assert "ams" not in client.state.raw_data


def test_flat_cfs_sets_the_loaded_tray():
    fake = FakeMoonraker()
    fake.objects = [*OBJECTS, "box"]
    fake.status["box"] = BOX_FLAT
    client = make_client(fake)
    tick(client)
    assert client.state.tray_now == 0


def test_the_bambu_ams_controls_say_no_instead_of_crashing():
    client = make_client(FakeMoonraker())
    tick(client)
    assert client.ams_load_filament(0) is False
    assert client.ams_unload_filament() is False
    assert client.ams_set_filament_setting(0, 0, "PLA", "FFFFFF") is False
    assert client.extrusion_cali_sel(0, 0, 0, 0) is False
    assert client.register_assignment_verification(0, 0, {}) is None


# ---------------------------------------------------------------- printer errors


def test_a_healthy_printer_reports_no_error():
    client = make_client(FakeMoonraker())
    tick(client)
    assert client.state.printer_error is None


def test_a_failed_job_surfaces_klipper_s_own_message_and_reaches_the_completion_payload():
    fake = FakeMoonraker()
    completes = []
    client = make_client(fake, on_print_complete=completes.append)
    tick(client)
    fake.set_print("printing", progress=0.4, duration=600.0)
    tick(client)
    fake.set_print("error", progress=0.4, duration=600.0)
    fake.status["print_stats"]["message"] = "Heater extruder not heating at expected rate\nSee docs"
    tick(client)

    assert client.state.printer_error == "Heater extruder not heating at expected rate"
    assert completes[0]["status"] == "failed"
    assert completes[0]["printer_error"] == "Heater extruder not heating at expected rate"


def test_a_halted_klipper_reports_its_shutdown_message():
    fake = FakeMoonraker()
    client = make_client(fake)
    tick(client)
    fake.status["webhooks"] = {"state": "shutdown", "state_message": "Lost communication with MCU 'mcu'"}
    tick(client)
    assert client.state.printer_error == "Lost communication with MCU 'mcu'"


def test_a_paused_print_with_no_filament_at_the_sensor_is_a_runout():
    fake = FakeMoonraker()
    fake.objects = [*OBJECTS, "filament_switch_sensor runout"]
    fake.status["filament_switch_sensor runout"] = {"enabled": True, "filament_detected": True}
    client = make_client(fake)
    tick(client)
    fake.set_print("printing", progress=0.3, duration=300.0)
    tick(client)
    assert client.state.printer_error is None

    fake.set_print("paused", progress=0.3, duration=300.0)
    fake.status["filament_switch_sensor runout"]["filament_detected"] = False
    tick(client)
    assert client.state.printer_error == "Filament runout (runout)"

    # a pause the user asked for, with filament present, is not an error
    fake.status["filament_switch_sensor runout"]["filament_detected"] = True
    tick(client)
    assert client.state.printer_error is None


def test_a_disabled_runout_sensor_is_ignored():
    fake = FakeMoonraker()
    fake.objects = [*OBJECTS, "filament_switch_sensor runout"]
    fake.status["filament_switch_sensor runout"] = {"enabled": False, "filament_detected": False}
    client = make_client(fake)
    tick(client)
    fake.set_print("paused", progress=0.3, duration=300.0)
    tick(client)
    assert client.state.printer_error is None


def test_a_cleared_error_goes_away_when_the_printer_recovers():
    fake = FakeMoonraker()
    client = make_client(fake)
    tick(client)
    fake.status["webhooks"] = {"state": "shutdown", "state_message": "MCU shutdown"}
    tick(client)
    assert client.state.printer_error
    fake.status["webhooks"] = {"state": "ready"}
    tick(client)
    assert client.state.printer_error is None


# ---------------------------------------------------------------- downloads (archive enrichment)


def test_file_metadata_is_exposed():
    fake = FakeMoonraker()
    client = make_client(fake)
    tick(client)
    assert client.get_file_metadata("benchy.gcode")["layer_count"] == 100


def test_download_gcode_streams_the_file_to_disk(tmp_path):
    fake = FakeMoonraker()
    fake.files["sub dir/benchy.gcode"] = b"G28\n" * 1000
    client = make_client(fake)
    tick(client)
    dest = tmp_path / "out.gcode"

    assert client.download_gcode("sub dir/benchy.gcode", dest) == 4000
    assert dest.read_bytes() == b"G28\n" * 1000


def test_download_gcode_returns_none_and_leaves_nothing_when_missing_or_too_big(tmp_path):
    fake = FakeMoonraker()
    fake.files["big.gcode"] = b"x" * 5000
    client = make_client(fake)
    tick(client)

    assert client.download_gcode("absent.gcode", tmp_path / "a.gcode") is None
    assert client.download_gcode("big.gcode", tmp_path / "b.gcode", max_bytes=1000) is None
    assert not (tmp_path / "a.gcode").exists() and not (tmp_path / "b.gcode").exists()


def test_download_thumbnail_resolves_the_path_against_the_files_folder():
    fake = FakeMoonraker()
    fake.files["models/.thumbs/benchy-300x300.png"] = b"\x89PNG-bytes"
    fake.files[".thumbs/root-300x300.png"] = b"\x89PNG-root"
    client = make_client(fake)
    tick(client)

    assert client.download_thumbnail("models/benchy.gcode", ".thumbs/benchy-300x300.png") == b"\x89PNG-bytes"
    assert client.download_thumbnail("root.gcode", ".thumbs/root-300x300.png") == b"\x89PNG-root"
    assert client.download_thumbnail("models/benchy.gcode", ".thumbs/none.png") is None


# ---------------------------------------------------------------- K2 camera through a go2rtc bridge


def _go2rtc_transport(streams: dict, serving: str):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "http://192.168.1.50:1984/api/streams":
            return httpx.Response(200, json=streams)
        if url.startswith(serving):
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8jpeg")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_a_go2rtc_bridge_gives_the_k2_a_usable_camera():
    fake = FakeMoonraker()
    client = make_client(fake)
    client._advertised_webcams = lambda: []
    client._probe_http = httpx.Client(
        transport=_go2rtc_transport({"k2plus": {"producers": []}}, "http://192.168.1.50:1984/api/frame.jpeg?src=k2plus")
    )

    found = client.find_camera()

    assert found["verified"] is True
    assert found["stream_url"] == "http://192.168.1.50:1984/api/stream.mjpeg?src=k2plus"
    assert found["snapshot_url"] == "http://192.168.1.50:1984/api/frame.jpeg?src=k2plus"


def test_a_stream_name_with_odd_characters_is_quoted():
    client = make_client(FakeMoonraker())
    client._advertised_webcams = lambda: []
    client._probe_http = httpx.Client(
        transport=_go2rtc_transport({"my cam/1": {}}, "http://192.168.1.50:1984/api/frame")
    )
    assert client.find_camera()["stream_url"] == "http://192.168.1.50:1984/api/stream.mjpeg?src=my%20cam%2F1"


def test_no_go2rtc_means_no_extra_candidates_and_no_error():
    client = make_client(FakeMoonraker())
    client._advertised_webcams = lambda: []
    client._probe_http = httpx.Client(transport=image_server())
    assert client._go2rtc_streams() == []
    assert all(":1984" not in stream for stream, _ in client._camera_candidates(None))


# ---------------------------------------------------------------- cancel objects (exclude_object)


def _exclude_fake(excluded=()):
    fake = FakeMoonraker()
    fake.objects = [*OBJECTS, "exclude_object"]
    fake.status["exclude_object"] = {
        "objects": [
            {"name": "Cube_id_0", "center": [50.0, 60.0], "polygon": []},
            {"name": "Cube_id_1", "center": [150.5, 60.0], "polygon": []},
            {"name": "My part", "center": [100.0, 120.0], "polygon": []},
        ],
        "excluded_objects": list(excluded),
        "current_object": "Cube_id_0",
    }
    return fake


def test_the_objects_of_a_running_print_are_published_for_the_skip_dialog():
    fake = _exclude_fake()
    fake.set_print("printing", progress=0.2, duration=100.0)
    client = make_client(fake)
    tick(client)

    objects = client.state.printable_objects
    assert objects[1] == {"name": "Cube", "x": 50.0, "y": 60.0}
    assert objects[2]["x"] == 150.5
    assert objects[3]["name"] == "My part"
    assert client.state.skipped_objects == []


def test_already_excluded_objects_are_reported_as_skipped():
    fake = _exclude_fake(excluded=["Cube_id_1"])
    fake.set_print("printing", progress=0.2, duration=100.0)
    client = make_client(fake)
    tick(client)
    assert client.state.skipped_objects == [2]


def test_no_objects_are_offered_when_nothing_is_printing():
    fake = _exclude_fake()
    client = make_client(fake)
    tick(client)
    assert client.state.printable_objects == {}
    assert client.state.skipped_objects == []


def test_skipping_sends_exclude_object_by_the_slicers_own_name_quoting_when_needed():
    fake = _exclude_fake()
    fake.set_print("printing", progress=0.2, duration=100.0)
    client = make_client(fake)
    tick(client)

    assert client.skip_objects([2, 3]) is True
    assert [b["script"] for p, b, _ in fake.posts] == [
        "EXCLUDE_OBJECT NAME=Cube_id_1",
        'EXCLUDE_OBJECT NAME="My part"',
    ]


def test_skipping_an_unknown_object_or_nothing_is_refused():
    fake = _exclude_fake()
    fake.set_print("printing", progress=0.2, duration=100.0)
    client = make_client(fake)
    tick(client)
    assert client.skip_objects([99]) is False
    assert client.skip_objects([]) is False
    assert fake.posts == []


def test_a_printer_without_exclude_object_offers_nothing():
    fake = FakeMoonraker()
    fake.set_print("printing", progress=0.2, duration=100.0)
    client = make_client(fake)
    tick(client)
    assert client.state.printable_objects == {}
    assert client.skip_objects([1]) is False
