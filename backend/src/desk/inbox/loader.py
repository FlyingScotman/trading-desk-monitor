"""Load a private adapter (08 §2): locate it, fingerprint it, and check its declaration through the runner.

Nothing here imports adapter code: ``describe()`` runs in the child like every other call, so a failing
import or a name collision is reported by the child and the backend process stays clean. Loading never
raises; a failure gives an ``AdapterHandle`` with ``error`` set and ingestion stays off with that reason.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.machinery
import re
from dataclasses import dataclass, field
from pathlib import Path

from desk.inbox import runner
from desk.inbox.schema import load_schema
from desk.inbox.spi import (
    CODE_RE,
    COMPLETENESS,
    COVERS,
    DATASETS,
    DATASETS_WITH_BOOK,
    SPI_MAJORS,
    TIME_BASES,
    AdapterFailure,
    contract_supported,
    source_datasets,
)

DESCRIBE_TIMEOUT_S = 30.0
CONFIG_NAME_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}")  # a plain file name: no folder, no dot first


@dataclass(frozen=True)
class AdapterHandle:
    spec: str                    # as given; "" means the reference adapter
    name: str | None
    version: str | None
    fingerprint: str | None
    describe: dict = field(default_factory=dict)
    error: str | None = None     # "<code>[: detail]" when loading failed; then ingestion is off
    config_dir: str | None = None
    warnings: tuple[str, ...] = ()  # optional describe() keys of a bad shape, ignored (config_files)
    inbox_dirs: tuple[str, ...] = ()  # passed to the adapter as ctx.inbox_dirs (08 §7)

    @property
    def ok(self) -> bool:
        return self.error is None

    def source(self, source_id: str) -> dict | None:
        return next((s for s in self.describe.get("sources", []) if s.get("source") == source_id), None)


def _package_files(spec: str, location: dict | None = None) -> tuple[Path, list[Path]]:
    """The adapter's own files: the package directory (path spec, or a module spec resolving to a package), or
    the single module file. ``location`` (``{"file", "path"}`` as the child imported the module) wins over the
    parent's own resolution, whose ``sys.path`` is not the child's."""
    if location is not None:
        if location.get("path"):
            root = Path(location["path"][0])
        elif location.get("file"):
            return Path(location["file"]).parent, [Path(location["file"])]
        else:
            raise FileNotFoundError(spec)
    elif runner.is_path_spec(spec):
        root = Path(spec).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(spec)
    else:
        path = None
        for part in spec.split("."):
            found = importlib.machinery.PathFinder.find_spec(part, path)
            if found is None:
                raise FileNotFoundError(spec)
            path = found.submodule_search_locations
        if path:
            root = Path(next(iter(path)))
        elif found.origin:
            return Path(found.origin).parent, [Path(found.origin)]
        else:
            raise FileNotFoundError(spec)
    files = [p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    return root, files


def fingerprint(spec: str | None, config_dir: str | None, location: dict | None = None) -> str:
    """SHA-256 over the adapter package's files and the contents of the config folder (08 §2)."""
    spec = spec or runner.REFERENCE_ADAPTER
    h = hashlib.sha256()
    root, files = _package_files(spec, location)
    groups = [("adapter", root, files)]
    if config_dir:
        c = Path(config_dir)
        groups.append(("config", c, [p for p in c.rglob("*") if p.is_file()] if c.is_dir() else []))
    for label, base, paths in groups:
        for p in sorted(paths, key=lambda p: p.relative_to(base).as_posix()):
            h.update(f"{label}/{p.relative_to(base).as_posix()}\0".encode())
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def _one_of(value: object, allowed: frozenset[str]) -> bool:
    return isinstance(value, str) and value in allowed  # a list or dict is not hashable: type first


