"""
A note on the oil market by Claude, written from what the OIL page shows.

The same two writers as the earnings reports and the morning brief (Settings
decides which): Claude Code on your Claude plan, or the API on your key. Only
written when you press the button: each one counts toward your plan's usage or
bills your key. The material it is given is saved next to the database in
oil_work/<day>/material.md (private, never committed), so a note can always be
traced back to the numbers it was written from.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timezone
from typing import Optional

from . import store, oil

SYSTEM = """You write a one-page note on the oil market for one private investor in the Netherlands. The material is a JSON document the investor's own terminal assembled from public sources: prices and the futures curve, refining margins, production by country (the EIA and JODI), tanker traffic through the chokepoints and tanker loadings by country (IMF PortWatch, from satellite AIS), US stocks (EIA), OPEC+ decisions and targets, futures positioning (CFTC and ICE), crude trade by country (UN Comtrade), pump prices in Europe, and how the investor's own holdings move with Brent.

Everything you write must come from the material, or — only if you were given web search — from pages you found, which you then cite by name. Never invent a number or round one into a different one. Say how old a figure is when it matters: PortWatch is about a week behind and misses ships that switch their transponders off; JODI and the EIA's monthly data run one to two months behind. When the material does not explain a move, say so rather than guessing.

Write for someone who knows markets: plain, specific, short. Structure, in Markdown:
# a one-line headline
**The short version** — three bullets.
## Supply and flows
## Prices, the curve and refining
## OPEC+
## What it means for this portfolio
## What to watch
At most five bullets, with the dates from the calendar.

Link the page's sections the way the material does, e.g. [the curve](/m/OIL#s-curve). Do not recommend buying or selling anything. Stay under 600 words."""

NOTE_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _today() -> str:
    return date.today().isoformat()


def _safe(fn):
    try:
        return fn()
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {e}"[:200]}


