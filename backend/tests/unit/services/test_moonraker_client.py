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