def check_describe(desc: object, spi_version: object, has_reference: bool) -> str | None:
    """The reason a declaration is unusable (08 §4, §9), or None."""
    if not isinstance(desc, dict):
        return "bad_describe: not an object"
    if not (isinstance(desc.get("name"), str) and desc["name"] and isinstance(desc.get("version"), str)):
        return "bad_describe: name and version must be strings"
    if spi_version != desc.get("spi") or type(spi_version) is not int:
        return "spi_mismatch: SPI_VERSION differs from describe()['spi']"
    if spi_version not in SPI_MAJORS:
        return f"unsupported_spi: {spi_version} (supported {sorted(SPI_MAJORS)})"
    if not contract_supported(desc.get("contract")):
        return "unsupported_contract: need private/1.x"
    sources = desc.get("sources")
    if not isinstance(sources, list) or not sources:
        return "bad_describe: sources must be a non-empty list"
    seen: set[str] = set()
    for s in sources:
        why = _check_source(s)
        if why:
            return f"bad_source: {why}"
        if s["source"] in seen:
            return f"bad_source: {s['source']} declared twice"
        seen.add(s["source"])
        if not has_reference and any(d in DATASETS_WITH_BOOK for d in source_datasets(s)):
            return f"missing_reference: {s['source']} emits book_id, reference() is required"
    return None


def check_config_files(desc: object) -> tuple[list[dict], list[str]]:
    """The optional ``describe()["config_files"]`` (08 §4): the usable entries ``{name, title, help, required,
    template}`` and warnings for the rest. Never fatal: a bad entry is left out, a bad key ignored."""
    raw = desc.get("config_files") if isinstance(desc, dict) else None
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["config_files: not a list, ignored"]
    out: list[dict] = []
    warnings: list[str] = []
    for i, e in enumerate(raw):
        name = e.get("name") if isinstance(e, dict) else None
        if not (isinstance(name, str) and CONFIG_NAME_RE.fullmatch(name)):
            warnings.append(f"config_files[{i}]: name must be a plain file name, ignored")
            continue
        if any(o["name"] == name for o in out):
            warnings.append(f"config_files[{i}]: {name} declared twice, ignored")
            continue
        entry = {"name": name, "title": e.get("title") if isinstance(e.get("title"), str) else name,
                 "help": e.get("help") if isinstance(e.get("help"), str) else "",
                 "required": e.get("required") is True, "template": e.get("template")}
        for key, want in (("title", str), ("help", str), ("required", bool)):
            if e.get(key) is not None and not isinstance(e[key], want):
                warnings.append(f"config_files[{i}]: {key} must be {want.__name__}")
        out.append(entry)  # the template crossed the runner as JSON: plain data already
    return out, warnings


def _check_source(s: object) -> str | None:
    if not isinstance(s, dict) or not isinstance(s.get("source"), str) or not CODE_RE.match(s["source"]):
        return "source must be an id matching ^[a-z][a-z0-9_]{1,40}$"
    sid = s["source"]
    if ("dataset" in s) == ("datasets" in s):
        return f"{sid}: exactly one of dataset or datasets"
    multi = "datasets" in s
    ds = s["datasets"] if multi else [s["dataset"]]
    if not isinstance(ds, list) or not ds or not all(_one_of(d, DATASETS) for d in ds) or len(set(ds)) != len(ds):
        return f"{sid}: unknown or repeated dataset"
    if not _one_of(s.get("time_basis"), TIME_BASES):
        return f"{sid}: time_basis must be one of {sorted(TIME_BASES)}"
    if not _one_of(s.get("completeness"), COMPLETENESS):
        return f"{sid}: completeness must be one of {sorted(COMPLETENESS)}"
    scope = s.get("scope")
    if multi and not (isinstance(scope, dict) and set(scope) == set(ds)):
        return f"{sid}: scope must be a dict with one entry per dataset"
    full = s["completeness"] == "full_snapshot"
    for d in ds:
        keys = scope.get(d) if multi else scope
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
            return f"{sid}: scope of {d} must be a list of field names"
        if full and not keys:
            return f"{sid}: a full_snapshot scope cannot be empty"
        schema = load_schema(d)
        if schema and not set(keys) <= {k for k in schema["properties"] if not k.startswith("_")}:
            return f"{sid}: scope of {d} names a field outside the contract"
        if s.get("covers") == "all_active_books" and not ("book_id" in keys and set(keys) <= {"book_id", "entity"}):
            return f"{sid}: covers=all_active_books needs a scope of book_id (and entity)"
    if full and not _one_of(s.get("covers"), COVERS):
        return f"{sid}: covers must be one of {sorted(COVERS)}"
    if not full and s.get("covers") is not None:
        return f"{sid}: covers applies to full_snapshot only"
    if s.get("priority") is not None and type(s["priority"]) is not int:
        return f"{sid}: priority must be an int"
    return None


