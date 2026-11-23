"""Legislator roster, committee assignments, and information-access scoring.

Source: the ``unitedstates/congress-legislators`` public dataset, which is the
canonical machine-readable roster of current members and committee membership.

The *sensitivity* score below is a stated editorial judgement, not a measured
quantity.  It ranks how much market-moving non-public information a committee
seat plausibly exposes a member to: which industries the committee writes law
for, whether it receives classified or pre-release briefings, and whether it
directs federal money.  It is used to sort and contextualise the trade data -
never as evidence that any particular trade used such information.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import requests

BASE = "https://unitedstates.github.io/congress-legislators/"
FILES = {
    "legislators": "legislators-current.json",
    "committees": "committees-current.json",
    "membership": "committee-membership-current.json",
    # Everyone who has ever served, back to 1789. Only the handful whose service
    # overlaps the disclosure era is loaded - see :func:`load_former`.
    "legislators_historical": "legislators-historical.json",
}

# Electronic PTR filings begin in 2019 in this dataset, so a member who left
# before then has no trades here and would only add surname collisions.
FORMER_SINCE = "2019-01-01"

# thomas_id -> (sensitivity 0-1, sectors whose prices the committee can move)
COMMITTEE_SENSITIVITY: dict[str, tuple[float, tuple[str, ...]]] = {
    # --- Direct authority over financial markets and tax ---
    "HSBA": (1.00, ("financials", "real_estate", "crypto")),      # House Financial Services
    "SSBK": (1.00, ("financials", "real_estate", "crypto")),      # Senate Banking
    "HSWM": (0.95, ("healthcare", "financials", "industrials")),  # House Ways and Means
    "SSFI": (0.95, ("healthcare", "financials", "industrials")),  # Senate Finance
    # --- Broad industrial jurisdiction / classified access / the purse ---
    "HSIF": (0.95, ("healthcare", "technology", "energy", "telecom")),  # Energy & Commerce
    "HLIG": (0.90, ("defense", "technology")),                    # House Intelligence
    "SLIN": (0.90, ("defense", "technology")),                    # Senate Intelligence
    "HSAP": (0.85, ("defense", "healthcare", "industrials")),     # House Appropriations
    "SSAP": (0.85, ("defense", "healthcare", "industrials")),     # Senate Appropriations
    # --- Sector-specific but powerful ---
    "HSAS": (0.80, ("defense", "aerospace")),                     # House Armed Services
    "SSAS": (0.80, ("defense", "aerospace")),                     # Senate Armed Services
    "SSCM": (0.80, ("technology", "telecom", "transport")),       # Senate Commerce
    "SSHR": (0.75, ("healthcare", "pharma")),                     # Senate HELP
    "HSJU": (0.70, ("technology", "telecom")),                    # House Judiciary (antitrust)
    "SSJU": (0.70, ("technology", "telecom")),                    # Senate Judiciary
    "SSEG": (0.70, ("energy", "utilities")),                      # Senate Energy & Natural Res.
    "HSSY": (0.65, ("technology", "aerospace")),                  # House Science & Technology
    "HSPW": (0.65, ("industrials", "transport", "materials")),    # Transportation & Infra.
    "SSEV": (0.60, ("energy", "utilities", "materials")),         # Senate Environment
    "HSAG": (0.60, ("agriculture", "commodities")),               # House Agriculture (CFTC)
    "SSAF": (0.60, ("agriculture", "commodities")),               # Senate Agriculture
    "HSHM": (0.55, ("defense", "technology")),                    # Homeland Security
    "SSGA": (0.50, ("defense", "technology")),                    # Senate Homeland/Gov Affairs
    # --- Oversight and policy, less direct market authority ---
    "HSII": (0.50, ("energy", "materials")),                      # Natural Resources
    "HSFA": (0.45, ("defense", "energy")),                        # Foreign Affairs
    "SSFR": (0.45, ("defense", "energy")),                        # Senate Foreign Relations
    "HSGO": (0.40, ()),                                           # Oversight
    "HSSM": (0.35, ()),                                           # Small Business
    "SSSB": (0.35, ()),                                           # Senate Small Business
    "HSBU": (0.35, ("financials",)),                              # House Budget
    "SSBU": (0.35, ("financials",)),                              # Senate Budget
    "HSED": (0.30, ()),                                           # Education & Workforce
    "HSVR": (0.25, ()),                                           # Veterans' Affairs
    "SSVA": (0.25, ()),                                           # Senate Veterans' Affairs
    "SSRA": (0.25, ()),                                           # Senate Rules
    "HSRU": (0.25, ()),                                           # House Rules
    "HSHA": (0.20, ()),                                           # House Administration
    "SLIA": (0.20, ("energy", "materials")),                      # Indian Affairs (gaming, land, minerals)
    "HSSO": (0.10, ()),                                           # Ethics
    "SLET": (0.10, ()),                                           # Senate Ethics

    # --- Joint committees ---
    # Joint Taxation is the scorekeeper for every tax bill: its staff price
    # legislation before it is public, which is real advance knowledge of
    # measures that move whole sectors.
    "JSTX": (0.90, ("financials", "healthcare", "industrials")),
    "JSEC": (0.45, ("financials",)),                              # Joint Economic
    "JSLC": (0.10, ()),                                           # Library
    "JSPR": (0.10, ()),                                           # Printing
    "JCSE": (0.30, ("defense", "energy")),                        # Security & Co-op in Europe

    # --- Select committees ---
    # The China competition panel takes classified briefings on export
    # controls, semiconductors and supply chains - among the most directly
    # market-moving subject matter in Congress right now.
    "HSZS": (0.85, ("technology", "defense", "industrials")),
    "HSQJ": (0.10, ()),                                           # Jan 6 select subcommittee
    "SPAG": (0.45, ("healthcare", "pharma")),                     # Senate Aging
    "SCNC": (0.20, ()),                                           # Narcotics caucus
}

# --------------------------------------------------------------------------
# Leadership access
# --------------------------------------------------------------------------
# House leadership sits on no standing committees, so a jurisdiction-based
# remit misses them entirely.  That is a real hole rather than a technicality:
# the Speaker is briefed across every committee's subject matter and sits in
# the Gang of Eight for classified matters, which is broader access than any
# single committee seat confers, not narrower.
#
# congress-legislators publishes no leadership roles and no historical
# committee membership (`committee-membership-historical.json` does not exist),
# so this list is maintained by hand and deliberately kept short.  Membership
# of it is a stated editorial judgement, exactly like COMMITTEE_SENSITIVITY.
#
# Their remit is ALL sectors, because their access is not subject-limited.
# The consequence is that "in remit" means something different for these
# members - blanket access rather than a jurisdiction match - and the UI labels
# them separately so the two are never silently conflated.
LEADERSHIP_ROLES: dict[str, tuple[str, float]] = {
    "P000197": ("Speaker Emerita", 1.00),   # Nancy Pelosi
}


def all_sectors() -> list[str]:
    """Every sector in the committee taxonomy, for blanket-access roles."""
    out: set[str] = set()
    for _sens, sectors in COMMITTEE_SENSITIVITY.values():
        out.update(sectors)
    return sorted(out)


# A chair or ranking member sets the agenda and is briefed first.
TITLE_MULTIPLIER = {
    "chairman": 1.30, "chair": 1.30, "chairwoman": 1.30,
    "ranking member": 1.20, "vice chairman": 1.15, "vice chair": 1.15,
    "ex officio": 1.00,
}

DEFAULT_SENSITIVITY = 0.30


def _fetch_json(name: str, cache_dir: Path, refresh: bool = False):
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / FILES[name]
    if refresh or not dest.exists():
        resp = requests.get(BASE + FILES[name], timeout=90)
        resp.raise_for_status()
        dest.write_text(resp.text, encoding="utf-8")
    return json.loads(dest.read_text(encoding="utf-8"))


def _state_district(term: dict) -> str:
    """Render a House term as the 'GA12' form used by the Clerk's filings."""
    if term.get("type") != "rep":
        return ""
    state = term.get("state", "")
    district = term.get("district", 0)
    try:
        district = int(district)
    except (TypeError, ValueError):
        district = 0
    # At-large districts are filed as '00' by the Clerk.
    return f"{state}{district:02d}"


