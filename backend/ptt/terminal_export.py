"""
The terminal's view of the tracker's database (POL, build 49).

Run after every pipeline run, in the tracker's own process (its modules on
the path), never inside the terminal's server:

    python ptt/terminal_export.py

It writes four tables beside the tracker's, which the terminal's page reads
with plain queries. The tracker's own tables are only read.

    pol_trade      every disclosed transaction, one row each: ISO dates (the
                   Clerk's are M/D/YYYY and do not sort), chamber and party,
                   the alpha from the first open after the filing, the grade,
                   and the committee sectors it falls in, judged against the
                   seats held on the day of the trade (congress.member_access_score
                   as_of the trade, the tracker's own point-in-time test)
    pol_member     every member who filed: committees and remit as seated
                   now, access score, the ranking from politician_stats, and
                   whether the in-remit strategy tracks them
    pol_committee  committees.stats(): what trading inside each full
                   committee's jurisdiction returned
    pol_ocr        rows read from scanned filings, staged unreviewed by the
                   tracker's OCR and never scored

Each is built under a temporary name and swapped in with the others in one
transaction, so a page never reads half an export.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import congress  # noqa: E402
import db as db_mod  # noqa: E402
from backtest import _to_iso  # noqa: E402

DB = ROOT / "data" / "tracker.db"

TABLES = {
    "pol_trade": """(
        id INTEGER PRIMARY KEY, doc_id TEXT, seq INTEGER, bioguide_id TEXT, member TEXT,
        chamber TEXT, party TEXT, state_district TEXT, owner TEXT, ticker TEXT, asset_name TEXT,
        asset_type TEXT, tx_type TEXT, traded TEXT, filed TEXT, amount_raw TEXT, amount_mid REAL,
        entry_date TEXT, alpha_1m REAL, alpha_3m REAL, alpha_6m REAL, alpha_12m REAL,
        grade TEXT, grade_score REAL, remit TEXT, sectors TEXT, pdf_url TEXT)""",
    "pol_member": """(
        bioguide_id TEXT PRIMARY KEY, name TEXT, chamber TEXT, party TEXT, state TEXT,
        state_district TEXT, is_former INTEGER, last_term_end TEXT, score REAL,
        committees TEXT, remit TEXT, stats TEXT, tracked INTEGER, n_trades INTEGER,
        n_buys INTEGER, first_traded TEXT, last_traded TEXT, last_filed TEXT)""",
    "pol_committee": """(
        committee_id TEXT PRIMARY KEY, name TEXT, chamber TEXT, row TEXT)""",
    "pol_ocr": """(
        id INTEGER PRIMARY KEY, doc_id TEXT, member TEXT, filed TEXT, asset_name TEXT,
        tx_type TEXT, traded TEXT, amount_raw TEXT, confident INTEGER, pdf_url TEXT)""",
}
INDEXES = ["CREATE INDEX ix_pol_trade_ticker ON pol_trade(ticker)",
           "CREATE INDEX ix_pol_trade_member ON pol_trade(bioguide_id)",
           "CREATE INDEX ix_pol_trade_filed ON pol_trade(filed)"]


def _name(s) -> str:
    return (s or "").replace("Hon. ", "").strip()


def _has(conn, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _alpha(r, h: str):
    a, b = r[f"ret_{h}"], r[f"bench_{h}"]
    return a - b if a is not None and b is not None else None


def trades(conn) -> list[tuple]:
    ticker_sectors = {r["ticker"]: sorted(s for s in (r["sectors"] or "").split(",") if s)
                      for r in conn.execute("SELECT ticker, sectors FROM ticker_meta")}
    snaps = congress.snapshot_dates(conn)
    grades = _has(conn, "trade_grades")
    rows = conn.execute(f"""
        SELECT t.id, t.doc_id, t.seq, t.bioguide_id, t.member_name,
               COALESCE(t.state_district, l.state_district) AS state_district, t.owner, t.ticker,
               t.asset_name, t.asset_type, t.tx_type, COALESCE(t.tx_date_fixed, t.tx_date) AS traded, t.filing_date,
               t.amount_raw, t.amount_mid, l.chamber, l.party, f.pdf_url,
               p.entry_date, p.ret_1m, p.ret_3m, p.ret_6m, p.ret_12m, p.bench_1m, p.bench_3m, p.bench_6m, p.bench_12m
               {", g.grade, g.score AS grade_score" if grades else ", NULL AS grade, NULL AS grade_score"}
          FROM transactions t
     LEFT JOIN legislators l ON l.bioguide_id = t.bioguide_id
     LEFT JOIN filings f ON f.doc_id = t.doc_id
     LEFT JOIN trade_performance p ON p.transaction_id = t.id
          {"LEFT JOIN trade_grades g ON g.transaction_id = t.id" if grades else ""}""").fetchall()
    remit_cache: dict[tuple, set] = {}
    out = []
    for r in rows:
        secs = ticker_sectors.get(r["ticker"] or "", [])
        overlap = []
        if r["bioguide_id"] and secs and r["traded"]:
            key = (r["bioguide_id"], congress.snapshot_at(snaps, r["traded"]))
            if key not in remit_cache:
                remit_cache[key] = set(congress.member_access_score(conn, r["bioguide_id"], as_of=r["traded"])[2])
            overlap = sorted(remit_cache[key] & set(secs))
        chamber = r["chamber"] or ("senate" if str(r["doc_id"]).startswith("sen:") else "house")
        out.append((r["id"], r["doc_id"], r["seq"], r["bioguide_id"], _name(r["member_name"]), chamber, r["party"],
                    r["state_district"], r["owner"], r["ticker"], r["asset_name"], r["asset_type"], r["tx_type"],
                    r["traded"], _to_iso(r["filing_date"]), r["amount_raw"], r["amount_mid"], r["entry_date"],
                    _alpha(r, "1m"), _alpha(r, "3m"), _alpha(r, "6m"), _alpha(r, "12m"), r["grade"], r["grade_score"],
                    ",".join(overlap), ",".join(secs), r["pdf_url"]))
    return out


def members(conn, trade_rows: list[tuple]) -> list[tuple]:
    import strategy
    payload = strategy.load(conn) or {}
    tracked = {m["bioguide_id"] for m in payload.get("members") or []}
    stats = {r["bioguide_id"]: dict(r) for r in conn.execute("SELECT * FROM politician_stats")}
    per: dict[str, dict] = {}
    for t in trade_rows:
        bg = t[3]
        if not bg:
            continue
        e = per.setdefault(bg, {"n": 0, "buys": 0, "first": None, "last": None, "filed": None})
        e["n"] += 1
        e["buys"] += t[12] == "purchase"
        if t[13]:
            e["first"] = min(e["first"] or t[13], t[13])
            e["last"] = max(e["last"] or t[13], t[13])
        if t[14]:
            e["filed"] = max(e["filed"] or t[14], t[14])
    out = []
    for r in conn.execute("SELECT * FROM legislators"):
        bg = r["bioguide_id"]
        if bg not in per and bg not in stats:
            continue
        r = dict(r)
        score, _names, remit = congress.member_access_score(conn, bg)
        seats = [dict(x) for x in conn.execute(
            """SELECT c.committee_id, c.name, c.chamber, c.sectors, c.parent_id, m.title
                 FROM committee_memberships m JOIN committees c ON c.committee_id = m.committee_id
                WHERE m.bioguide_id = ? ORDER BY (c.parent_id IS NOT NULL AND c.parent_id <> ''), c.name""", (bg,))]
        e = per.get(bg, {})
        out.append((bg, _name(r.get("full_name")), r.get("chamber"), r.get("party"), r.get("state"), r.get("state_district"),
                    int(r.get("is_former") or 0), r.get("last_term_end"), score, json.dumps(seats), ",".join(remit),
                    json.dumps(stats.get(bg), default=str) if bg in stats else None, int(bg in tracked), e.get("n", 0),
                    e.get("buys", 0), e.get("first"), e.get("last"), e.get("filed")))
    return out, tracked


def committees(conn, tracked: set) -> list[tuple]:
    import committees as committees_mod
    return [(c.get("committee_id"), c.get("name"), c.get("chamber"), json.dumps(c, default=str))
            for c in committees_mod.stats(conn, tracked_ids=tracked)]


def ocr_rows(conn) -> list[tuple]:
    if not _has(conn, "ocr_transactions"):
        return []
    return [(r["id"], r["doc_id"], _name(r["member_name"]), _to_iso(r["filing_date"]), r["asset_name"], r["tx_type"],
             r["tx_date"], r["amount_raw"], r["confident"], r["pdf_url"])
            for r in conn.execute("""SELECT o.*, f.member_name, f.filing_date, f.pdf_url FROM ocr_transactions o
                                     LEFT JOIN filings f ON f.doc_id = o.doc_id""")]


def run(path: Path = DB) -> dict:
    conn = db_mod.connect(path)
    t = trades(conn)
    m, tracked = members(conn, t)
    c = committees(conn, tracked)
    o = ocr_rows(conn)
    data = {"pol_trade": t, "pol_member": m, "pol_committee": c, "pol_ocr": o}
    with conn:
        for name, cols in TABLES.items():
            conn.execute(f"DROP TABLE IF EXISTS {name}_new")
            conn.execute(f"CREATE TABLE {name}_new {cols}")
            if data[name]:
                conn.executemany(f"INSERT INTO {name}_new VALUES ({','.join('?' * len(data[name][0]))})", data[name])
        for name in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {name}")
            conn.execute(f"ALTER TABLE {name}_new RENAME TO {name}")
        for ix in INDEXES:
            conn.execute(ix)
        info = {"built_at": dt.datetime.now().isoformat(timespec="seconds"), "trades": len(t), "members": len(m),
                "committees": len(c), "ocr": len(o), "newest_filed": max((x[14] for x in t if x[14]), default=None),
                "in_remit": sum(1 for x in t if x[24])}
        db_mod.set_meta(conn, "pol_export", json.dumps(info))
    conn.close()
    return info


if __name__ == "__main__":
    print(json.dumps(run(Path(sys.argv[1]) if len(sys.argv) > 1 else DB)))