def material() -> str:
    """The page, trimmed to what a writer needs."""
    P = {k: _safe(f) for k, f in oil.PARTS.items()}
    ins = []
    for k in ("prices", "curve", "cracks", "flows", "supply", "opec", "stocks", "trade", "pump", "book"):
        ins += [i["text"] for i in (P.get(k) or {}).get("insights") or []]
    pr = P.get("prices") or {}
    cv = P.get("curve") or {}
    curve = {}
    for root, r in (cv.get("roots") or {}).items():
        curve[r["name"]] = {"front": r.get("front"), "m1_m2": r.get("m1m2"), "m1_m7": r.get("m1m7"),
                            "m1_m13": r.get("m1m13"), "m1_m13_pct": r.get("m1m13_pct"),
                            "curves": [{"label": c["label"], "day": c["day"],
                                        "months": [[p["lab"], p["px"]] for p in c["pts"][:13]]}
                                       for c in r.get("curves") or []]}
    sp = P.get("supply") or {}
    fl = P.get("flows") or {}
    op = P.get("opec") or {}
    st = P.get("stocks") or {}
    tr = P.get("trade") or {}
    pu = P.get("pump") or {}
    bk = P.get("book") or {}
    nw = P.get("news") or {}
    mat = (tr.get("matrix") or {})
    importers = []
    for imp in (mat.get("importers") or [])[:10]:
        sup = sorted((f for f in mat.get("flows") or [] if f["to"] == imp["iso"]), key=lambda f: -f["kbd"])[:5]
        importers.append({"importer": imp["name"], "year": imp["year"], "kbd": imp["kbd"],
                          "suppliers": [[oil.cname(f["from"]), f["kbd"]] for f in sup]})
    slim = {
        "written_for": _today(), "page": "/m/OIL",
        "rule_based_findings": ins,
        "prices": [{k: t.get(k) for k in ("name", "last", "chg", "pct", "unit", "asof", "sub")} for t in pr.get("tiles") or []],
        "curve": curve,
        "refining_cracks": (P.get("cracks") or {}).get("rows"),
        "production": {"last_month_of_data": sp.get("last_hist"), "eia_outlook": sp.get("edition"),
                       "countries": [{k: r.get(k) for k in ("name", "group", "kbd", "month", "d1", "d6", "d12", "src",
                                                            "kind", "share")} | {"exports_kbd": (r.get("exports") or {}).get("kbd")}
                                     for r in (sp.get("rows") or [])[:30]],
                       "world_crude_mbd": [x for x in (sp.get("world") or {}).get("crude") or []][-14:]},
        "chokepoints": [{k: c.get(k) for k in ("name", "w7", "y7", "vs_y7", "status", "end", "dwt7")}
                        for c in fl.get("chokepoints") or [] if c.get("oil")],
        "tanker_loadings_by_country": [{"country": c["name"], "kbd_30d_crude_equivalent": c.get("exp_kbd30"),
                                        "vs_previous_30d_pct": c["exp"].get("vs_p30"),
                                        "vs_year_ago_pct": c["exp"].get("vs_y30"), "to": c["exp"].get("end")}
                                       for c in (fl.get("countries") or [])[:24]],
        "us_stocks": [{k: r.get(k) for k in ("label", "last", "unit", "w1", "y1", "vs5_pct", "asof")} for r in st.get("rows") or []],
        "opec": {"next_meeting": op.get("next_meeting"), "group": op.get("group"),
                 "targets": op.get("targets"),
                 "members": [{k: m.get(k) for k in ("name", "group", "kbd", "month", "y1", "target", "gap")}
                             for m in op.get("members") or []],
                 "spare_capacity_mbd": [x for x in ((op.get("capacity") or {}).get("spare") or []) if not x[2]][-6:],
                 "unplanned_outages_mbd": [x for x in ((op.get("capacity") or {}).get("outages") or []) if not x[2]][-6:],
                 "recent_releases": [{k: r.get(k) for k in ("date", "kind", "decision", "next")} for r in (op.get("releases") or [])[:6]]},
        "positioning": [{k: r.get(k) for k in ("name", "asof", "mm_net", "mm_w1", "mm_pctile", "pm_net", "sw_net", "oi")}
                        for r in tr.get("positioning") or []],
        "crude_importers": importers,
        "pump_prices_eur_per_litre": [{k: r.get(k) for k in ("name", "petrol", "diesel", "petrol_y1", "diesel_y1",
                                                             "petrol_tax", "diesel_tax", "asof")}
                                      for r in pu.get("eu") or [] if r.get("c") in ("NL", "DE", "BE", "FR", "EU", "DK", "IT", "ES")]
                                     + [{k: r.get(k) for k in ("name", "kind", "eur_l", "y1", "asof")} for r in pu.get("other") or []],
        "brent_in_currencies": pu.get("currencies"),
        "portfolio": {"sensitivity": bk.get("you"), "brent": bk.get("brent"), "move_usd": bk.get("move"),
                      "holdings_most_exposed": [{k: h.get(k) for k in ("t", "name", "w", "beta", "r2", "per10", "oil_name")}
                                                for h in sorted(bk.get("holdings") or [], key=lambda h: -abs(h.get("beta") or 0))[:10]]},
        "calendar": (P.get("calendar") or {}).get("events"),
        "headlines": [{"headline": x["headline"], "source": x.get("source"),
                       "date": datetime.fromtimestamp(x["ts"]).date().isoformat() if x.get("ts") else None}
                      for x in (nw.get("items") or [])[:25]],
        "errors": {k: v["error"] for k, v in P.items() if isinstance(v, dict) and v.get("error")},
    }
    return "<oil_page>\n" + json.dumps(slim, default=str, ensure_ascii=False) + "\n</oil_page>"