def load_former(conn: sqlite3.Connection, cache_dir: Path,
                refresh: bool = False, since: str = FORMER_SINCE) -> dict:
    """Add members who have left Congress but were serving during the filing era.

    Without this, a departed member's filings are orphaned: 26% of the
    transactions in this database resolved to nobody, because the roster source
    is ``legislators-current.json`` and lists only sitting members. Marjorie
    Taylor Greene's 560 disclosures, Thomas Carper's 1,121 and Markwayne
    Mullin's 559 simply had no row to attach to.

    They are flagged ``is_former`` and deliberately given **no committee
    seats**, because no current source has any for them. That is not an
    oversight - it is the whole reason they cannot enter the strategy. "In
    remit" needs a jurisdiction, they have none on file, so every one of their
    trades scores as out-of-remit and the equity curve never sees them. They
    appear in the tables and on their own pages; they do not appear in the
    result.

    The roster reaches back to 1789 and loading all of it would put thousands of
    extra surnames in front of the matcher, so only members whose service
    overlaps ``since`` are kept.
    """
    people = _fetch_json("legislators_historical", cache_dir, refresh)
    n = 0
    for person in people:
        bioguide = person.get("id", {}).get("bioguide")
        terms = person.get("terms") or []
        if not bioguide or not terms:
            continue
        last = terms[-1]
        end = last.get("end", "")
        if not end or end < since:
            continue
        name = person.get("name", {})
        chamber = "house" if last.get("type") == "rep" else "senate"
        conn.execute(
            """INSERT INTO legislators
               (bioguide_id, full_name, first_name, last_name, chamber, state,
                district, state_district, party, start_date, end_date,
                is_former, last_term_end)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)
               ON CONFLICT(bioguide_id) DO NOTHING""",
            (bioguide,
             name.get("official_full")
             or f"{name.get('first','')} {name.get('last','')}".strip(),
             name.get("first", ""), name.get("last", ""), chamber,
             last.get("state", ""), str(last.get("district", "")),
             _state_district(last), last.get("party", ""),
             last.get("start", ""), end, end))
        n += 1
    conn.commit()
    total = conn.execute(
        "SELECT COUNT(*) FROM legislators WHERE is_former = 1").fetchone()[0]
    return {"considered": n, "former_in_roster": total}


