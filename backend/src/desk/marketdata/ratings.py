"""The desk's rating view of the hub's ``ratings`` dataset (static contract 1.1+, HS5; 1.3 adds issuer scope and the
agencies' own sources): per bond and per issuer the best current national-scale rating of the Russian agencies
(АКРА, Эксперт РА, НКР, НРА), with its agency, the rating as the agency writes it, its date and outlook, and the
other agencies' ratings beside it.

Rules (the desk's, written down here because the hub publishes the ratings verbatim and infers nothing):

* **Which rows count.** The four agencies of ``AGENCIES`` (by any name the sources give them), on a national scale
  (``scale`` names a national scale, or ``national_ru``; an international scale of the same agency is left out),
  not withdrawn (``rating`` null, ``Withdrawn`` / ``отозван``, or ``action = withdrawn``), and a rating that reads as a
  letter grade. A structured-finance rating (``ruAAA.sf``, ``AAA(ru.sf)``) counts and is marked ``sf``.
* **Grade.** The hub's ``grade`` (1.3) when present, else the same rule the hub documents
  (``mdhub.sources.ratings_common.normalize_grade``): the agency's scale marks dropped (``AA-(RU)`` АКРА,
  ``ruAA-`` Эксперт РА, ``AA-.ru`` НКР, ``AA-|ru|`` НРА, ``.sf``), Cyrillic look-alikes and dashes mapped to ASCII:
  ``AAA`` ... ``D`` (``GRADES``).
* **Per bond** (``RatingBook.bond``): the bond's own (issue) ratings; without one the issuer's ratings (issuer scope,
  1.3); without those the issuer's level by its bonds (below). The best grade of the agencies is the desk rating, the
  newest rating date breaks a tie. ``basis`` says which: ``issue`` / ``issuer`` / ``issuer_bonds``.
* **Per issuer** (``RatingBook.issuer``): the issuer-scope ratings (1.3), else its level by its bonds: the grade most
  of its rated bonds have (each bond's best), the higher on a tie (subordinated and guaranteed issues are the
  exception, not the level).
* **Buckets** (``bucket``): ``AAA``, ``AA`` (AA+/AA/AA-), ``A``, ``BBB``, ``≤BB`` (BB+ and below, also C/RD/D), ``б/р``
  (no current rating in the data).

``NOT_RATED`` (``б/р``) is what an issuer or bond without a rating shows when the static has ratings at all;
``NO_DATA`` (``нет данных``) when the static has no ``ratings`` rows (contract 1.0, or the hub's source blocked):
then nobody is "not rated", the data is missing.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

GRADES = ("AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-",
          "CCC+", "CCC", "CCC-", "CC", "C", "RD", "SD", "D")
"""National-scale letter grades, best first (the order of the pages' rating axes)."""
BUCKETS = ("AAA", "AA", "A", "BBB", "≤BB", "б/р")
NOT_RATED = "б/р"
NO_DATA = "нет данных"
AGENCIES = {"акра": "АКРА", "acra": "АКРА", "эксперт ра": "Эксперт РА", "raex": "Эксперт РА",
            "raexpert": "Эксперт РА", "ra expert": "Эксперт РА", "рейтинговое агентство эксперт ра": "Эксперт РА",
            "нкр": "НКР", "nkr": "НКР", "нра": "НРА", "nra": "НРА"}
"""The agencies of the desk's view, by the names the hub's sources give them -> the display name."""
AGENCY_ORDER = ("АКРА", "Эксперт РА", "НКР", "НРА")

_LOOKALIKE = str.maketrans({"А": "A", "В": "B", "С": "C", "Д": "D", "а": "A", "в": "B", "с": "C", "–": "-", "—": "-",
                            "−": "-", "‐": "-", "‑": "-", "＋": "+"})
_MARKS = re.compile(r"\((?:RU|RU\.SF|SF)\)|\|RU\|(?:\.SF)?|\.RU(?:\.SF)?|\.SF|\(EXP\)\s*$")
_GRADE = re.compile(r"^(AAA|AA|A|BBB|BB|B|CCC|CC|C|RD|SD|D)([+-]?)$")
_WITHDRAWN = ("withdrawn", "отозван", "снят")


def agency(name: str | None) -> str | None:
    """The display name of one of the desk's agencies, None for another agency (S&P, Fitch, ...)."""
    if not name:
        return None
    k = " ".join(name.replace("«", "").replace("»", "").replace('"', "").split()).lower()
    return AGENCIES.get(k)


def grade(rating: str | None) -> str | None:
    """A rating as the agency writes it -> its letter grade (``GRADES``), None when it is not one (withdrawn,
    expected, ESG, a score)."""
    if not rating:
        return None
    s = rating.strip().translate(_LOOKALIKE).upper().replace(" ", "")
    s = s.removeprefix("RU")
    s = _MARKS.sub("", s)
    m = _GRADE.match(s)
    if not m:
        return None
    g = m[1] + m[2]
    return g if g in GRADES else m[1]


def rank(g: str | None) -> int:
    """Position of a grade in ``GRADES`` (0 the best); unrated last."""
    return GRADES.index(g) if g in GRADES else len(GRADES)


def bucket(g: str | None) -> str:
    """The desk's bucket of a grade (``BUCKETS``)."""
    if not g or g not in GRADES:
        return NOT_RATED
    if g == "AAA":
        return "AAA"
    for b in ("AA", "A"):
        if g in (b, b + "+", b + "-"):
            return b
    if g.startswith("BBB"):
        return "BBB"
    return "≤BB"


def _national(scale: str | None) -> bool:
    s = (scale or "").lower()
    if not s:
        return True  # a source that names no scale: the agencies' national lists (1.3 agency rows carry national_ru)
    return "national" in s or "национальн" in s or "структ" in s or "российск" in s


def _sf(row: Mapping[str, Any]) -> bool:
    r = (row.get("rating") or "").lower()
    return ".sf" in r or "(sf)" in r or "структ" in (row.get("scale") or "").lower()


def current(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """One ``ratings`` row as the desk counts it (module doc), or None when it does not count."""
    ag = agency(row.get("agency"))
    if ag is None or not _national(row.get("scale")):
        return None
    r = row.get("rating")
    if r is None or (row.get("action") or "").lower() == "withdrawn" or any(w in r.lower() for w in _WITHDRAWN):
        return None
    g = row.get("grade") or grade(r)
    if g not in GRADES:
        return None
    return {"agency": ag, "rating": r, "grade": g, "date": row.get("rating_date"), "outlook": row.get("outlook"),
            "watch": row.get("watch"), "sf": _sf(row)}


def _best(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The best of one object's ratings (one per agency: the agency's newest), with the others beside it."""
    if not rows:
        return None
    per: dict[str, dict[str, Any]] = {}
    for r in rows:
        o = per.get(r["agency"])
        if o is None or (r["date"] or "") > (o["date"] or "") or ((r["date"] or "") == (o["date"] or "")
                                                                 and rank(r["grade"]) < rank(o["grade"])):
            per[r["agency"]] = r
    alls = sorted(per.values(), key=lambda r: (rank(r["grade"]), -int((r["date"] or "0000-00-00").replace("-", "")),
                                               AGENCY_ORDER.index(r["agency"])))
    b = alls[0]
    return {"grade": b["grade"], "agency": b["agency"], "rating": b["rating"], "date": b["date"],
            "outlook": b["outlook"], "watch": b["watch"], "sf": b["sf"], "bucket": bucket(b["grade"]),
            "agencies": [[r["agency"], r["rating"], r["grade"], r["date"], r["outlook"]] for r in alls]}


class RatingBook:
    """The desk's rating view of one static snapshot's ``ratings`` (module doc). ``bond_issuer``: secid -> issuer id
    of the static's instruments (an issuer's level by its bonds)."""

    def __init__(self, rows: Iterable[Mapping[str, Any]], bond_issuer: Mapping[str, int | None]) -> None:
        self.rows = 0
        issue: dict[str, list[dict[str, Any]]] = defaultdict(list)
        issuer: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            self.rows += 1
            c = current(row)
            if c is None:
                continue
            if row.get("scope") == "issuer":
                if row.get("issuer_id") is not None:
                    issuer[int(row["issuer_id"])].append(c)
            elif row.get("secid"):
                issue[row["secid"]].append(c)
        self._issue = {s: b for s, rs in issue.items() if (b := _best(rs))}
        self._issuer = {i: b for i, rs in issuer.items() if (b := _best(rs))}
        by_bonds: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for s, b in self._issue.items():
            i = bond_issuer.get(s)
            if i is not None and not b["sf"]:
                by_bonds[int(i)].append(b)
        self._by_bonds: dict[int, dict[str, Any]] = {}
        for i, bs in by_bonds.items():
            n = Counter(b["grade"] for b in bs)
            g = min(n, key=lambda x: (-n[x], rank(x)))
            pick = max((b for b in bs if b["grade"] == g), key=lambda b: b["date"] or "")
            self._by_bonds[i] = {**pick, "bonds": len(bs), "bonds_at_grade": n[g]}
        self.bond_issuer = bond_issuer

    @property
    def has_data(self) -> bool:
        """The static has ``ratings`` rows at all: an object without a rating is then ``NOT_RATED``, not ``NO_DATA``."""
        return self.rows > 0

    def issuer(self, issuer_id: int | None) -> dict[str, Any] | None:
        if issuer_id is None:
            return None
        if (b := self._issuer.get(int(issuer_id))) is not None:
            return {**b, "basis": "issuer"}
        if (b := self._by_bonds.get(int(issuer_id))) is not None:
            return {**b, "basis": "issuer_bonds"}
        return None

    def bond(self, secid: str | None, issuer_id: int | None = None) -> dict[str, Any] | None:
        """The bond's desk rating (module doc), None when neither it nor its issuer is rated."""
        if secid and (b := self._issue.get(secid)) is not None:
            return {**b, "basis": "issue"}
        if issuer_id is None and secid:
            issuer_id = self.bond_issuer.get(secid)
        return self.issuer(issuer_id)

    def label(self, r: dict[str, Any] | None) -> str:
        """The grade a page shows: the grade, ``NOT_RATED`` or ``NO_DATA``."""
        if r is not None:
            return r["grade"]
        return NOT_RATED if self.has_data else NO_DATA

    def coverage(self, secids: Iterable[str]) -> dict[str, Any]:
        """How many of ``secids`` have a desk rating, by basis."""
        n, by = 0, Counter()
        for s in secids:
            n += 1
            r = self.bond(s)
            by[r["basis"] if r else "none"] += 1
        return {"bonds": n, "rated": n - by["none"], "by_basis": dict(by), "rows": self.rows,
                "issuers_rated": len(self._issuer.keys() | self._by_bonds.keys())}


BASIS_TEXT = {"issue": "рейтинг выпуска", "issuer": "рейтинг эмитента", "issuer_bonds": "уровень эмитента по выпускам"}


def compact(r: dict[str, Any] | None) -> dict[str, Any] | None:
    """A desk rating for a run's fact or a page: the fields the pages show (no None values)."""
    if r is None:
        return None
    keys = ("grade", "agency", "rating", "date", "outlook", "watch", "bucket", "basis", "agencies")
    out = {k: r[k] for k in keys if r.get(k) is not None}
    if r.get("sf"):
        out["sf"] = True
    return out


def position_rating(fact: Mapping[str, Any], issuer_ref: Mapping[str, Any] | None) -> str | None:
    """The rating a position shows and a scenario's rating rule matches: the bond's desk rating (``extra.rating``,
    VALUATION_VERSION 8), else its issuer's (the run's issuer reference), else None."""
    r = ((fact.get("extra") or {}).get("rating") or {}) if isinstance(fact.get("extra"), dict) else {}
    if r.get("grade"):
        return r["grade"]
    return (issuer_ref or {}).get("rating")
