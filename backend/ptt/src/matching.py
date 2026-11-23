"""Match a disclosure filer to a bioguide id.

Filings identify a member by name plus a state/district code ("MO04"); the
legislator roster keys on bioguide id.

**District alone is not enough, and assuming it was cost 6,373 transactions.**
A seat changes hands every couple of years and the roster lists only *current*
members, so keying on the seat handed Donna Shalala's 567 filings to Maria
Elvira Salazar, who merely holds FL27 now - and did the same for Lowenthal into
Dave Min, Langevin into Magaziner, Yarmuth into McGarvey, and twenty more. Eight
of the site's tracked members were blends of two different people.

So a district match must be confirmed by the name. That in turn needs surname
comparison to actually work, which it did not: the old check compared the
filer's last *token* against the roster's whole ``last_name`` string, so "April
McClain Delaney" never matched surname "McClain Delaney" and fell through to the
seat-only fallback. Accents did the same to "Sanchez" against "Sánchez".

Filings from members who have since left Congress now resolve to ``None``, which
is the documented intent: their trades stay in the data and stay out of the
leaderboard.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from functools import lru_cache

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "md", "phd", "dds", "esq",
            "facs", "dr"}
HONORIFICS = {"hon", "mr", "mrs", "ms", "dr", "rep", "sen", "the", "honorable"}


def fold_accents(text: str) -> str:
    """'Sánchez' -> 'sanchez'. Filings are ASCII; the roster is not."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", text or "")
                   if not unicodedata.combining(ch))


def normalize_name(name: str) -> str:
    """Lowercase, fold accents, drop honorifics, suffixes and middle initials."""
    name = re.sub(r"[^\w\s]", " ", fold_accents(name).lower())
    tokens = [t for t in name.split() if t]
    tokens = [t for t in tokens if t not in HONORIFICS and t not in SUFFIXES]
    # Drop single-letter middle initials.
    tokens = [t for t in tokens if len(t) > 1]
    return " ".join(tokens)


def name_parts(name: str) -> tuple[str, str]:
    tokens = normalize_name(name).split()
    if not tokens:
        return "", ""
    return tokens[0], tokens[-1]


def surname_matches(filer_name: str, legislator_last: str) -> bool:
    """Do these two refer to the same family name?

    Compound surnames are the whole difficulty: the roster stores "McClain
    Delaney" and "Wasserman Schultz" as one field while a filing may print
    either part last. Any shared token counts, which is deliberately generous -
    the caller has already narrowed to a single seat, so the job here is to
    reject a *different person*, not to be strict about form.
    """
    leg_tokens = set(normalize_name(legislator_last).split())
    filer_tokens = set(normalize_name(filer_name).split())
    if not leg_tokens or not filer_tokens:
        return False
    return bool(leg_tokens & filer_tokens)


def _is_former(row) -> bool:
    """True for a member who has left Congress.

    Guarded with a key check rather than ``row["is_former"]`` because the column
    is added by migration, and a database built before that migration must still
    open rather than raising on every roster read.
    """
    try:
        return bool(row["is_former"])
    except (IndexError, KeyError):
        return False