def load_into_db(conn: sqlite3.Connection, cache_dir: Path, refresh: bool = False) -> dict:
    """Populate the legislators, committees and membership tables."""
    legislators = _fetch_json("legislators", cache_dir, refresh)
    committees = _fetch_json("committees", cache_dir, refresh)
    membership = _fetch_json("membership", cache_dir, refresh)

    n_leg = 0
    for person in legislators:
        bioguide = person.get("id", {}).get("bioguide")
        if not bioguide:
            continue
        name = person.get("name", {})
        term = person.get("terms", [{}])[-1]
        chamber = "house" if term.get("type") == "rep" else "senate"
        conn.execute(
            """INSERT INTO legislators
               (bioguide_id, full_name, first_name, last_name, chamber, state,
                district, state_district, party, start_date, end_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(bioguide_id) DO UPDATE SET
                 full_name=excluded.full_name, chamber=excluded.chamber,
                 state=excluded.state, district=excluded.district,
                 state_district=excluded.state_district, party=excluded.party,
                 start_date=excluded.start_date, end_date=excluded.end_date""",
            (bioguide,
             name.get("official_full") or f"{name.get('first','')} {name.get('last','')}".strip(),
             name.get("first", ""), name.get("last", ""), chamber,
             term.get("state", ""), str(term.get("district", "")),
             _state_district(term), term.get("party", ""),
             term.get("start", ""), term.get("end", "")))
        n_leg += 1

    n_com = 0
    for com in committees:
        tid = com.get("thomas_id")
        if not tid:
            continue
        sens, sectors = COMMITTEE_SENSITIVITY.get(tid, (DEFAULT_SENSITIVITY, ()))
        conn.execute(
            """INSERT INTO committees (committee_id, name, chamber, parent_id, sensitivity, sectors)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(committee_id) DO UPDATE SET
                 name=excluded.name, chamber=excluded.chamber,
                 sensitivity=excluded.sensitivity, sectors=excluded.sectors""",
            (tid, com.get("name", ""), com.get("type", ""), None, sens, ",".join(sectors)))
        n_com += 1
        # Subcommittees inherit the parent's sensitivity, discounted slightly:
        # narrower remit, but still real jurisdiction.
        for sub in com.get("subcommittees", []) or []:
            # Subcommittees inherit the parent's sectors verbatim, so they add
            # no jurisdiction. Measured on the live roster: every one of 2,552
            # subcommittee seats belongs to someone already on the parent
            # committee, so deleting all 181 would change no member's remit.
            # They are stored for display and for the small breadth bonus only.
            sub_id = f"{tid}{sub.get('thomas_id','')}"
            conn.execute(
                """INSERT INTO committees (committee_id, name, chamber, parent_id, sensitivity, sectors)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(committee_id) DO UPDATE SET
                     name=excluded.name, parent_id=excluded.parent_id,
                     sensitivity=excluded.sensitivity, sectors=excluded.sectors""",
                (sub_id, f"{com.get('name','')} - {sub.get('name','')}",
                 com.get("type", ""), tid, round(sens * 0.85, 3), ",".join(sectors)))
            n_com += 1

    conn.execute("DELETE FROM committee_memberships")
    n_mem = 0
    for committee_id, members in membership.items():
        for m in members:
            bioguide = m.get("bioguide")
            if not bioguide:
                continue
            conn.execute(
                """INSERT INTO committee_memberships (bioguide_id, committee_id, title, rank)
                   VALUES (?,?,?,?)
                   ON CONFLICT(bioguide_id, committee_id) DO UPDATE SET
                     title=excluded.title, rank=excluded.rank""",
                (bioguide, committee_id, m.get("title", ""), m.get("rank")))
            n_mem += 1

    conn.commit()
    return {"legislators": n_leg, "committees": n_com, "memberships": n_mem}