def notes(d: Optional[str] = None) -> list[dict]:
    store.init_db()
    with store.conn() as c:
        if d:
            rows = c.execute("SELECT id,d,kind,question,created,model,body,meta,cost_usd FROM oil_note "
                             "WHERE d=? ORDER BY id", (d,)).fetchall()
        else:
            rows = c.execute("SELECT id,d,kind,question,created,model,body,meta,cost_usd FROM oil_note "
                             "ORDER BY id DESC LIMIT 12").fetchall()
    out = []
    for r in rows:
        x = dict(r)
        try:
            x["meta"] = json.loads(x["meta"] or "{}")
        except Exception:
            x["meta"] = {}
        out.append(x)
    return out


def delete_note(nid: int) -> bool:
    with store.conn() as c:
        return c.execute("DELETE FROM oil_note WHERE id=?", (nid,)).rowcount > 0


def _save_note(d: str, question: Optional[str], body: str, model_name: str, meta: dict,
               cost_usd: Optional[float]) -> int:
    store.init_db()
    with store.conn() as c:
        cur = c.execute("INSERT INTO oil_note (d,kind,question,created,model,body,meta,cost_usd) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (d, "answer" if question else "note", question,
                         datetime.now(timezone.utc).isoformat(timespec="seconds"), model_name, body,
                         json.dumps(meta), cost_usd))
        return cur.lastrowid


def _ask(question: str) -> str:
    return (f"The investor has read your note and asks:\n\n{question}\n\n"
            "Answer directly from the material. As long as it needs, no longer.")


def _work_dir(d: str) -> str:
    w = os.path.join(os.path.dirname(os.path.abspath(store.DB_PATH)), "oil_work", d)
    os.makedirs(w, exist_ok=True)
    return w


