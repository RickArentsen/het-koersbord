"""
Claude's second opinion on one stock from the watchlist: is the fall an
opportunity or a value trap, from what the screen found plus the analysts,
the news and the days it fell.

The same two writers as the earnings reports, the brief and the oil note
(Settings decides which): Claude Code on your Claude plan, or the API on your
key. Written only when you press the button. The material is saved next to
the database in wl_work/<symbol>-<day>/material.md (private, never committed).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timezone
from typing import Optional

from . import store, watch

SYSTEM = """You write a one-page second opinion on one stock for a private investor in the Netherlands. The investor's own terminal flagged it on a watchlist screen: a company listed in the US or Europe, worth over $5bn, trading well below its 52-week high. The material is a JSON document the terminal assembled: every check the screen ran with the number it found (quality, growth, analysts' estimates and how they moved, valuation against the sector and against its own multiple at the high, income), how far the price fell and how much of that was the market, the sector and the company, the days it fell most with the catalyst found for each, the analysts' consensus and actions, recent headlines, and how the stock fits the investor's portfolio.

Your job is to answer one question: does this fall look like the price getting ahead of a business that is still intact, or like the market seeing a problem the numbers have not shown yet? Argue both sides, then say which the evidence favours and how strongly.

Everything you write must come from the material, or — only if you were given web search — from pages you found, which you then cite by name. Never invent a number or round one into a different one. Say how old a figure is when it matters: statements are the last reported quarter or year; estimates and prices are from the last scan. When the material cannot settle a point, say what would.

Write for someone who knows markets: plain, specific, short. Structure, in Markdown:
# a one-line verdict
**The short version** — three bullets.
## Why it fell
## The case that the business is intact
## The case that it is not
## What would change the view
At most four bullets: the numbers or events to watch, with dates where the material has them.
## How it fits the portfolio
One short paragraph.

Do not tell the investor to buy or sell. Stay under 650 words."""

NOTE_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _today() -> str:
    return date.today().isoformat()


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        return default if default is not None else {"error": f"{e.__class__.__name__}: {e}"[:200]}


def material(sym: str) -> str:
    """The stock's row from the last scan, with the analysts, the news and the falling days."""
    from . import research
    r = watch.row_of(sym)
    if not r or r.get("near_only"):
        raise LookupError(f"{sym} is not among the stocks the last scan checked in full")
    pf = watch.prefs()
    labels = watch.CHECKS
    ck = [{"check": labels[k][1], "group": watch.GROUPS[labels[k][0]], "result": st, "found": txt,
           "rule": labels[k][2]} for k, st, txt in r.get("ck") or []]
    st = _safe(lambda: research.street(r["sym"]), {}) or {}
    periods = [{k: p.get(k) for k in ("label", "eps", "rev", "trend", "revisions")} for p in st.get("periods") or []]
    nw = _safe(lambda: research.news(r["sym"]), {}) or {}
    why = _safe(lambda: watch.why(r["sym"]), {}) or {}
    slim = {
        "written_for": _today(), "page": f"/m/WCH#s={r['sym']}",
        "stock": {k: r.get(k) for k in ("sym", "name", "sector", "region", "country", "exch", "ccy", "px", "mcap_usd",
                                         "also", "about")},
        "industry": (r.get("m") or {}).get("industry"),
        "screen": {"preset_in_use": watch.PRESETS[pf["preset"]]["label"], "pullback_threshold_pct": pf["thr"],
                   "status": watch.status(r, pf["preset"], pf["thr"]),
                   "down_for_a_reason": r.get("broken"),
                   "group_scores": {watch.GROUPS[g]: v for g, v in (r.get("gs") or {}).items()},
                   "preset_results": r.get("ps"), "checks": ck},
        "price": r.get("pr"), "fall_split_pct_points": r.get("fall"),
        "numbers": r.get("m"),
        "analysts": {"periods": periods, "ltg": st.get("ltg"), "targets": st.get("targets"),
                     "recommendations": (st.get("recommendations") or [])[:2],
                     "surprises": (st.get("surprises") or [])[-6:], "next_earnings": st.get("next_earnings"),
                     "actions_since_high": (why.get("actions") or [])[:10]},
        "falling_days": [{k: d.get(k) for k in ("d", "r", "mkt", "sec", "idio", "z", "driver", "why", "headline",
                                                 "catalysts")} for d in (why.get("days") or [])[:8]],
        "earnings_since_high": why.get("earnings"),
        "headlines": [{"headline": x.get("headline"), "source": x.get("source"), "category": x.get("cat"),
                       "date": datetime.fromtimestamp(x["ts"]).date().isoformat() if x.get("ts") else None}
                      for x in (nw.get("items") or [])[:20]],
        "portfolio_fit": r.get("fit"),
        "pin": r.get("pin"),
    }
    return "<watchlist_stock>\n" + json.dumps(slim, default=str, ensure_ascii=False) + "\n</watchlist_stock>"


