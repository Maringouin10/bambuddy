"""Anycubic LAN Mode client: handshake, report mapping, lifecycle and commands."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from backend.app.services import anycubic_client as ac
from backend.app.services.anycubic_client import AnycubicClient, AnycubicError

TOKEN = "ABCDEFGHIJKLMNOP0123456789abcdef"
IP = "192.168.1.50"
CREDS = {"broker": "mqtts://192.168.1.50:9883", "username": "user", "password": "pw", "deviceId": "dev123"}


def fake_printer(ctrl_type="lan", model_id=20026, upload_reply=None, seen=None):
    seen = seen if seen is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.path == "/info" and request.method == "GET":
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "token": TOKEN,
                        "ctrlInfoUrl": f"http://{IP}:18910/ctrl",
                        "modelId": model_id,
                        "ctrlType": ctrl_type,
                        "cn": "SERIAL1",
                        "modelName": "Anycubic Kobra 3 Max",
                    }
                ),
            )
        if url.path == "/ctrl" and request.method == "POST":
            q = parse_qs(urlsplit(str(url)).query)
            seen["ctrl_query"] = q
            iv = "IVSOURCE-1234567890"
            info = ac.encrypt_info(json.dumps(CREDS).encode(), iv, TOKEN)
            return httpx.Response(200, content=json.dumps({"code": 200, "data": {"info": info, "token": iv}}))
        if url.path == "/gcode_upload":
            seen["upload"] = request
            seen["upload_body"] = request.read()
            return httpx.Response(200, content=json.dumps(upload_reply or {"code": 200, "message": "ok"}))
        return httpx.Response(404)

    return httpx.MockTransport(handler), seen


class FakeMqtt:
    def __init__(self, client_id, rc=0):
        self.client_id = client_id
        self.rc = rc
        self.published: list[tuple[str, dict]] = []
        self.subscribed: list[str] = []
        self.on_publish_hook = None

    def username_pw_set(self, user, password):
        self.creds = (user, password)

    def connect(self, host, port, keepalive):
        self.target = (host, port)

    def loop_start(self):
        self.on_connect(self, None, None, self.rc, None)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def publish(self, topic, payload):
        body = json.loads(payload)
        self.published.append((topic, body))
        if self.on_publish_hook:
            self.on_publish_hook(topic, body)

        class R:
            rc = 0

        return R()


def make_client(**kwargs):
    transport, seen = fake_printer(**{k: kwargs.pop(k) for k in list(kwargs) if k in ("ctrl_type", "upload_reply")})
    mqtt_holder = {}

    def factory(client_id):
        mqtt_holder["mqtt"] = FakeMqtt(client_id)
        return mqtt_holder["mqtt"]

    client = AnycubicClient(IP, "SERIAL1", mqtt_factory=factory, http_transport=transport, **kwargs)
    return client, mqtt_holder, seen


# ---------------------------------------------------------------- handshake


def test_signature_matches_reference():
    import hashlib

    keyed = hashlib.md5(TOKEN[:16].encode()).hexdigest()
    expected = hashlib.md5((keyed + "1700000000000" + "aB3dE9").encode()).hexdigest()
    assert ac.build_signature(TOKEN, 1700000000000, "aB3dE9") == expected
    url = ac.control_url(TOKEN, "http://x/ctrl?a=1", 1, "n", "D")
    assert url.startswith("http://x/ctrl?a=1&ts=1&nonce=n&sign=")


def test_encrypt_decrypt_round_trip():
    blob = ac.encrypt_info(b'{"a":1}', "short", TOKEN)
    assert ac.decrypt_info(blob, "short", TOKEN) == b'{"a":1}'


def test_lan_mode_off_is_reported():
    with pytest.raises(AnycubicError) as err:
        ac.parse_discovery({"ctrlType": "cloud"})
    assert err.value.slug == ac.ERROR_LAN_MODE_OFF


def test_handshake_and_probe():
    transport, seen = fake_printer()
    with httpx.Client(transport=transport) as http:
        discovery, creds = ac.handshake(http, IP, now_ms=123)
    assert discovery["model_id"] == 20026
    assert creds == {"host": IP, "port": 9883, "username": "user", "password": "pw", "device_id": "dev123"}
    assert seen["ctrl_query"]["ts"] == ["123"]
    assert len(seen["ctrl_query"]["did"][0]) == 32
    info = ac.probe(IP, transport=transport)
    assert info["model"] == "Kobra 3 Max" and info["supported"] is True


def test_probe_cloud_mode():
    transport, _ = fake_printer(ctrl_type="cloud")
    with pytest.raises(AnycubicError) as err:
        ac.probe(IP, transport=transport)
    assert err.value.slug == "lan_mode_off"


def test_establish_subscribes_and_queries():
    client, holder, _ = make_client()
    client.establish()
    mqtt = holder["mqtt"]
    assert mqtt.creds == ("user", "pw")
    assert mqtt.subscribed == ["anycubic/anycubicCloud/v1/printer/public/20026/dev123/#"]
    client.send_queries()
    topics = [t for t, _ in mqtt.published]
    assert "anycubic/anycubicCloud/v1/web/printer/20026/dev123/info" in topics
    assert any(t.endswith("/multiColorBox") for t in topics)


def test_establish_refused_credentials():
    transport, _ = fake_printer()
    client = AnycubicClient(IP, "S", mqtt_factory=lambda cid: FakeMqtt(cid, rc=5), http_transport=transport)
    with pytest.raises(AnycubicError) as err:
        client.establish()
    assert err.value.slug == ac.ERROR_UNAUTHORIZED


# ---------------------------------------------------------------- state mapping


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        (None, "IDLE"),
        ({"state": "printing", "print_status": 1}, "RUNNING"),
        ({"state": "paused", "print_status": 1}, "PAUSE"),
        ({"state": "printing", "pause": 1}, "PAUSE"),
        ({"state": "resuming", "pause": 1}, "RUNNING"),
        ({"state": "preheating", "print_status": 6}, "PREPARE"),
        ({"state": "printing", "print_status": 9}, "PREPARE"),
        ({"state": "finished"}, "FINISH"),
        ({"print_status": 2}, "FINISH"),
        ({"state": "failed"}, "FAILED"),
        ({"state": "stoped"}, "IDLE"),
        ({"state": "cancelled"}, "IDLE"),
    ],
)
def test_job_gcode_state(job, expected):
    assert ac.job_gcode_state(job, "free") == expected


def test_busy_without_job_is_prepare():
    assert ac.job_gcode_state(None, "busy") == "PREPARE"


def info(project, **extra):
    data = {
        "state": "busy" if project else "free",
        "version": "2.4.0",
        "temp": {"curr_nozzle_temp": 210, "target_nozzle_temp": 215, "curr_hotbed_temp": 60, "target_hotbed_temp": 60},
        "fan_speed_pct": 80,
        "print_speed_mode": 2,
        "project": project,
        "urls": {"fileUploadurl": f"http://{IP}:18910/gcode_upload?s=KEY", "rtspUrl": f"http://{IP}:18088/flv"},
    }
    data.update(extra)
    return {"type": "info", "action": "query", "state": "done", "code": 200, "data": data}


def job(state="printing", progress=40, **extra):
    data = {
        "task_id": "-1",
        "filename": "benchy.gcode",
        "progress": progress,
        "curr_layer": 20,
        "total_layers": 100,
        "remain_time": 33,
        "state": state,
        "print_status": 1,
    }
    data.update(extra)
    return data


def test_info_report_fills_state():
    client, _, _ = make_client()
    client.handle_report(info(job()))
    s = client.state
    assert s.connected and s.state == "RUNNING"
    assert s.temperatures["nozzle"] == 210 and s.temperatures["bed_target"] == 60
    assert s.progress == 40 and s.layer_num == 20 and s.total_layers == 100 and s.remaining_time == 33
    assert s.cooling_fan_speed == 80 and s.speed_level == 2
    assert s.subtask_name == "benchy" and s.firmware_version == "2.4.0"
    assert s.ftp_capable is False


def test_lifecycle_start_and_complete():
    starts, completes = [], []
    client, _, _ = make_client(on_print_start=starts.append, on_print_complete=completes.append)
    client.handle_report(info(None))
    client.handle_report(info(job(state="preheating", print_status=6)))
    assert client.state.state == "PREPARE" and not starts
    client.handle_report(info(job()))
    assert len(starts) == 1 and starts[0]["filename"] == "benchy.gcode"
    client.handle_report({"type": "print", "action": "update", "state": "paused", "data": {"pause": 1}})
    assert client.state.state == "PAUSE"
    client.handle_report({"type": "print", "action": "update", "state": "resumed", "data": {"pause": 0}})
    assert client.state.state == "RUNNING" and len(starts) == 1
    client.handle_report({"type": "print", "action": "update", "state": "finished", "data": job("finished", 100)})
    assert completes and completes[0]["status"] == "completed"
    client.handle_report(info(None))
    assert client.state.state == "IDLE" and len(completes) == 1


def test_job_vanishing_uses_last_project():
    completes = []
    client, _, _ = make_client(on_print_complete=completes.append)
    client.handle_report(info(None))
    client.handle_report(info(job()))
    client.handle_report(info(None, last_project=job("finished", 100, print_status=2)))
    assert completes[0]["status"] == "completed"


def test_stop_is_aborted():
    completes = []
    client, _, _ = make_client(on_print_complete=completes.append)
    client.handle_report(info(None))
    client.handle_report(info(job()))
    client.handle_report({"type": "print", "action": "stop", "state": "stoped", "data": {"taskid": "-1"}})
    assert completes[0]["status"] == "aborted"


def test_filament_step_is_not_a_job_state():
    client, _, _ = make_client()
    client.handle_report(info(job()))
    client.handle_report({"type": "print", "action": "start", "state": "ace_filament_feeding", "data": {"unit_no": 1}})
    assert client.state.state == "RUNNING"


def test_ace_boxes_become_ams_units():
    client, _, _ = make_client()
    client.handle_report(
        {
            "type": "multiColorBox",
            "action": "getInfo",
            "state": "success",
            "data": {
                "multi_color_box": [
                    {
                        "id": 0,
                        "temp": 25,
                        "humidity": 31,
                        "loaded_slot": 1,
                        "slots": [
                            {"index": 0, "type": "PLA", "color": [255, 0, 0], "status": 4, "edit_status": 0},
                            {"index": 1, "type": "PETG", "color": [0, 0, 255], "status": 5, "edit_status": 1},
                            {"index": 2, "type": "", "color": [0, 0, 0], "status": 0, "edit_status": 2},
                        ],
                    }
                ]
            },
        }
    )
    unit = client.state.raw_data["ams"][0]
    assert unit["module_type"] == "ace" and unit["humidity_raw"] == 31
    assert unit["tray"][0]["tray_color"] == "FF0000FF" and unit["tray"][0]["tray_type"] == "PLA"
    assert unit["tray"][2]["exists"] is False
    assert client.state.tray_now == 1
    mapping = client.ams_mapping_for_start()
    assert mapping["use_ams"] is True and mapping["ams_box_mapping"][0]["ams_index"] == 1
    # partial update: slot 0 changes colour only
    client.handle_report(
        {
            "type": "multiColorBox",
            "action": "setInfo",
            "state": "success",
            "data": {"multi_color_box": [{"id": 0, "slots": [{"index": 0, "color": [0, 255, 0]}]}]},
        }
    )
    tray0 = client.state.raw_data["ams"][0]["tray"][0]
    assert tray0["tray_color"] == "00FF00FF" and tray0["tray_type"] == "PLA"


def test_no_ace_start_mapping():
    client, _, _ = make_client()
    assert client.ams_mapping_for_start() == {"use_ams": False, "ams_box_mapping": []}


# ---------------------------------------------------------------- commands


def test_job_commands_carry_task_id():
    client, holder, _ = make_client()
    client.establish()
    client.handle_report(info(job(task_id="777")))
    mqtt = holder["mqtt"]
    assert client.pause_print() and client.stop_print()
    sent = [b for t, b in mqtt.published if t.endswith("/print")]
    assert [b["action"] for b in sent] == ["pause", "stop"]
    assert sent[0]["data"] == {"taskid": "777"} and "msgid" in sent[0] and "timestamp" in sent[0]


def test_controls():
    client, holder, _ = make_client()
    client.establish()
    client.set_bed_temperature(65)
    client.set_nozzle_temperature(220)
    client.set_fan_speed(1, 255)
    client.set_chamber_light(True)
    sent = {t.rsplit("/", 1)[1]: b for t, b in holder["mqtt"].published[-4:]}
    assert holder["mqtt"].published[-4][1]["data"] == {"type": 1, "target_nozzle_temp": 0, "target_hotbed_temp": 65}
    assert sent["tempature"]["data"]["target_nozzle_temp"] == 220
    assert sent["fan"]["data"] == {"fan_speed_pct": 100}
    assert sent["light"]["action"] == "control" and sent["light"]["data"]["status"] == 1
    assert client.send_gcode("G28") is False and client.ams_control("pause") is False


def test_commands_need_connection():
    client, _, _ = make_client()
    assert client.pause_print() is False


def test_upload_and_print(tmp_path):
    client, holder, seen = make_client()
    client.START_CONFIRM_TIMEOUT = 0.05
    client.establish()
    client.handle_report(info(None))
    gcode = tmp_path / "part.gcode"
    gcode.write_bytes(b"G28\nG1 X10\n")
    assert client.upload_and_print(str(gcode), "part.gcode") is True
    request = seen["upload"]
    assert request.url.params["s"] == "KEY"
    assert request.headers["X-File-Length"] == str(len(b"G28\nG1 X10\n"))
    body = seen["upload_body"]
    assert b'name="filename"' in body and b'name="gcode"; filename="part.gcode"' in body
    start = [b for t, b in holder["mqtt"].published if t.endswith("/print")][-1]
    assert start["action"] == "start"
    assert start["data"]["filename"] == "part.gcode" and start["data"]["filetype"] == 1
    assert start["data"]["ams_settings"] == {"use_ams": False, "ams_box_mapping": []}


def test_start_refused(tmp_path):
    client, holder, _ = make_client()
    client.establish()
    client.handle_report(info(None))
    holder["mqtt"].on_publish_hook = lambda topic, body: (
        topic.endswith("/print")
        and client.handle_report(
            {"type": "print", "action": "start", "state": "failed", "code": 10115, "msg": "bad params", "data": {}}
        )
    )
    gcode = tmp_path / "x.gcode"
    gcode.write_bytes(b"G28\n")
    assert client.upload_and_print(str(gcode)) is False
    assert "bad params" in client.last_error


def test_upload_refused(tmp_path):
    client, _, _ = make_client(upload_reply={"code": 500, "message": "disk full"})
    client.establish()
    client.handle_report(info(None))
    gcode = tmp_path / "x.gcode"
    gcode.write_bytes(b"G28\n")
    assert client.upload_and_print(str(gcode)) is False
    assert "disk full" in client.last_error


def test_upload_never_leaves_the_printer():
    client, _, _ = make_client()
    client.handle_report(info(None, urls={"fileUploadurl": "http://evil.example:18910/gcode_upload?s=K"}))
    assert client.upload_url() == f"http://{IP}:18910/gcode_upload?s=K"
