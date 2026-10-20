"""Credit ratings of the hub's ``ratings`` dataset (contract 1.1, HS5) on one desk scale (R9 D9).

The hub publishes each agency's rating verbatim: АКРА ``AA+(RU)``, Эксперт РА ``ruAA+``, НКР ``AA+.ru``,
НРА ``AA+|ru|`` (and the older Fitch ``AA+(rus)`` and Moody's Interfax ``Aa1.ru``). ``desk_grade`` maps a
national-scale rating to the desk's scale ``RATINGS`` (``AA+``), the order every page sorts by
(``api.compat`` ``RATING_ORDER``). ``position_rating`` picks the best national-scale rating of the bond itself,
else the best of its issuer. Nothing is inferred: an international-scale rating, a withdrawn one and a value that
is not a grade (``Withdrawn``, ``приостановлен``) are not used.

What a position shows when there is no grade: ``NO_DATA`` when the snapshot carries no ratings at all (a 1.0
snapshot, or an empty dataset: no source, so "unrated" would be a claim we cannot make), ``NO_RATING`` when the hub
has ratings and none applies to this bond.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from .adapter import BondTerms, Rating, StaticView

RATINGS = ("AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-",
           "CCC+", "CCC", "CCC-", "CC", "C", "RD", "SD", "D")
"""The desk scale, best first (the agencies' national scales share these grades; ``RD``/``SD`` a restricted or
selective default)."""
NO_DATA = "нет данных"
NO_RATING = "без рейтинга"

_NATIONAL = re.compile(r"^ru(?=[A-Z])|\((?:RU|rus)\)$|\.ru$|\|ru\|$", re.IGNORECASE)
_GRADE = re.compile(r"^(AAA|AA|A|BBB|BB|B|CCC|CC|C|RD|SD|D)([+-])?$")
_MOODYS = re.compile(r"^(Aaa|Aa|A|Baa|Ba|B|Caa|Ca|C)([123])?$")
_MOODYS_GRADE = {"Aaa": "AAA", "Aa": "AA", "A": "A", "Baa": "BBB", "Ba": "BB", "B": "B", "Caa": "CCC", "Ca": "CC",
                 "C": "C"}
_NATIONAL_SCALE = re.compile(r"(?<!inter)national|национальн|\bru\b|\bрф\b", re.IGNORECASE)
_WITHDRAWN = re.compile(r"withdraw|отозван|снят", re.IGNORECASE)


def desk_grade(rating: str | None) -> str | None:
    """The desk grade (``RATINGS``) of a national-scale rating in any agency's notation, else None.
    ``ruAA+``, ``AA+(RU)``, ``AA+.ru``, ``AA+|ru|`` -> ``AA+``; ``Aa1.ru`` -> ``AA+``; ``BBB-`` -> ``BBB-``."""
    if not rating:
        return None
    s = _NATIONAL.sub("", rating.strip()).strip()
    if m := _GRADE.match(s):
        g = m[1] + (m[2] or "")
        return g if g in RATINGS else m[1]
    if m := _MOODYS.match(s):
        base = _MOODYS_GRADE[m[1]]
        if not m[2] or base in ("AAA", "CC", "C"):
            return base
        return base + {"1": "+", "2": "", "3": "-"}[m[2]]
    return None


def rating_rank(r: str | None) -> int:
    """Sort key of a rating as a page shows it: the desk grade's place, unknown values after, no grade last."""
    g = desk_grade(r)
    return RATINGS.index(g) if g is not None else len(RATINGS) + (0 if r and r not in (NO_DATA, NO_RATING) else 1)


def is_national(r: Rating) -> bool:
    """A national-scale (Russia) rating: its notation says so (``ru``, ``(RU)``, ``.ru``, ``|ru|``), or the scale."""
    return bool(r.rating and (_NATIONAL.search(r.rating.strip()) or _NATIONAL_SCALE.search(r.scale or "")))


def usable(r: Rating) -> bool:
    return is_national(r) and desk_grade(r.rating) is not None and not _WITHDRAWN.search(r.action or "")


@dataclass(frozen=True)
class PositionRating:
    grade: str
    """``RATINGS`` grade, else ``NO_RATING`` / ``NO_DATA``."""
    agency: str | None = None
    rating: str | None = None
    """The agency's rating verbatim."""
    scope: str | None = None
    rating_date: date | None = None

    def extra(self) -> dict[str, str] | None:
        if self.rating is None:
            return None
        return {"agency": self.agency or "", "rating": self.rating, "scope": self.scope or "",
                **({"date": self.rating_date.isoformat()} if self.rating_date else {})}


def best(rows: Iterable[Rating]) -> PositionRating | None:
    """The best usable rating of ``rows`` (the latest of equal grades)."""
    rs = [r for r in rows if usable(r)]
    if not rs:
        return None
    r = min(rs, key=lambda r: (RATINGS.index(desk_grade(r.rating)),  # type: ignore[arg-type]
                               -(r.rating_date or date.min).toordinal(), r.agency))
    return PositionRating(desk_grade(r.rating), r.agency, r.rating, r.scope, r.rating_date)  # type: ignore[arg-type]


def issuer_rating(view: StaticView, issuer_id: int | None) -> PositionRating:
    """The issuer's own best national-scale rating (``scope`` issuer)."""
    if view.ratings is None or not view.ratings:
        return PositionRating(NO_DATA)
    found = best(r for r in view.ratings if r.scope == "issuer" and issuer_id is not None and r.issuer_id == issuer_id)
    return found or PositionRating(NO_RATING)


def position_rating(view: StaticView, terms: BondTerms) -> PositionRating:
    """The best national-scale rating of the issue, else of its issuer (D9); ``NO_DATA`` without ratings in the
    snapshot, ``NO_RATING`` when none applies."""
    if view.ratings is None or not view.ratings:
        return PositionRating(NO_DATA)
    issue = best(r for r in view.ratings if r.scope == "issue" and r.secid == terms.secid)
    return issue or issuer_rating(view, terms.issuer_id)