def notes(sym: str, d: Optional[str] = None) -> list[dict]:
    store.init_db()
    with store.conn() as c:
        if d:
            rows = c.execute("SELECT id,sym,d,kind,question,created,model,body,meta,cost_usd FROM wl_note "
                             "WHERE sym=? AND d=? ORDER BY id", (sym, d)).fetchall()
        else:
            rows = c.execute("SELECT id,sym,d,kind,question,created,model,body,meta,cost_usd FROM wl_note "
                             "WHERE sym=? ORDER BY id DESC LIMIT 12", (sym,)).fetchall()
    out = []
    for r in rows:
        x = dict(r)
        try:
            x["meta"] = json.loads(x["meta"] or "{}")
        except Exception:
            x["meta"] = {}
        out.append(x)
    return out


def noted() -> list[dict]:
    """Every stock with a note, newest first — for the page's list."""
    store.init_db()
    with store.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT sym, MAX(created) created, COUNT(*) n FROM wl_note WHERE kind='note' GROUP BY sym ORDER BY created DESC LIMIT 40")]


def delete_note(nid: int) -> bool:
    with store.conn() as c:
        return c.execute("DELETE FROM wl_note WHERE id=?", (nid,)).rowcount > 0


def _save_note(sym: str, d: str, question: Optional[str], body: str, model_name: str, meta: dict,
               cost_usd: Optional[float]) -> int:
    store.init_db()
    with store.conn() as c:
        cur = c.execute("INSERT INTO wl_note (sym,d,kind,question,created,model,body,meta,cost_usd) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (sym, d, "answer" if question else "note", question,
                         datetime.now(timezone.utc).isoformat(timespec="seconds"), model_name, body,
                         json.dumps(meta), cost_usd))
        return cur.lastrowid


def _ask(question: str) -> str:
    return (f"The investor has read your note and asks:\n\n{question}\n\n"
            "Answer directly from the material. As long as it needs, no longer.")


def _work_dir(sym: str, d: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in sym)
    w = os.path.join(os.path.dirname(os.path.abspath(store.DB_PATH)), "wl_work", f"{safe}-{d}")
    os.makedirs(w, exist_ok=True)
    return w


def _run_note(job: dict, sym: str, d: str, question: Optional[str], web: bool):
    from . import earnai
    try:
        job["phase"] = "reading the stock"
        mat = material(sym)
        ask = _ask(question) if question else f"Write the second opinion on {sym} for {d}."
        if web and not question:
            ask += (" You have web search: use it only to find the cause of a fall the material gives no reason for, "
                    "or news since the last scan. At most four searches; cite what you used.")
        prior = [n for n in notes(sym, d) if n["kind"] == "note"]
        if job["writer"] == "claude-code":
            work = _work_dir(sym, d)
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
            _save_note(sym, d, question, out["text"], name, meta, None)
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
            _save_note(sym, d, question, body, served, {"writer": "api", "usage": usage, "web": web}, c)
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


def start_note(sym: str, question: Optional[str] = None, web: bool = False) -> dict:
    from . import earnai
    sym = sym.upper()
    r = watch.row_of(sym)
    if not r or r.get("near_only"):
        raise LookupError(f"{sym} is not among the stocks the last scan checked in full")
    sym = r["sym"]
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
    key = f"{sym}|{d}"
    with _LOCK:
        j = NOTE_JOBS.get(key)
        if j and j["state"] == "running":
            return _pub(j)
        j = {"state": "running", "phase": "starting", "text": "", "started": time.time(), "finished": None,
             "question": question, "error": None, "writer": w, "web": web, "sym": sym,
             "model": (earnai.cc_model() or "default") if w == "claude-code" else earnai.model()}
        NOTE_JOBS[key] = j
    threading.Thread(target=_run_note, args=(j, sym, d, question, web), daemon=True, name=f"wl-note-{sym}").start()
    return _pub(j)


def note_state(sym: str) -> dict:
    from . import earnai
    sym = sym.upper()
    r = watch.row_of(sym)
    if r:
        sym = r["sym"]
    w = earnai.writer()
    d = _today()
    ns = notes(sym)
    return {"sym": sym, "writer": w, "writer_label": earnai.WRITERS[w], "has_key": bool(earnai.api_key()),
            "model_label": earnai.MODELS[earnai.model()][0], "cc_model_label": earnai.CC_MODELS[earnai.cc_model()],
            "cc": earnai.cc_status() if w == "claude-code" else None,
            "job": _pub(NOTE_JOBS.get(f"{sym}|{d}")), "today": d,
            "notes": [n for n in ns if n["kind"] == "note"], "answers": [n for n in ns if n["kind"] == "answer"]}
