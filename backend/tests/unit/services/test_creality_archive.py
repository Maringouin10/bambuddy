"""Completing a Creality print's archive from what Moonraker knows about the file."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
import backend.app.services.creality_archive as module
from backend.app.core.database import Base
from backend.app.models.archive import PrintArchive
from backend.app.models.filament import Filament
from backend.app.models.printer import Printer
from backend.app.services.creality_archive import (
    _largest_thumbnail,
    archive_fields_from_metadata,
    enrich_archive,
)
from backend.app.services.moonraker_client import MoonrakerClient

METADATA = {
    "estimated_time": 5400,
    "filament_weight_total": 12.345,
    "filament_type": "PETG;PETG",
    "layer_height": 0.2,
    "layer_count": 150,
    "nozzle_diameter": 0.4,
    "first_layer_bed_temp": 70,
    "first_layer_extr_temp": 235,
    "thumbnails": [
        {"width": 32, "height": 32, "relative_path": ".thumbs/a-32x32.png"},
        {"width": 300, "height": 300, "relative_path": ".thumbs/a-300x300.png"},
    ],
}


def test_the_largest_thumbnail_wins():
    assert _largest_thumbnail(METADATA) == ".thumbs/a-300x300.png"
    assert _largest_thumbnail({}) is None
    assert _largest_thumbnail({"thumbnails": [{"width": 1, "height": 1}]}) is None


def test_only_what_moonraker_really_knows_becomes_a_field():
    fields = archive_fields_from_metadata(METADATA)
    assert fields == {
        "print_time_seconds": 5400,
        "filament_used_grams": 12.35,
        "filament_type": "PETG",
        "layer_height": 0.2,
        "total_layers": 150,
        "nozzle_diameter": 0.4,
        "bed_temperature": 70,
        "nozzle_temperature": 235,
    }
    assert archive_fields_from_metadata({"estimated_time": 0, "filament_weight_total": "n/a"}) == {}


@pytest.fixture
async def env(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        printer = Printer(
            name="Ender",
            serial_number="CREALITY-1",
            ip_address="10.0.0.5",
            access_code="",
            model="Ender-3 V3 Plus",
            printer_type="creality",
        )
        db.add(printer)
        await db.flush()
        archive = PrintArchive(
            printer_id=printer.id,
            filename="benchy.gcode",
            file_path="",
            file_size=0,
            print_name="benchy",
            status="printing",
            print_time_seconds=None,
            extra_data={"no_3mf_available": False, "no_3mf_reason": "no_ftp_service"},
        )
        db.add(archive)
        db.add(Filament(name="PETG", type="PETG", cost_per_kg=40.0))
        await db.commit()
        ids = SimpleNamespace(printer=printer.id, archive=archive.id)
    base = tmp_path / "data"
    (base / "archive").mkdir(parents=True)
    with (
        patch.object(module, "async_session", maker),
        patch.object(module.settings, "base_dir", base),
        patch.object(module.settings, "archive_dir", base / "archive"),
    ):
        yield SimpleNamespace(maker=maker, ids=ids, base=base)
    await engine.dispose()


def _client(*, metadata=METADATA, gcode=b"G28\n; printer_model = Creality Ender-3 V3\n", thumb=b"\x89PNG"):
    client = MagicMock(spec=MoonrakerClient)
    client.ip_address = "10.0.0.5"
    client.get_file_metadata.return_value = metadata
    client.download_thumbnail.return_value = thumb

    def download(filename, dest, max_bytes):
        if gcode is None:
            return None
        dest.write_bytes(gcode)
        return len(gcode)

    client.download_gcode.side_effect = download
    return client


async def _run(env, client):
    with patch.object(module.printer_manager, "get_client", MagicMock(return_value=client)):
        done = await enrich_archive(env.ids.archive, env.ids.printer, "benchy.gcode")
    async with env.maker() as db:
        return done, await db.get(PrintArchive, env.ids.archive)


@pytest.mark.asyncio
async def test_the_archive_gets_a_cover_filament_cost_and_a_reprintable_gcode(env):
    done, archive = await _run(env, _client())

    assert done is True
    assert (env.base / archive.thumbnail_path).read_bytes() == b"\x89PNG"
    gcode = env.base / archive.file_path
    assert gcode.name == "benchy.gcode" and gcode.read_bytes().startswith(b"G28")
    assert archive.file_size == gcode.stat().st_size
    assert len(archive.content_hash) == 64
    assert archive.sliced_for_model == "Creality Ender-3 V3"
    assert (archive.print_time_seconds, archive.filament_used_grams, archive.filament_type) == (5400, 12.35, "PETG")
    assert (archive.total_layers, archive.layer_height, archive.nozzle_diameter) == (150, 0.2, 0.4)
    assert archive.cost == round(12.35 / 1000 * 40.0, 2)
    assert archive.extra_data["no_3mf_available"] is False and archive.extra_data["source"] == "moonraker"


@pytest.mark.asyncio
async def test_a_missing_gcode_still_completes_the_rest(env):
    done, archive = await _run(env, _client(gcode=None))
    assert done is True
    assert archive.file_path == "" and archive.thumbnail_path
    assert archive.filament_used_grams == 12.35


@pytest.mark.asyncio
async def test_an_existing_print_time_is_not_overwritten(env):
    async with env.maker() as db:
        archive = await db.get(PrintArchive, env.ids.archive)
        archive.print_time_seconds = 4000
        await db.commit()
    _, archive = await _run(env, _client())
    assert archive.print_time_seconds == 4000


@pytest.mark.asyncio
async def test_default_filament_price_is_used_for_a_type_with_no_price(env):
    meta = {**METADATA, "filament_type": "ASA"}
    _, archive = await _run(env, _client(metadata=meta))
    assert archive.cost == round(12.35 / 1000 * 25.0, 2)


@pytest.mark.asyncio
async def test_no_metadata_or_the_wrong_printer_leaves_the_stub_alone(env):
    done, archive = await _run(env, _client(metadata={}))
    assert done is False
    assert archive.thumbnail_path is None and archive.file_path == ""

    with patch.object(module.printer_manager, "get_client", MagicMock(return_value=_client())):
        assert await enrich_archive(env.ids.archive, 9999, "benchy.gcode") is False


@pytest.mark.asyncio
async def test_a_bambu_client_is_never_enriched(env):
    with patch.object(module.printer_manager, "get_client", MagicMock(return_value=MagicMock())):
        assert await enrich_archive(env.ids.archive, env.ids.printer, "benchy.gcode") is False