def load_adapter(spec: str | None, config_dir: str | None, expected_sha256: str | None,
                 timeout_s: float = DESCRIBE_TIMEOUT_S, inbox_dirs: list[str] | tuple[str, ...] = ()) -> AdapterHandle:
    """The adapter of ``DESK_PRIVATE_ADAPTER`` (None or "": the reference adapter). Never raises. ``inbox_dirs``:
    the report folders, given to every call as ``ctx.inbox_dirs``."""
    spec = spec or ""
    inbox_dirs = tuple(str(d) for d in inbox_dirs)
    base = AdapterHandle(spec, None, None, None, {}, None, config_dir, inbox_dirs=inbox_dirs)

    def fail(reason: str, **kw) -> AdapterHandle:
        return dataclasses.replace(base, error=reason, **kw)

    if config_dir and not Path(config_dir).is_dir():
        return fail("config_dir_missing: DESK_PRIVATE_ADAPTER_CONFIG is not a folder")

    def fp_of(location: dict | None = None) -> str | None:
        try:
            return fingerprint(spec, config_dir, location)
        except (OSError, ImportError, ValueError, TypeError):
            return None

    # A package directory is fingerprinted before any of its code runs. A module name only after describe:
    # the child reports the files it imported (its sys.path is not ours).
    # shortcut: a module adapter's describe() runs before DESK_PRIVATE_ADAPTER_SHA256 is checked (in the
    # stripped child, so it sees no secrets); upgrade if module adapters must be verified before import too.
    by_path = runner.is_path_spec(spec)
    fp = fp_of() if by_path else None
    if by_path and fp is None:
        return fail("not_found: the adapter package cannot be located")
    if fp and expected_sha256 and expected_sha256.lower() != fp:
        return fail("fingerprint_mismatch: DESK_PRIVATE_ADAPTER_SHA256 differs", fingerprint=fp)
    try:
        resp = runner.call_describe(spec or None, config_dir, timeout_s, inbox_dirs)
    except AdapterFailure as f:
        return fail(f"describe_{f.kind}: {f.type}", fingerprint=fp or fp_of())
    if not resp.get("ok"):
        detail = resp.get("type") or ", ".join(resp.get("missing", [])) or "-"
        return fail(f"{resp.get('load_error', 'load_failed')}: {detail}", fingerprint=fp or fp_of())
    r = resp["result"]
    if not by_path:
        fp = fp_of(r.get("location"))
        if fp is None:
            return fail("not_found: the adapter module's files cannot be located")
        if expected_sha256 and expected_sha256.lower() != fp:
            return fail("fingerprint_mismatch: DESK_PRIVATE_ADAPTER_SHA256 differs", fingerprint=fp)
    desc = r["describe"]
    try:
        why = check_describe(desc, r["spi_version"], r["has_reference"])
    except Exception as e:  # noqa: BLE001 - load_adapter never raises; a declaration we cannot even check
        why = f"bad_describe: {type(e).__name__}"
    if why:
        return fail(why, fingerprint=fp, describe=desc if isinstance(desc, dict) else {})
    return AdapterHandle(spec, desc["name"], desc["version"], fp, desc, None, config_dir,
                         tuple(check_config_files(desc)[1]), inbox_dirs)