# The strongest realistic seat is the chair of a top-jurisdiction committee:
# sensitivity 1.0 x the 1.30 chair multiplier.  Dividing by that maps the score
# onto 0-1 without a cap that flattens everyone at the top.
MAX_RAW_SCORE = 1.30
MAX_BREADTH_BONUS = 0.15


def is_leadership(conn: sqlite3.Connection, bioguide_id: str) -> bool:
    """Does this member's remit come from a leadership role rather than a seat?

    True only when they hold a listed leadership role *and* no committee seat -
    the case the jurisdiction filter would otherwise miss entirely.
    """
    if bioguide_id not in LEADERSHIP_ROLES:
        return False
    n = conn.execute(
        "SELECT COUNT(*) FROM committee_memberships WHERE bioguide_id = ?",
        (bioguide_id,)).fetchone()[0]
    return n == 0


# --------------------------------------------------------------------------
# Point-in-time committee membership
# --------------------------------------------------------------------------
# congress-legislators publishes committee membership only as it stands today.
# Judging a 2021 trade against a member's 2026 seat credits them with
# committees they had not joined - Marjorie Taylor Greene held *no* seats at all
# through 2021 and 2022, and Homeland Security only from 2023, so a
# current-roster reading puts two years of her trades inside a jurisdiction she
# did not have.
#
# There is no historical membership file, but the repository's *git history* is
# one: the current file at any past commit is that date's membership. Snapshots
# are taken quarterly, which is the natural granularity - assignments turn over
# at the start of each Congress and change only individually in between.
COMMITTEE_FILE = "committee-membership-current.yaml"
COMMITS_API = ("https://api.github.com/repos/unitedstates/"
               "congress-legislators/commits")
RAW_BASE = ("https://raw.githubusercontent.com/unitedstates/"
            "congress-legislators/{sha}/" + COMMITTEE_FILE)
HISTORY_START = "2019-01-01"

# A complete snapshot carries roughly 3,900 seats. At the start of every Congress
# the file is emptied and refilled committee by committee over weeks or months,
# so a snapshot taken on 1 January of an odd year is not "nobody sits on
# anything" - it is a file mid-rebuild. Reading it literally would delete a whole
# quarter of in-remit trades and silently understate the strategy.
#
# Anything below this is treated as incomplete, and the fetcher walks forward
# month by month to the first complete snapshot of that same Congress. The date
# it actually used is stored beside the quarter, because that gap is look-ahead
# and has to be auditable rather than assumed away.
MIN_COMPLETE_SEATS = 3000
MAX_FORWARD_MONTHS = 12


