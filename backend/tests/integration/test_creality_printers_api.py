"""Integration tests: adding and reading Creality (Klipper / Moonraker) printers."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.printer import Printer
from backend.app.services.bambu_mqtt import PrinterState

PROBE = "backend.app.services.printer_manager.printer_manager.test_connection"
CONNECT = "backend.app.services.printer_manager.printer_manager.connect_printer"

OK = {
    "success": True,
    "state": "IDLE",
    "model": None,
    "reason": None,
    "firmware_version": "v0.11.0-creality",
    "webcam": {
        "stream_url": "http://192.168.1.50/webcam/?action=stream",
        "snapshot_url": "http://192.168.1.50/webcam/?action=snapshot",
    },
}


@pytest.fixture(autouse=True)
def _no_real_connection():
    with patch(CONNECT, new=AsyncMock(return_value=True)):
        yield


class TestCrealityPrintersAPI:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_creality_printer_needs_only_name_and_ip(self, async_client: AsyncClient, db_session):
        with patch(PROBE, new=AsyncMock(return_value=OK)) as probe:
            response = await async_client.post(
                "/api/v1/printers/",
                json={
                    "name": "Ender 3 V3 KE",
                    "ip_address": "192.168.1.50",
                    "printer_type": "creality",
                    "model": "Ender-3 V3 KE",
                },
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["printer_type"] == "creality"
        assert body["serial_number"] == "CREALITY-192.168.1.50"
        assert body["model"] == "Ender-3 V3 KE"
        assert body["supports_nozzle_flow_type"] is not None
        probe.assert_awaited_once()
        assert probe.await_args.kwargs["printer_type"] == "creality"
        assert probe.await_args.kwargs["api_port"] is None

        row = (await db_session.execute(select(Printer).where(Printer.id == body["id"]))).scalar_one()
        assert row.printer_type == "creality"
        assert row.access_code == ""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_custom_port_is_probed_and_stored(self, async_client: AsyncClient, db_session):
        with patch(PROBE, new=AsyncMock(return_value=OK)) as probe:
            response = await async_client.post(
                "/api/v1/printers/",
                json={"name": "E3", "ip_address": "192.168.1.51", "printer_type": "creality", "api_port": 4408},
            )

        assert response.status_code == 200, response.text
        assert response.json()["api_port"] == 4408
        assert probe.await_args.kwargs["api_port"] == 4408

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_camera_is_prefilled_from_the_printers_webcam_config(self, async_client: AsyncClient):
        with patch(PROBE, new=AsyncMock(return_value=OK)):
            response = await async_client.post(
                "/api/v1/printers/",
                json={"name": "E3", "ip_address": "192.168.1.50", "printer_type": "creality"},
            )

        body = response.json()
        assert body["external_camera_enabled"] is True
        assert body["external_camera_type"] == "mjpeg"
        assert body["external_camera_url"] == "http://192.168.1.50/webcam/?action=stream"
        assert body["external_camera_snapshot_url"] == "http://192.168.1.50/webcam/?action=snapshot"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_camera_the_user_set_is_not_overwritten(self, async_client: AsyncClient):
        with patch(PROBE, new=AsyncMock(return_value=OK)):
            response = await async_client.post(
                "/api/v1/printers/",
                json={
                    "name": "E3",
                    "ip_address": "192.168.1.50",
                    "printer_type": "creality",
                    "external_camera_url": "rtsp://cam.local/stream",
                    "external_camera_type": "rtsp",
                    "external_camera_enabled": True,
                },
            )

        body = response.json()
        assert body["external_camera_url"] == "rtsp://cam.local/stream"
        assert body["external_camera_type"] == "rtsp"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unreachable_creality_printer_is_not_saved(self, async_client: AsyncClient, db_session):
        failed = {"success": False, "state": None, "model": None, "reason": "unreachable", "webcam": None}
        with patch(PROBE, new=AsyncMock(return_value=failed)):
            response = await async_client.post(
                "/api/v1/printers/",
                json={"name": "Ghost", "ip_address": "192.168.1.77", "printer_type": "creality"},
            )

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "printer_connection_failed"
        assert detail["reason"] == "unreachable"
        assert "Moonraker" in detail["message"]
        rows = (await db_session.execute(select(Printer).where(Printer.ip_address == "192.168.1.77"))).scalars().all()
        assert rows == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_two_printers_cannot_share_an_ip(self, async_client: AsyncClient):
        payload = {"name": "E3", "ip_address": "192.168.1.50", "printer_type": "creality"}
        with patch(PROBE, new=AsyncMock(return_value=OK)):
            assert (await async_client.post("/api/v1/printers/", json=payload)).status_code == 200
            second = await async_client.post("/api/v1/printers/", json={**payload, "name": "E3 again"})
        assert second.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bambu_printers_still_default_to_bambu_and_still_need_an_access_code(self, async_client: AsyncClient):
        missing_code = await async_client.post(
            "/api/v1/printers/",
            json={"name": "X1C", "serial_number": "00M09A111111111", "ip_address": "192.168.1.100"},
        )
        assert missing_code.status_code == 422

        with patch(PROBE, new=AsyncMock(return_value={"success": True, "state": "IDLE", "model": "X1C"})) as probe:
            ok = await async_client.post(
                "/api/v1/printers/",
                json={
                    "name": "X1C",
                    "serial_number": "00M09A111111111",
                    "ip_address": "192.168.1.100",
                    "access_code": "12345678",
                },
            )
        assert ok.status_code == 200
        assert ok.json()["printer_type"] == "bambu"
        assert probe.await_args.kwargs["printer_type"] == "bambu"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_connection_test_endpoint_accepts_creality_without_credentials(self, async_client: AsyncClient):
        with patch(PROBE, new=AsyncMock(return_value=OK)) as probe:
            response = await async_client.post(
                "/api/v1/printers/test", params={"ip_address": "192.168.1.50", "printer_type": "creality"}
            )

        assert response.status_code == 200
        assert response.json()["success"] is True
        assert probe.await_args.kwargs["printer_type"] == "creality"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_connection_test_endpoint_still_requires_bambu_credentials(self, async_client: AsyncClient):
        response = await async_client.post("/api/v1/printers/test", params={"ip_address": "192.168.1.50"})
        assert response.status_code == 400

        bad_type = await async_client.post(
            "/api/v1/printers/test", params={"ip_address": "192.168.1.50", "printer_type": "prusa"}
        )
        assert bad_type.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_status_endpoint_serves_a_klipper_printer(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(name="Ender", printer_type="creality", model="Ender-3 V3 KE")
        state = PrinterState(connected=True, state="RUNNING", ftp_capable=False)
        state.gcode_file = "benchy.gcode"
        state.current_print = state.subtask_name = "benchy"
        state.progress = 42.0
        state.remaining_time = 31
        state.layer_num, state.total_layers = 40, 100
        state.temperatures = {"nozzle": 210.0, "nozzle_target": 210.0, "bed": 60.0, "bed_target": 60.0}

        with patch("backend.app.services.printer_manager.printer_manager.get_status", return_value=state):
            response = await async_client.get(f"/api/v1/printers/{printer.id}/status")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["connected"] is True
        assert body["state"] == "RUNNING"
        assert body["progress"] == 42.0
        assert body["remaining_time"] == 31
        assert body["layer_num"] == 40 and body["total_layers"] == 100
        assert body["temperatures"]["bed"] == 60.0
        assert body["ams"] == [] and body["ams_exists"] is False


class TestCrealityK2ChamberAPI:
    CLIENT = "backend.app.services.printer_manager.printer_manager.get_client"
    STATUS = "backend.app.services.printer_manager.printer_manager.get_status"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_k2_plus_chamber_can_be_heated_through_the_normal_route(
        self, async_client: AsyncClient, printer_factory
    ):
        from unittest.mock import MagicMock

        printer = await printer_factory(name="K2", printer_type="creality", model="K2 Plus")
        client = MagicMock()
        client.set_chamber_temperature.return_value = True

        with patch(self.CLIENT, return_value=client):
            response = await async_client.post(
                f"/api/v1/printers/{printer.id}/temperature/chamber", params={"target": 55}
            )

        assert response.status_code == 200, response.text
        client.set_chamber_temperature.assert_called_once_with(55)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_ender_has_no_chamber_heater_and_is_refused(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(name="Ender", printer_type="creality", model="Ender-3 V3 KE")
        response = await async_client.post(f"/api/v1/printers/{printer.id}/temperature/chamber", params={"target": 40})
        assert response.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_k2_status_reports_chamber_temperature_and_heater_support(
        self, async_client: AsyncClient, printer_factory
    ):
        printer = await printer_factory(name="K2", printer_type="creality", model="K2 Plus")
        state = PrinterState(connected=True, state="IDLE", ftp_capable=False)
        state.temperatures = {"nozzle": 25.0, "bed": 24.0, "chamber": 38.4, "chamber_target": 55.0}

        with patch(self.STATUS, return_value=state):
            body = (await async_client.get(f"/api/v1/printers/{printer.id}/status")).json()

        assert body["temperatures"]["chamber"] == 38.4
        assert body["temperatures"]["chamber_target"] == 55.0
        assert body["supports_chamber_heater"] is True
