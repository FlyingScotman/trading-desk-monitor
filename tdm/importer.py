"""Импорт JSON-пакета (формат contract/bundle.schema.json) — для расчётного сервиса на любом языке."""
import json
from pathlib import Path

from .writer import Run, load_history, upsert_scenarios


def import_bundle(db_path, bundle):
    """bundle — dict или путь к .json. Возвращает (run_id, status, warnings)."""
    if not isinstance(bundle, dict):
        bundle = json.loads(Path(bundle).read_text(encoding="utf-8"))
    if bundle.get("scenarios"):
        upsert_scenarios(db_path, bundle["scenarios"])
    if bundle.get("history"):
        load_history(db_path, bundle["history"])
    r = bundle["run"]
    run = Run(db_path, as_of=r["as_of"], run_type=r.get("run_type", "EOD"), source=r.get("source"),
              lib_version=r.get("lib_version"), market_snapshot=r.get("market_snapshot"), comment=r.get("comment"),
              contract_version=bundle.get("contract_version", "1.0"))
    with run:
        run.add_books(bundle["books"])
        run.add_issuers(bundle.get("issuers", []))
        run.add_curves(bundle["curves"])
        run.add_fx(bundle["fx"])
        run.add_indices(bundle.get("indices", []))
        run.add_positions(bundle["positions"])
        run.add_funding(bundle.get("funding", []))
        for s in bundle.get("scenario_results", []):
            run.add_scenario_results(s["scenario_id"], s["positions"], s.get("funding_residual"))
    return run.run_id, run.status, run.warnings
