"""Queue dispatch to a Creality (Moonraker) printer.

The Bambu path is FTPS + an MQTT ``project_file``; a Creality printer is sent a plain
``.gcode`` through Moonraker's upload endpoint, which stores and starts it in one request.
"""

import zipfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
import backend.app.services.print_scheduler as scheduler_module
from backend.app.core.database import Base
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.moonraker_client import MoonrakerClient
from backend.app.services.print_scheduler import PrintScheduler


@pytest.fixture
async def case(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    yield SimpleNamespace(session_maker=session_maker, tmp=tmp_path)
    await engine.dispose()


async def _seed(
    case, *, filename="benchy.gcode", cleanup=False, printer_type="creality", content=None
) -> SimpleNamespace:
    path = case.tmp / filename
    path.write_bytes(b"; gcode\nG28\n" if content is None else content)
    async with case.session_maker() as db:
        printer = Printer(
            name="Ender",
            serial_number="CREALITY-192.168.1.50" if printer_type == "creality" else "SERIAL",
            ip_address="192.168.1.50",
            access_code="",
            model="Ender-3 V3 Plus",
            printer_type=printer_type,
        )
        db.add(printer)
        await db.flush()
        lib = LibraryFile(
            filename=filename,
            file_path=str(path),
            file_type="gcode" if filename.endswith(".gcode") else "3mf",
            file_size=path.stat().st_size,
        )
        db.add(lib)
        await db.flush()
        item = PrintQueueItem(
            printer_id=printer.id,
            library_file_id=lib.id,
            status="pending",
            cleanup_library_after_dispatch=cleanup,
        )
        db.add(item)
        await db.commit()
        return SimpleNamespace(item_id=item.id, lib_id=lib.id, path=path)


def _client(*, ok=True, error=None):
    client = MagicMock(spec=MoonrakerClient)
    client.upload_and_print.return_value = ok
    client.last_error = error
    return client


async def _run(case, ids, client, *, connected=True, state="IDLE"):
    scheduler = PrintScheduler()
    started = AsyncMock()
    failed = AsyncMock()
    async with case.session_maker() as db:
        item = await db.get(PrintQueueItem, ids.item_id)
        with ExitStack() as stack:
            pm = scheduler_module.printer_manager
            stack.enter_context(patch.object(pm, "get_client", MagicMock(return_value=client)))
            stack.enter_context(patch.object(pm, "is_connected", MagicMock(return_value=connected)))
            stack.enter_context(patch.object(pm, "get_status", MagicMock(return_value=SimpleNamespace(state=state))))
            stack.enter_context(patch.object(scheduler_module.notification_service, "on_queue_job_started", started))
            stack.enter_context(patch.object(scheduler_module.notification_service, "on_queue_job_failed", failed))
            stack.enter_context(patch.object(scheduler_module.ws_manager, "send_queue_item_failed", AsyncMock()))
            stack.enter_context(patch.object(scheduler, "_power_off_if_needed", AsyncMock()))
            stack.enter_context(patch.object(scheduler, "_propagate_owner_to_printer_manager", AsyncMock()))
            await scheduler._start_print(db, item)
    async with case.session_maker() as db:
        final = await db.get(PrintQueueItem, ids.item_id)
    return final, started, failed, scheduler


@pytest.mark.asyncio
async def test_gcode_is_sent_to_moonraker_and_the_item_starts_printing(case):
    ids = await _seed(case)
    client = _client()

    final, started, failed, scheduler = await _run(case, ids, client)

    client.upload_and_print.assert_called_once_with(str(ids.path), "benchy.gcode", True)
    assert final.status == "printing"
    assert final.started_at is not None
    assert final.billing_run_id
    started.assert_awaited_once()
    failed.assert_not_awaited()
    # the printer is held against a second dispatch while it digests the job
    assert scheduler._printer_in_dispatch_hold(final.printer_id)


@pytest.mark.asyncio
async def test_a_one_off_upload_is_removed_from_the_library_after_it_starts(case):
    ids = await _seed(case, cleanup=True)

    final, *_ = await _run(case, ids, _client())

    assert final.status == "printing"
    assert final.library_file_id is None
    assert not ids.path.exists()
    async with case.session_maker() as db:
        assert await db.get(LibraryFile, ids.lib_id) is None


@pytest.mark.asyncio
async def test_a_refused_upload_fails_the_item_with_the_printers_own_reason(case):
    ids = await _seed(case)
    client = _client(ok=False, error="The printer refused the file (HTTP 409: File is loaded)")

    final, _, failed, _ = await _run(case, ids, client)

    assert final.status == "failed"
    assert "HTTP 409" in final.error_message
    assert final.started_at is None
    failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_busy_printer_defers_the_item_without_sending_anything(case):
    ids = await _seed(case)
    client = _client()

    final, *_ = await _run(case, ids, client, state="RUNNING")

    client.upload_and_print.assert_not_called()
    assert final.status == "pending"


@pytest.mark.asyncio
async def test_a_disconnected_printer_fails_the_item(case):
    ids = await _seed(case)
    client = _client()

    final, *_ = await _run(case, ids, client, connected=False)

    client.upload_and_print.assert_not_called()
    assert final.status == "failed"
    assert final.error_message == "Printer not connected"


@pytest.mark.asyncio
async def test_a_missing_library_file_on_disk_fails_clearly(case):
    ids = await _seed(case)
    ids.path.unlink()

    final, *_ = await _run(case, ids, _client())

    assert final.status == "failed"
    assert "not found" in final.error_message.lower()


@pytest.mark.asyncio
async def test_bambu_printers_do_not_take_the_moonraker_path(case):
    ids = await _seed(case, printer_type="bambu")
    scheduler = PrintScheduler()
    async with case.session_maker() as db:
        item = await db.get(PrintQueueItem, ids.item_id)
        with (
            patch.object(scheduler, "_start_print_moonraker", AsyncMock()) as moonraker,
            patch.object(scheduler_module.printer_manager, "is_connected", MagicMock(return_value=False)),
            patch.object(scheduler, "_power_off_if_needed", AsyncMock()),
        ):
            await scheduler._start_print(db, item)
    moonraker.assert_not_awaited()
    assert Path(ids.path).exists()


def _sliced_3mf(tmp: Path, name: str, plates: dict[int, bytes]) -> bytes:
    archive = tmp / f"_{name}"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        for number, gcode in plates.items():
            zf.writestr(f"Metadata/plate_{number}.gcode", gcode)
    return archive.read_bytes()


@pytest.mark.asyncio
async def test_a_sliced_3mf_has_its_gcode_extracted_and_that_is_what_gets_sent(case):
    sent = {}

    def capture(path, name, start):
        sent["name"], sent["start"] = name, start
        sent["body"] = Path(path).read_bytes()
        sent["existed"] = Path(path).exists()
        return True

    client = _client()
    client.upload_and_print.side_effect = capture
    data = _sliced_3mf(case.tmp, "x.gcode.3mf", {1: b"G28\nG1 X5 ; plate one\n"})
    ids = await _seed(case, filename="Benchy.gcode.3mf", content=data)

    final, started, *_ = await _run(case, ids, client)

    assert final.status == "printing"
    assert sent == {"name": "Benchy.gcode", "start": True, "body": b"G28\nG1 X5 ; plate one\n", "existed": True}
    started.assert_awaited_once()
    # the temporary extraction is gone afterwards
    assert not list(Path(tempfile_dir()).glob("bambuddy-gcode-*"))


def tempfile_dir() -> str:
    import tempfile

    return tempfile.gettempdir()


@pytest.mark.asyncio
async def test_a_3mf_without_gcode_is_refused_as_unsliced(case):
    data = _sliced_3mf(case.tmp, "y.3mf", {})
    ids = await _seed(case, filename="project.3mf", content=data)
    client = _client()

    final, _, failed, _ = await _run(case, ids, client)

    client.upload_and_print.assert_not_called()
    assert final.status == "failed"
    assert "no sliced G-code" in final.error_message
    failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_corrupt_3mf_is_refused_not_crashed(case):
    ids = await _seed(case, filename="broken.3mf", content=b"not a zip at all")
    client = _client()

    final, *_ = await _run(case, ids, client)

    client.upload_and_print.assert_not_called()
    assert final.status == "failed"
    assert "could not be read" in final.error_message


@pytest.mark.asyncio
async def test_other_file_types_are_refused(case):
    ids = await _seed(case, filename="model.stl", content=b"solid x")
    final, *_ = await _run(case, ids, _client())
    assert final.status == "failed"
    assert ".gcode" in final.error_message