def _run_note(job: dict, d: str, question: Optional[str], web: bool):
    from . import earnai
    try:
        job["phase"] = "reading the page"
        mat = material()
        ask = _ask(question) if question else f"Write the oil note for {d}."
        if web and not question:
            ask += (" You have web search: use it only to explain a move the material gives no cause for, or to "
                    "confirm a big story. At most four searches; cite what you used.")
        prior = [n for n in notes(d) if n["kind"] == "note"]
        if job["writer"] == "claude-code":
            work = _work_dir(d)
            tools = "WebSearch,WebFetch" if web and not question else ""
            resume = (prior[-1]["meta"] or {}).get("session") if (question and prior) else None
            out = None
            if resume:
                try:
                    job["phase"] = "answering"
                    out = earnai.cc_stream(earnai.cc_command("", resume, system=SYSTEM), _ask(question), work, job)
                except RuntimeError as e:
                    if "conversation" not in str(e).lower() and "session" not in str(e).lower():
                        raise
                    out = None
            if out is None:
                text = mat + ("\n\n<your_note>\n" + prior[-1]["body"] + "\n</your_note>" if question and prior else "") + "\n\n" + ask
                with open(os.path.join(work, "material.md"), "w", encoding="utf-8") as f:
                    f.write(text)
                job["phase"] = "writing"
                out = earnai.cc_stream(earnai.cc_command(tools, system=SYSTEM), text, work, job)
            if not out["text"]:
                raise RuntimeError("Claude Code returned no text")
            name = f"Claude Code · {out['model'] or 'default model'}"
            meta = {"writer": "claude-code", "session": out["session"], "web": web,
                    "usage": out["usage"], "api_equivalent_usd": out["api_usd"], "ms": out["ms"]}
            _save_note(d, question, out["text"], name, meta, None)
            job.update(state="done", phase="done", model=name, api_usd=out["api_usd"])
        else:
            m = earnai.model()
            content = [{"type": "text", "text": mat, "cache_control": {"type": "ephemeral"}}]
            if question and prior:
                content.append({"type": "text", "text": "<your_note>\n" + prior[-1]["body"] + "\n</your_note>"})
            content.append({"type": "text", "text": ask})
            kw = dict(model=m, max_tokens=16000, system=SYSTEM, messages=[{"role": "user", "content": content}],
                      thinking={"type": "adaptive"}, output_config={"effort": earnai.WRITE_EFFORT})
            betas = []
            if m in earnai.FALLBACK_OK:
                betas.append("server-side-fallback-2026-07-01")
                kw["fallbacks"] = "default"
            if web and not question:
                kw["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}]
            job["phase"] = "writing"
            client = earnai._client()
            usage, parts, served = {}, [], m
            for _ in range(3):
                with client.beta.messages.stream(betas=betas, **kw) as stream:
                    for ev in stream:
                        if ev.type == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                            job["text"] += ev.delta.text
                    msg = stream.get_final_message()
                served = getattr(msg, "model", m) or m
                for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                    usage[k] = usage.get(k, 0) + (getattr(msg.usage, k, 0) or 0)
                if msg.stop_reason == "refusal":
                    raise RuntimeError("the model declined this request")
                parts += [bl.text for bl in msg.content if getattr(bl, "type", "") == "text"]
                if msg.stop_reason == "pause_turn":
                    kw["messages"] = kw["messages"] + [{"role": "assistant", "content": msg.content}]
                    continue
                break
            body = "\n".join(p for p in parts if p).strip()
            if not body:
                raise RuntimeError("the model returned no text")
            c = earnai.cost(served if served in earnai.MODELS else m, usage)
            _save_note(d, question, body, served, {"writer": "api", "usage": usage, "web": web}, c)
            job.update(state="done", phase="done", model=served, cost_usd=c)
    except Exception as e:
        msg = f"{e.__class__.__name__}: {e}"
        try:
            import anthropic
            if isinstance(e, anthropic.AuthenticationError):
                msg = "the Anthropic API key was refused — check it in Settings"
        except Exception:
            pass
        job.update(state="error", error=msg[:300])
    finally:
        job["finished"] = time.time()


def _pub(j: Optional[dict]) -> Optional[dict]:
    if not j:
        return None
    return {**j, "elapsed_s": round((j.get("finished") or time.time()) - j["started"])}


def start_note(question: Optional[str] = None, web: bool = False) -> dict:
    from . import earnai
    w = earnai.writer()
    if w == "api" and not earnai.api_key():
        raise PermissionError("no Anthropic API key — add one in Settings, or switch the writer to Claude Code")
    if w == "claude-code":
        st = earnai.cc_status()
        if not st["found"]:
            raise PermissionError("Claude Code was not found on this computer — install the Claude desktop app, "
                                  "or switch the writer to an API key in Settings")
        if not st["logged_in"]:
            st = earnai.cc_status(force=True)
            if not st["logged_in"]:
                raise PermissionError("Claude Code is not signed in yet — sign in once from ERN or ALRT")
    d = _today()
    with _LOCK:
        j = NOTE_JOBS.get(d)
        if j and j["state"] == "running":
            return _pub(j)
        j = {"state": "running", "phase": "starting", "text": "", "started": time.time(), "finished": None,
             "question": question, "error": None, "writer": w, "web": web,
             "model": (earnai.cc_model() or "default") if w == "claude-code" else earnai.model()}
        NOTE_JOBS[d] = j
    threading.Thread(target=_run_note, args=(j, d, question, web), daemon=True, name=f"oil-note-{d}").start()
    return _pub(j)


def note_state() -> dict:
    from . import earnai
    w = earnai.writer()
    d = _today()
    ns = notes()
    return {"writer": w, "writer_label": earnai.WRITERS[w], "has_key": bool(earnai.api_key()),
            "model_label": earnai.MODELS[earnai.model()][0], "cc_model_label": earnai.CC_MODELS[earnai.cc_model()],
            "cc": earnai.cc_status() if w == "claude-code" else None,
            "job": _pub(NOTE_JOBS.get(d)), "today": d,
            "notes": [n for n in ns if n["kind"] == "note"], "answers": [n for n in ns if n["kind"] == "answer"]}
