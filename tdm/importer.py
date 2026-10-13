"""Импорт JSON-пакета (формат contract/bundle.schema.json) — для расчётного сервиса на любом языке."""
import json
from pathlib import Path

from .writer import Run, load_history, upsert_carry_scenarios, upsert_cbr_meetings, upsert_scenarios


def import_bundle(db_path, bundle):
    """bundle — dict или путь к .json (контракт 1.0–1.3). Возвращает (run_id, status, warnings).

    1.3: run.prev_date / accrual_days / jtd_recovery / ofz_spread_move; книги с book_type, entity, ocp_to; курсы ЦБ в fx;
    funding_rules, ocp_transfers (на прогон); carry_scenarios, cbr_meetings (глобально); строки фондирования в funding.
    run.derive_funding = true — недостающие строки фондирования строит tdm (Run.derive_funding)."""
    if not isinstance(bundle, dict):
        bundle = json.loads(Path(bundle).read_text(encoding="utf-8"))
    if bundle.get("scenarios"):
        upsert_scenarios(db_path, bundle["scenarios"])
    if bundle.get("carry_scenarios"):
        upsert_carry_scenarios(db_path, bundle["carry_scenarios"])
    if bundle.get("cbr_meetings"):
        upsert_cbr_meetings(db_path, bundle["cbr_meetings"])
    if bundle.get("history"):
        load_history(db_path, bundle["history"])
    r = bundle["run"]
    run = Run(db_path, as_of=r["as_of"], run_type=r.get("run_type", "EOD"), source=r.get("source"),
              lib_version=r.get("lib_version"), market_snapshot=r.get("market_snapshot"), comment=r.get("comment"),
              contract_version=bundle.get("contract_version", "1.0"), prev_date=r.get("prev_date"), accrual_days=r.get("accrual_days"),
              jtd_recovery=r.get("jtd_recovery"), ofz_spread_move=r.get("ofz_spread_move"))
    with run:
        run.add_books(bundle["books"])
        run.add_issuers(bundle.get("issuers", []))
        run.add_curves(bundle["curves"])
        run.add_fx(bundle["fx"])
        run.add_indices(bundle.get("indices", []))
        run.add_funding_rules(bundle.get("funding_rules", []))
        run.add_positions(bundle["positions"])
        run.add_funding(bundle.get("funding", []))
        if r.get("derive_funding"):
            run.derive_funding()
        if "ocp_transfers" in bundle:
            run.add_ocp_transfers(bundle["ocp_transfers"])
        elif r.get("derive_funding"):
            run.add_ocp_transfers()
        for s in bundle.get("scenario_results", []):
            run.add_scenario_results(s["scenario_id"], s["positions"], s.get("funding_residual"))
    return run.run_id, run.status, run.warnings


def load_demo_market(db_path, path):
    """Демо-история рынка за год (fixtures/demo_market.json.gz, выгружена из prototypes/data.js):
    md_curve_point, md_bond_quote, ref_instrument и pos_snapshot облигаций. Повторная загрузка заменяет строки."""
    import gzip

    from .db import init_db, now_iso
    m = json.loads(gzip.decompress(Path(path).read_bytes()).decode("utf-8"))
    dates, ts = m["dates"], now_iso()
    con = init_db(db_path)
    try:
        with con:
            con.executemany("INSERT OR REPLACE INTO md_curve_point (date, curve_id, tenor_years, rate, source, updated_at) VALUES (?,?,?,?,?,?)",
                            [(d, c, t, r, "demo", ts) for c, days in m["curves"].items() for d, pts in zip(dates, days) for t, r in zip(m["pillars"], pts)])
            cols = ["instrument_id", "isin", "name", "issuer_id", "asset_class", "segment", "ccy", "face_value", "maturity", "coupon_type",
                    "coupon_rate", "coupon_freq", "index_name", "margin", "offer_date", "is_mbs"]
            con.executemany(f"INSERT OR REPLACE INTO ref_instrument ({', '.join(cols)}, updated_at) VALUES ({', '.join('?' * len(cols))}, ?)",
                            [[i.get(c) for c in cols] + [ts] for i in m["instruments"]])
            q, snap = [], []
            for iid, s in m["quotes"].items():
                for k, d in enumerate(dates):
                    q.append((d, iid, s["clean_price"][k], s["ytm"][k], s["z_spread_bp"][k], s["g_spread_bp"][k], s["accrued"][k], s["volume_rub"][k], "demo", ts))
                    if s["face_amount"][k]:
                        snap.append((d, s["book_id"], s["position_id"], iid, s["face_amount"][k], ts))
            con.executemany("INSERT OR REPLACE INTO md_bond_quote (date, instrument_id, clean_price, ytm, z_spread_bp, g_spread_bp, accrued, volume_rub, source, updated_at)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?)", q)
            con.executemany("INSERT OR REPLACE INTO pos_snapshot (as_of, book_id, position_id, instrument_id, face_amount, updated_at) VALUES (?,?,?,?,?,?)", snap)
        return {"dates": len(dates), "curve_points": len(dates) * len(m["curves"]) * len(m["pillars"]), "bond_quotes": len(q), "pos_snapshot": len(snap)}
    finally:
        con.close()