def _quarters(start: str, end: str) -> list[str]:
    out, year, q = [], int(start[:4]), (int(start[5:7]) - 1) // 3
    while True:
        d = f"{year:04d}-{q * 3 + 1:02d}-01"
        if d > end:
            return out
        if d >= start:
            out.append(d)
        q += 1
        if q == 4:
            q, year = 0, year + 1


def _commit_log(session) -> list[tuple[str, str]]:
    """Every commit that touched the membership file, newest first.

    Pulled once and reused for every snapshot. Asking the API per quarter would
    be thirty-odd calls against an unauthenticated limit of sixty an hour, and
    would start failing halfway through a re-run.
    """
    log, page = [], 1
    while page <= 10:
        r = session.get(COMMITS_API, timeout=60, params={
            "path": COMMITTEE_FILE, "per_page": 100, "page": page})
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        log += [(c["commit"]["author"]["date"][:10], c["sha"]) for c in batch]
        if len(batch) < 100:
            break
        page += 1
    log.sort(reverse=True)
    return log


def sync_committee_history(conn: sqlite3.Connection, cache_dir: Path,
                           start: str = HISTORY_START, refresh: bool = False,
                           progress: bool = True) -> dict:
    """Reconstruct quarterly committee membership from the repository history."""
    import datetime as dt

    import requests
    import yaml

    out_dir = cache_dir / "committee-history"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    wanted = _quarters(start, today)

    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT snapshot_date FROM committee_membership_history")}
    todo = [q for q in wanted if refresh or q not in have]
    if not todo:
        return {"snapshots": len(have), "fetched": 0, "rows": 0}

    session = requests.Session()
    session.headers["User-Agent"] = "trade-of-the-house/1.0"
    log = None
    fetched = 0

    def seats_at(probe: str):
        """Membership as the file stood on ``probe``, with the commit date used."""
        nonlocal log, fetched
        if log is None:
            log = _commit_log(session)
        sha_date = next(((d, h) for d, h in log if d <= probe), None)
        if sha_date is None:
            return None, None
        source, sha = sha_date
        cached = out_dir / (sha[:12] + ".yaml")
        if cached.exists() and not refresh:
            text = cached.read_text(encoding="utf-8")
        else:
            r = session.get(RAW_BASE.format(sha=sha), timeout=120)
            if r.status_code != 200:
                return None, None
            text = r.text
            cached.write_text(text, encoding="utf-8")
            fetched += 1
        data = yaml.safe_load(text) or {}
        return [(committee, m.get("bioguide"), m.get("title", ""))
                for committee, members in data.items()
                for m in (members or []) if m.get("bioguide")], source

    rows_written, forward_filled = 0, 0
    for q in todo:
        seats, source = seats_at(q)
        # Walk forward past a mid-rebuild file to the first complete one.
        months = 0
        while (seats is not None and len(seats) < MIN_COMPLETE_SEATS
               and months < MAX_FORWARD_MONTHS):
            months += 1
            y, mo = int(q[:4]), int(q[5:7]) + months
            y, mo = y + (mo - 1) // 12, (mo - 1) % 12 + 1
            probe = f"{y:04d}-{mo:02d}-28"
            if probe > today:
                break
            nxt, nxt_source = seats_at(probe)
            if nxt and len(nxt) >= MIN_COMPLETE_SEATS:
                seats, source = nxt, nxt_source
                forward_filled += 1
                break
        if seats is None:
            if progress:
                print("  " + q + ": no usable commit, skipped", flush=True)
            continue

        payload = [(q, source, c, b, t) for c, b, t in seats]
        conn.execute(
            "DELETE FROM committee_membership_history WHERE snapshot_date=?", (q,))
        conn.executemany(
            """INSERT OR IGNORE INTO committee_membership_history
               (snapshot_date, source_date, committee_id, bioguide_id, title)
               VALUES (?,?,?,?,?)""", payload)
        rows_written += len(payload)
        if progress:
            note = "" if source <= q else f"  <- from {source} (file was mid-rebuild)"
            print(f"  {q}: {len(payload):,} seats across "
                  f"{len({p[2] for p in payload})} committees{note}", flush=True)
    conn.commit()

    total = conn.execute(
        "SELECT COUNT(DISTINCT snapshot_date) FROM committee_membership_history"
    ).fetchone()[0]
    return {"snapshots": total, "fetched": fetched, "rows": rows_written,
            "forward_filled": forward_filled}


