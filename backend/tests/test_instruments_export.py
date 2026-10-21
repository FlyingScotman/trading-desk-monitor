"""``desk instruments export`` (R9) on the hub's golden fixtures: the universe without positions, as the desk sees it."""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import pytest

from desk import cli
from desk.marketdata.bundle import SnapshotDir
from desk.publish import tdm_run
from desk.report import instruments

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
STATIC = "4f173275220b492c558acad982351bac01fe73baea6d50dcb0fcc38400953c4a"


@pytest.fixture(scope="module")
def export():
    static, market = instruments.find_snapshots(FIX, "2026-10-15")
    return instruments.build(static, market)


def rows_of(ex) -> dict[str, dict]:
    return {r["secid"]: r for r in ex.rows}


def test_picks_the_static_snapshot_desk_eod_uses(export):
    assert export.static.id == STATIC
    assert export.market is not None and export.market.static_snapshot_id == STATIC
    static, _ = instruments.find_snapshots(FIX)  # newest static without a date
    assert static.id == STATIC
    assert set(SnapshotDir(FIX).manifests()) == {STATIC, export.market.id}


def test_every_instrument_one_row_with_every_column(export):
    assert len(export.rows) == 10
    keys = [k for k, _ in instruments.COLUMNS]
    assert all(list(r) == keys for r in export.rows)
    assert len(set(keys)) == len(keys)


def test_monitor_mapping_matches_publish(export):
    assert instruments.MON_ASSET_CLASS == tdm_run.ASSET_CLASS
    assert instruments.MON_SEGMENT == tdm_run.SEGMENT
    r = rows_of(export)
    assert r["SU26238RMFS4"]["mon_name"] == "ОФЗ-ПД 26238 15/05/2041"  # MOEX NAME, as position_row shows it (D1)
    rzd = r["RU000A0JXQ44"]  # "открытое акционерное общество ..." read without the legal form (D2); no group yet
    assert rzd["mon_issuer"] == rzd["mon_issuer_group"] == "Российские железные дороги"
    assert r["RU000A100GW5"]["mon_issuer"] == "Альфа-Банк" and r["RU000A1034P7"]["mon_issuer"] == "Банк ВТБ"
    assert r["RU000A109XR1"]["mon_coupon_type"] == "float" and r["SU52005RMFS4"]["mon_coupon_type"] == "indexed_face"
    assert r["RU000A10GCT1"]["mon_coupon_type"] == "unknown" and r["SU26238RMFS4"]["mon_coupon_type"] == "fix"
    assert all(x["rating"] is None for x in export.rows)


def test_core_static_reasons_and_coupons(export):
    r = rows_of(export)
    assert r["SU26238RMFS4"]["core_static"] == "ok" and r["SU26238RMFS4"]["mon_coupon_pct"] == 7.1
    assert r["SU52005RMFS4"]["core_static"] == "face_indexed"
    assert r["RU000A10BRM5"]["core_static"] == "not_rub"  # USD face settled in RUB
    assert "currency_mismatch" in r["RU000A10BRM5"]["dq_flags"]
    assert r["RU000A100GW5"]["core_assumptions"].startswith("perpetual: call assumed")
    # a floater whose running period has an amount and no rate: the amount as a rate (D4)
    assert r["RU000A109XR1"]["core_static"] == "ok" and r["RU000A109XR1"]["mon_coupon_pct"] == 15.7558
    assert r["SU52005RMFS4"]["core_static"] == "face_indexed" and r["SU52005RMFS4"]["mon_coupon_pct"] == 2.5
    assert "floater_formula_inferred" in r["RU000A109XR1"]["dq_flags"]


def test_flags(export):
    r = rows_of(export)
    assert "coupon_type_unknown" in r["RU000A10GCT1"]["dq_flags"]
    assert "perpetual_no_call" in r["RU000A1034P7"]["dq_flags"]
    assert "issuer_name_dated" in r["RU000A0JXQ44"]["dq_flags"]  # "открытое акционерное общество ..."
    assert set(instruments.FLAGS) >= {f for x in export.rows for f in x["dq_flags"]}


def test_quotes_and_summary(export):
    assert sum(r["has_quote"] for r in export.rows) >= 9
    s = instruments.summary(export.rows)
    assert s["instruments"] == 10 and s["by_class"]["corporate"] == 6
    # the hub has no sector in the fixture; the four OFZ get the sovereign by the bond class (R25)
    assert s["fill"]["_total"]["sector"] == 4 and s["fill"]["_total"]["isin"] == 10
    assert s["fill"]["_total"]["rating_agency"] == 0  # a 1.0 static: no ratings
    assert s["core_static"]["ok"] == 8
    assert "instruments" in instruments.summary_text(s)


def test_cli_csv_and_xlsx(tmp_path, capsys):
    out = tmp_path / "instruments.csv"
    assert cli.main(["instruments", "export", "--out", str(out), "--snapshots", str(FIX), "--date", "2026-10-15",
                     "--value"]) == 0
    assert "10 instruments" in capsys.readouterr().out
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM: Excel reads UTF-8
    rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    assert len(rows) == 10 and {r["core_market"] for r in rows} <= {"exact", "assumed", "not_rub", "face_indexed"}
    assert rows[0]["secid"] == "RU000A0JXQ44"

    xl = tmp_path / "instruments.xlsx"
    assert cli.main(["instruments", "export", "--out", str(xl), "--snapshots", str(FIX), "--active"]) == 0
    with zipfile.ZipFile(xl) as z:
        names = set(z.namelist())
        assert {"xl/workbook.xml", "xl/worksheets/sheet1.xml", "xl/worksheets/sheet3.xml"} <= names
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert "ОФЗ 26238" in sheet and sheet.count("<row ") == 11


def test_static_file(tmp_path):
    out = tmp_path / "x.csv"
    ex, _ = instruments.export(out, tmp_path, static_file=FIX / f"{STATIC}.json.gz")
    assert ex.market is None and len(ex.rows) == 10 and all(r["has_quote"] is None for r in ex.rows)