class MemberMatcher:
    """Resolves (name, state_district) pairs to bioguide ids, with caching."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        rows = conn.execute("SELECT * FROM legislators").fetchall()

        # Two separate indexes, and the order they are consulted is the whole
        # safety property. Former members share seats and surnames with sitting
        # ones - Donna Shalala and Maria Elvira Salazar both held FL27 - so
        # putting them in one pool would let a departed member win a filing that
        # belongs to the person who replaced them. That is precisely the bug
        # that misattributed 6,373 transactions before.
        #
        # So the current roster is resolved on its own first, exactly as it was
        # when it was the only roster. Former members are consulted only for
        # filings that matched nobody serving, which means adding them can
        # attribute an orphan but can never move an existing match.
        self._current = self._index(r for r in rows if not _is_former(r))
        self._former = self._index(r for r in rows if _is_former(r))
        self._rows = [r for r in rows if not _is_former(r)]
        self._all_rows = rows

    @staticmethod
    def _index(rows) -> dict:
        by_district: dict[str, list[sqlite3.Row]] = {}
        by_last: dict[str, list[sqlite3.Row]] = {}
        kept = []
        for r in rows:
            kept.append(r)
            sd = (r["state_district"] or "").upper()
            if sd:
                by_district.setdefault(sd, []).append(r)
            # Indexed per surname token, so "Wasserman Schultz" is findable
            # under either half - a filing may print only one of them.
            for token in set(normalize_name(r["last_name"] or "").split()):
                by_last.setdefault(token, []).append(r)
        return {"district": by_district, "last": by_last, "rows": kept}

    @lru_cache(maxsize=4096)
    def _resolve(self, name: str, state_district: str) -> str | None:
        """Sitting members first, then - only for orphans - former members."""
        found = self._resolve_in(self._current, name, state_district)
        if found:
            return found
        return self._resolve_in(self._former, name, state_district)

    def _resolve_in(self, index: dict, name: str,
                    state_district: str) -> str | None:
        sd = (state_district or "").upper().strip()
        first, last = name_parts(name)
        by_district, by_last = index["district"], index["last"]
        seat = by_district.get(sd, [])

        # 1. District match, confirmed by surname. Token-wise, so a compound
        #    roster surname ("McClain Delaney") still matches a filing that
        #    prints only part of it.
        for r in seat:
            if surname_matches(name, r["last_name"] or ""):
                return r["bioguide_id"]

        # 2. Same seat, same first name, different surname - a member who
        #    married or changed how they file. "Ashley Hinson Arenholz" and
        #    "Ashley Hinson" are one person; "Donna Shalala" and "Maria Elvira
        #    Salazar" are not, and only a name check can tell them apart.
        for r in seat:
            if first and normalize_name(r["first_name"] or "").split()[:1] == [first]:
                return r["bioguide_id"]

        # 2b. Redistricting moves sitting members between seat numbers: Ashley
        #     Hinson filed from IA01 and now holds IA02, so her old filings
        #     land on a seat someone else occupies. A surname match that is
        #     unique *within the same state* is still her. Garret Graves of
        #     Louisiana finds no Graves in Louisiana and stays unmatched, which
        #     is the point - this must not reach across state lines.
        #     Keyed on *any* token of the filed name, because the filing may
        #     carry a married surname the roster does not: "Ashley Hinson
        #     Arenholz" ends in a token the roster has never heard of, while
        #     "Hinson" sits in the middle.
        state = sd[:2]
        filer_tokens = set(normalize_name(name).split())
        same_state = {r["bioguide_id"]: r
                      for token in filer_tokens
                      for r in by_last.get(token, [])
                      if (r["state"] or "").upper() == state
                      and (r["chamber"] or "") == "house"}
        if len(same_state) == 1:
            return next(iter(same_state.values()))["bioguide_id"]

        # If the seat has a known current holder and the name matches nothing
        # above, this is a former member. Stop here rather than casting about
        # for a surname elsewhere in the country: falling through sent Garret
        # Graves of Louisiana to Sam Graves of Missouri, and Van Taylor of
        # Texas to David Taylor of Ohio.
        if seat:
            return None

        # 3. No usable district. Fall back to surname within the state - House
        #    only, since this resolver is for filings that carry a district.
        candidates = [r for r in by_last.get(last, [])
                      if (r["state"] or "").upper() == state
                      and (r["chamber"] or "") == "house"]
        if len(candidates) == 1:
            return candidates[0]["bioguide_id"]
        for r in candidates:
            if normalize_name(r["first_name"] or "").startswith(first[:4]) and first:
                return r["bioguide_id"]

        # 4. ...then a globally unique surname, restricted to the chamber that
        #    files with a district at all.
        globally = [r for r in by_last.get(last, [])
                    if (r["chamber"] or "") == "house"]
        if len(globally) == 1:
            return globally[0]["bioguide_id"]

        return None

    def resolve(self, name: str, state_district: str) -> str | None:
        return self._resolve(name or "", state_district or "")

    @lru_cache(maxsize=2048)
    def resolve_senator(self, first: str, last: str, state: str = "") -> str | None:
        """Match a Senate filer, who has no district to key on.

        The Senate's disclosure search returns a name and an office string but
        no state, so surname within the Senate does most of the work - there
        are only a hundred of them, and collisions are rare. State is used when
        the caller can supply it.
        """
        last_n, first_n = normalize_name(last), normalize_name(first)
        if not last_n:
            return None

        current = [r for r in self._rows if (r["chamber"] or "") == "senate"]
        found = self._senator_in(current, first_n, last_n, first, last, state)
        if found:
            return found
        former = [r for r in self._all_rows
                  if (r["chamber"] or "") == "senate" and _is_former(r)]
        return self._senator_in(former, first_n, last_n, first, last, state)

    def _senator_in(self, senators: list, first_n: str, last_n: str,
                    first: str, last: str, state: str) -> str | None:
        matches = [r for r in senators
                   if normalize_name(r["last_name"] or "") == last_n]

        if state:
            by_state = [r for r in matches if (r["state"] or "").upper() == state.upper()]
            if len(by_state) == 1:
                return by_state[0]["bioguide_id"]
            matches = by_state or matches

        if len(matches) == 1:
            return matches[0]["bioguide_id"]

        # Same surname in the Senate: fall back to the given name.
        if first_n:
            for r in matches:
                roster_first = normalize_name(r["first_name"] or "")
                if roster_first == first_n or roster_first.startswith(first_n[:4]) \
                        or first_n.startswith(roster_first[:4]):
                    return r["bioguide_id"]

        # Last resort: the full name as printed on the filing.
        full = normalize_name(f"{first} {last}")
        for r in senators:
            if normalize_name(r["full_name"] or "") == full:
                return r["bioguide_id"]
        return None
