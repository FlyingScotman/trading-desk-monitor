"""The GitHub Release's extra assets, for ``.github/workflows/release.yml`` (and by hand):

    python backend/tools/release_assets.py check-tag --tag v0.2.0
    python backend/tools/release_assets.py write --dist dist --tag v0.2.0 [--repo owner/name] [--commit SHA]

``check-tag``: the tag must be ``v<backend version>`` (``backend/pyproject.toml``): the in-app updater orders
releases by that version and the commit date (``desk.update.compare``), so every release bumps the version.

``write``: checks the zips ``build_windows_bundle.py`` left in ``--dist`` (exactly one full
``DeskMonitor-<version>.zip`` of the tag's version, patches to that version, none with the private adapter: a public
release never carries it; each zip reads as an update zip, ``desk.update.read_contents``) and writes next to them
``SHA256SUMS``, ``release.json`` (``desk.updater.release.release_doc``: what the updater reads first) and
``NOTES.md`` (the release notes, the checksums included: the browser reads them through the API). The notes start
with the version's section of ``CHANGELOG.md`` (``## <backend version>``) when it has one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "src"))

from desk import update
from desk.updater import release


def backend_version() -> str:
    return tomllib.loads((REPO / "backend/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def core_pin() -> str:
    project = tomllib.loads((REPO / "backend/pyproject.toml").read_text(encoding="utf-8"))
    pin = next(d for d in project["project"]["dependencies"] if d.startswith("rub-fixed-income-core=="))
    src = project["tool"]["uv"]["sources"]["rub-fixed-income-core"]
    return f"{pin} @ {src.get('git', '')}@{src.get('rev', '')}"


def check_tag(tag: str, version: str | None = None) -> str:
    want = "v" + (version or backend_version())
    if not tag.startswith("v"):
        raise SystemExit(f"tag {tag!r} has no 'v': a release tag is {want!r}. A tag or a release made without the 'v' "
                         "publishes no update files and the in-app updater skips it; delete it and push the v-tag")
    if tag != want:
        raise SystemExit(f"tag {tag!r}: a release tag must be {want!r} (backend/pyproject.toml); bump the version "
                         "there, commit, then tag")
    return tag


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(dist: Path, tag: str) -> tuple[str, list[tuple[str, int, str]]]:
    """(the release's bundle version, [(name, size, sha256)] of its zips), every zip checked."""
    zips = sorted(p for p in dist.glob("DeskMonitor-*.zip") if p.is_file())
    version, files = None, []
    for p in zips:
        kind = release.classify_name(p.name)
        if kind is None:
            raise SystemExit(f"{p.name}: not a DeskMonitor update zip name")
        if p.name.endswith("-with-adapter.zip"):
            raise SystemExit(f"{p.name}: a bundle with the private adapter is never published")
        with update.open_zip(p) as z:
            c = update.read_contents(z)
        if c.adapter:
            raise SystemExit(f"{p.name}: holds adapter/ (the private adapter): never published")
        if c.version != kind[1] or c.kind != kind[0]:
            raise SystemExit(f"{p.name}: its VERSION / kind is {c.version} / {c.kind}")
        if kind[0] == "full":
            if version is not None:
                raise SystemExit("more than one full bundle in --dist")
            version = c.version
        files.append((p.name, p.stat().st_size, sha256(p)))
    if version is None:
        raise SystemExit(f"no full DeskMonitor-<version>.zip in {dist}")
    if not version.startswith(tag.removeprefix("v") + "-"):
        raise SystemExit(f"the bundle is {version}, the tag {tag}")
    stray = [n for n, _, _ in files if release.classify_name(n)[1] != version]
    if stray:
        raise SystemExit(f"zips of another version in --dist: {', '.join(stray)}")
    return version, files


NOTES = """\
Desk Monitor {version}

{changes}Установка из программы: Настройки → «Обновление программы» → «Обновить до …» (программа сама находит новую версию,
скачивает архив сервером или браузером, сверяет SHA-256 и перезапускается). Вручную: скачайте
DeskMonitor-{version}.zip, распакуйте (INSTALL.txt внутри).

- Приватный адаптер в этот архив не входит: при обновлении установленный адаптер сохраняется.
- Ядро расчётов: {core}.
- Сборка из коммита {commit}.

SHA-256:

```text
{sums}```
"""


def changelog(version: str, path: Path = REPO / "CHANGELOG.md") -> str:
    """The body of the ``## <version>`` section of ``path`` (up to the next ``## ``), stripped; "" without one."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    out: list[str] | None = None
    for ln in lines:
        if ln.startswith("## "):
            if out is not None:
                break
            if ln[3:].split()[:1] == [version]:
                out = []
        elif out is not None:
            out.append(ln)
    return "\n".join(out or []).strip()


def write(dist: Path, tag: str, repo: str | None, commit: str | None) -> dict:
    version, files = collect(dist, tag)
    sums = release.sums_text(files)
    core = core_pin()
    changes = changelog(tag.removeprefix("v"))
    notes = NOTES.format(version=version, core=core, commit=commit or "?", sums=sums,
                         changes=changes + "\n\n---\n\n" if changes else "")
    doc = release.release_doc(tag, version, files, commit=commit, core=core,
                              published_at=datetime.now(UTC).isoformat(timespec="seconds"),
                              notes=f"Desk Monitor {version}" + (f" ({repo})" if repo else ""))
    (dist / release.SUMS_NAME).write_text(sums, encoding="utf-8")
    (dist / release.RELEASE_JSON).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (dist / "NOTES.md").write_text(notes, encoding="utf-8")
    for name, size, sha in files:
        print(f"{sha}  {name}  ({size / 1e6:.1f} MB)")
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check-tag")
    c.add_argument("--tag", required=True)
    w = sub.add_parser("write")
    w.add_argument("--dist", type=Path, required=True)
    w.add_argument("--tag", required=True)
    w.add_argument("--repo")
    w.add_argument("--commit")
    a = ap.parse_args(argv)
    if a.cmd == "check-tag":
        print(check_tag(a.tag))
    else:
        check_tag(a.tag)
        write(a.dist, a.tag, a.repo, a.commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