def snapshot_dates(conn: sqlite3.Connection) -> list[str]:
    """Every quarter we hold membership for, oldest first.

    Callers scoring thousands of trades bisect this once instead of querying per
    trade; :func:`member_access_score` still works without it.
    """
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT snapshot_date FROM committee_membership_history "
        "ORDER BY snapshot_date")]


def snapshot_at(dates: list[str], as_of: str | None) -> str | None:
    """The snapshot in force on ``as_of``, from a pre-fetched list."""
    import bisect

    if not as_of or not dates:
        return None
    i = bisect.bisect_right(dates, as_of)
    return dates[i - 1] if i else None


def snapshot_for(conn: sqlite3.Connection, as_of: str | None) -> str | None:
    """The snapshot in force on ``as_of``: the latest one at or before it.

    Before the earliest snapshot there is nothing to fall back on that would not
    be a guess, so this returns None and the caller treats the member as having
    no recorded seats.
    """
    if not as_of:
        return None
    row = conn.execute(
        """SELECT snapshot_date FROM committee_membership_history
            WHERE snapshot_date <= ? ORDER BY snapshot_date DESC LIMIT 1""",
        (as_of,)).fetchone()
    return row[0] if row else None


def member_access_score(conn: sqlite3.Connection, bioguide_id: str,
                        as_of: str | None = None) -> tuple[float, list[str], list[str]]:
    """Return (access score 0-1, full-committee names, exposed sectors).

    With ``as_of`` set, the seats are the ones held on that date, read from
    the quarterly snapshots in ``committee_membership_history``.  Without it
    the current roster is used, which is right for "what does this member
    oversee now" and wrong for scoring a trade made years ago.

    Driven by the member's *strongest* seat, because one seat on Financial
    Services conveys more than four on low-jurisdiction committees.  Extra seats
    add a capped breadth bonus - subcommittees count for less, since their
    jurisdiction already sits inside the parent committee's.  The result is
    normalised by the strongest seat anyone can hold (chair of a top-tier
    committee), so chairs separate from rank-and-file instead of all saturating
    at 1.0.
    """
    snapshot = snapshot_for(conn, as_of) if as_of else None
    if snapshot:
        rows = conn.execute(
            """SELECT c.name, c.sensitivity, c.sectors, m.title, c.parent_id
                 FROM committee_membership_history m
                 JOIN committees c ON c.committee_id = m.committee_id
                WHERE m.bioguide_id = ? AND m.snapshot_date = ?""",
            (bioguide_id, snapshot)).fetchall()
    else:
        rows = conn.execute(
            """SELECT c.name, c.sensitivity, c.sectors, m.title, c.parent_id
                 FROM committee_memberships m
                 JOIN committees c ON c.committee_id = m.committee_id
                WHERE m.bioguide_id = ?""", (bioguide_id,)).fetchall()

    # Leadership access applies when a member holds no committee seat, which is
    # the normal situation for the Speaker and the elected floor leaders.
    if not rows and bioguide_id in LEADERSHIP_ROLES:
        role, sensitivity = LEADERSHIP_ROLES[bioguide_id]
        return round(sensitivity, 3), [role], all_sectors()

    if not rows:
        return 0.0, [], []

    full, subs = [], []
    names, sectors = [], set()
    for r in rows:
        mult = TITLE_MULTIPLIER.get((r["title"] or "").strip().lower(), 1.0)
        weight = (r["sensitivity"] or 0) * mult
        if r["parent_id"] is None:
            full.append(weight)
            names.append(r["name"])
        else:
            subs.append(weight)
        for s in (r["sectors"] or "").split(","):
            if s:
                sectors.add(s)

    if not full and not subs:
        return 0.0, [], []

    full.sort(reverse=True)
    base = full[0] if full else max(subs)
    breadth = min(MAX_BREADTH_BONUS,
                  0.04 * sum(full[1:]) + 0.02 * sum(subs))
    score = min(1.0, (base + breadth) / MAX_RAW_SCORE)
    return round(score, 3), sorted(names), sorted(sectors)
