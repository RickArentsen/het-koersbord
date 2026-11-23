"""
The write-up of a report: a free one assembled by rules, and the one Claude
writes when you ask.

THE FREE ONE is sentences built from the numbers — surprise, guidance against
the consensus, the reaction against what options priced, the call's tone and
what analysts asked — in a fixed order. It never judges what it cannot count.

THE CLAUDE ONE reads the documents themselves: the press release and what was
furnished with it, the slides (a PDF goes in as a PDF, charts and all), the
MD&A of the 10-Q, the call transcript, anything you added — plus the figures
this page computed and your own position and thesis. It is written once per
report and kept; follow-up questions reuse the same material from Anthropic's
prompt cache, so they cost a fraction of the first.

Two ways to run it, chosen in Settings on the home screen:

    Claude Code   (the default) the Claude Code tool the desktop app installs,
                  signed in to your Claude subscription. No charge per report;
                  each one counts toward your plan's usage like a long chat.
                  Follow-up questions continue the report's own conversation.
    API key       your Anthropic API key, on the model you choose (Claude Opus
                  5 unless you pick another). Paid per report; the cost is
                  worked out from the tokens it used and shown next to it.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from . import store, research, earndocs as ed

MODELS = {
    # model: (label, $ per million input, $ per million output)
    "claude-opus-5": ("Claude Opus 5", 5.0, 25.0),
    "claude-sonnet-5": ("Claude Sonnet 5", 2.0, 10.0),
    "claude-fable-5-1": ("Claude Fable 5.1", 10.0, 50.0),
    "claude-opus-5-5": ("Claude Opus 5.5", 4.0, 20.0),
}
# Every write-up, by either writer, is Claude Opus 5.5 at high effort: the user's rule of 17 November
# 2026, after Claude Code, left on "your plan's default", ran a model their plan does not have.
WRITE_MODEL = "claude-opus-5-5"
WRITE_EFFORT = "high"
DEFAULT_MODEL = WRITE_MODEL
# Server-side fallback on a refusal: offered for these; unknown on the rest.
FALLBACK_OK = {"claude-opus-5", "claude-fable-5-1", "claude-opus-5-5"}
MAX_PDF_BYTES = 24 * 1024 * 1024

JOBS: dict = {}
_LOCK = threading.Lock()


def api_key() -> Optional[str]:
    k = (store.get_setting("anthropic_api_key") or "").strip()
    return k or None


def model() -> str:
    """The API writer's model: always WRITE_MODEL, whatever an older setting says."""
    return WRITE_MODEL


def cost(m: str, usage: dict) -> Optional[float]:
    if m not in MODELS or not usage:
        return None
    _, pin, pout = MODELS[m]
    inp = usage.get("input_tokens") or 0
    cw = usage.get("cache_creation_input_tokens") or 0
    cr = usage.get("cache_read_input_tokens") or 0
    out = usage.get("output_tokens") or 0
    searches = usage.get("web_search_requests") or 0
    return round((inp * pin + cw * pin * 1.25 + cr * pin * 0.1 + out * pout) / 1e6 + searches * 0.01, 4)


# --------------------------------------------------------------------------
# the free, rule-based write-up
# --------------------------------------------------------------------------

def _fmt_money(v: Optional[float]) -> str:
    if v is None:
        return "–"
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:,.2f}bn"
    if a >= 1e6:
        return f"${v / 1e6:,.1f}m"
    return f"${v:,.2f}"


def analysis(sym: str, d: str) -> dict:
    from . import earnings as E
    ev = E.resolve(sym, d)
    if not ev.get("reported") and not (ev.get("live") or {}).get("found"):
        return {"ok": False, "why": "the write-up is built once the report is out"}
    res = E.part(sym, "results", d)
    pb = E.part(sym, "playbook", d)
    cl = E.part(sym, "call", d)
    ex = E.part(sym, "expect", d)
    out = []                                   # (section, tone, sentence)
    short = ev["short"]

    eps, rev = res.get("eps") or {}, res.get("revenue") or {}
    if eps.get("surprise") is not None or rev.get("surprise") is not None:
        bits = []
        if rev.get("surprise") is not None:
            bits.append(f"revenue of {_fmt_money(rev['actual'])} {'beat' if rev['surprise'] > 0 else 'missed'} the "
                        f"{_fmt_money(rev['est'])} consensus by {abs(rev['surprise']):.1f}%")
        if eps.get("surprise") is not None:
            bits.append(f"EPS of ${eps['actual']:.2f} {'beat' if eps['surprise'] > 0 else 'missed'} ${eps['est']:.2f} "
                        f"by {abs(eps['surprise']):.1f}%")
        both = [x for x in (rev.get("surprise"), eps.get("surprise")) if x is not None]
        tone = "good" if all(x > 0 for x in both) else ("bad" if all(x < 0 for x in both) else "warn")
        out.append(("Headline", tone, f"{short}: " + " and ".join(bits) + "."))
        for k, lab in (("eps", "EPS"), ("revenue", "revenue")):
            c = res.get(k) or {}
            if c.get("outside"):
                out.append(("Headline", "good" if c["outside"] == "above" else "bad",
                            f"{lab.capitalize()} came in {c['outside']} the whole range of analyst estimates."))
        yoy = res.get("revenue_yoy")
        if yoy is not None:
            out.append(("Headline", "info", f"Revenue grew {yoy:+.1f}% on the year."))
        st = pb.get("stats") or {}
        if eps.get("surprise") is not None and st.get("n"):
            avg = st.get("avg_surprise")
            if avg is not None:
                out.append(("Headline", "info",
                            f"That is {'a bigger' if eps['surprise'] > avg else 'a smaller'} beat than its usual "
                            f"{avg:+.1f}%." if eps["surprise"] > 0 else
                            f"It usually beats by {avg:+.1f}%; a miss is rare for it and tends to be read hard."))

    # Guidance
    g = res.get("guidance") or []
    snap = E.snapshot(ev["symbol"], d) or {}
    nxt = snap.get("next") or {}
    for x in g:
        if x.get("low") is None or x.get("unit") != "$":
            continue
        mid = (x["low"] + x["high"]) / 2
        per = (x.get("period") or "").lower()
        if x.get("metric") == "revenue" and nxt.get("rev") and "year" not in per and "fiscal" not in per \
                and 0.5 < mid / nxt["rev"] < 2:
            gap = (mid / nxt["rev"] - 1) * 100
            out.append(("Guidance", "good" if gap > 0.5 else ("bad" if gap < -0.5 else "info"),
                        f"Next quarter's revenue guidance ({_fmt_money(x['low'])}–{_fmt_money(x['high'])}, midpoint "
                        f"{_fmt_money(mid)}) is {abs(gap):.1f}% {'above' if gap > 0 else 'below'} what analysts expected "
                        f"before the report ({_fmt_money(nxt['rev'])}). Guidance usually moves the stock more than the quarter."))
            break
        if x.get("metric") == "eps" and nxt.get("eps") and "year" not in per and "fiscal" not in per \
                and 0.5 < mid / nxt["eps"] < 2:
            gap = (mid / nxt["eps"] - 1) * 100
            out.append(("Guidance", "good" if gap > 0.5 else ("bad" if gap < -0.5 else "info"),
                        f"Next quarter's EPS guidance midpoint ${mid:.2f} is {abs(gap):.1f}% "
                        f"{'above' if gap > 0 else 'below'} the pre-report consensus of ${nxt['eps']:.2f}."))
            break
    # Raised or cut only when the guidance itself is the object: "increased ad
    # revenue" is a result, and "narrowed the range" is neither.
    tail = r"\s+(?:its|our|the)\s+(?:\S+\s+){0,3}(?:outlook|guidance|forecast|range|target|expectations?)"
    raised = [x for x in g if re.search(r"\b(?:rais(?:e|es|ed|ing)|increas(?:e|es|ed|ing)|lift(?:s|ed)?|boost(?:s|ed)?)" + tail, x["text"], re.I)]
    cut = [x for x in g if re.search(r"\b(?:lower(?:s|ed|ing)?|cut(?:s|ting)?|reduc(?:e|es|ed|ing)|trim(?:s|med)?)" + tail, x["text"], re.I)]
    for x in raised[:2]:
        out.append(("Guidance", "good", x["text"][:320]))
    for x in cut[:2]:
        out.append(("Guidance", "bad", x["text"][:320]))
    if not raised and not cut:
        for x in g[:3]:
            out.append(("Guidance", "info", x["text"][:320]))
    # Many companies give the outlook on the call, not in the release.
    if not g and cl.get("ok"):
        for x in (cl["analytics"].get("guidance") or [])[:5]:
            out.append(("Guidance", "info", f"On the call, {x['speaker']}: {x['text'][:300]}"))

    # Reaction
    rx = res.get("reaction") or {}
    imp = (res.get("consensus") or {}).get("implied") or {}
    if rx.get("move") is not None:
        s = f"The stock moved {rx['move']:+.1f}% ({rx.get('phase')})"
        if imp.get("move"):
            s += f" against ±{imp['move']:.1f}% priced by options — {rx['move'] / imp['move']:+.1f}× the implied move"
        if rx.get("mkt") is not None:
            s += f"; the S&P 500 did {rx['mkt']:+.1f}%"
        s += "."
        if rx.get("eur") is not None:
            s += f" On your position that is {rx['eur']:+,} euro."
        out.append(("Reaction", "good" if rx["move"] > 0 else "bad", s))
        if rx.get("gap") is not None and rx.get("intraday") is not None and abs(rx["gap"]) >= 2 and \
                (rx["gap"] > 0) != (rx["intraday"] > 0):
            out.append(("Reaction", "warn", f"It opened {rx['gap']:+.1f}% and then went the other way "
                                            f"({rx['intraday']:+.1f}% open to close): the market changed its mind "
                                            f"during the day — usually the call or the guidance detail."))
        if (eps.get("surprise") or 0) > 0 and rx["move"] < -1:
            out.append(("Reaction", "warn", "A beat that fell: the bar was higher than the consensus — "
                                            "look at guidance, margins and the segment the market watches."))
    after = res.get("analysts_after") or []
    ups = [a for a in after if (a.get("pt_action") or "").lower().startswith("rais")]
    downs = [a for a in after if (a.get("pt_action") or "").lower().startswith("lower")]
    if after:
        out.append(("Analysts", "good" if len(ups) > len(downs) else ("bad" if len(downs) > len(ups) else "info"),
                    f"Since the report: {len(ups)} price-target increase{'s' if len(ups) != 1 else ''}, "
                    f"{len(downs)} cut{'s' if len(downs) != 1 else ''}"
                    + (", and " + ", ".join(f"{a['firm']} {a['action']} to {a['to']}" for a in after
                                          if a.get("action") in ("up", "down"))[:200]
                       if any(a.get("action") in ("up", "down") for a in after) else "") + "."))

    # The call
    if cl.get("ok"):
        a = cl["analytics"]
        tp, ta = a["tone"]["prepared"], a["tone"]["answers"]
        vp = a.get("vs_prev")
        if vp and tp.get("net") is not None and (vp["tone_prepared"] or {}).get("net") is not None:
            dn = tp["net"] - vp["tone_prepared"]["net"]
            cmp_ = ("more upbeat than" if dn > 0.05 else "more guarded than" if dn < -0.05 else "about as upbeat as")
            out.append(("The call", "good" if dn > 0.05 else ("bad" if dn < -0.05 else "info"),
                        f"Management's prepared remarks were {cmp_} last quarter's "
                        f"(net tone {tp['net']:+.2f} vs {vp['tone_prepared']['net']:+.2f}); "
                        f"uncertainty words {tp['unc']:.1f} per 1,000 vs {vp['tone_prepared']['unc']:.1f}."))
        elif tp.get("net") is not None:
            out.append(("The call", "info", f"Net tone of the prepared remarks {tp['net']:+.2f}; "
                                            f"{tp['neg']:.1f} negative words per 1,000."))
        if ta.get("net") is not None and tp.get("net") is not None and ta["net"] < tp["net"] - 0.15:
            out.append(("The call", "warn", "The answers were noticeably less upbeat than the script — "
                                            "the Q&A is where the unscripted view comes out."))
        if vp and vp.get("new_topics"):
            out.append(("The call", "info", "New this quarter: " + ", ".join(vp["new_topics"][:6]) + "."))
        if vp and vp.get("dropped_topics"):
            out.append(("The call", "info", "No longer talked about: " + ", ".join(vp["dropped_topics"][:6]) + "."))
        if a["questions"]:
            topics = {}
            for q in a["questions"]:
                for t in q["topics"][:2]:
                    topics[t] = topics.get(t, 0) + 1
            top = sorted(topics.items(), key=lambda kv: -kv[1])[:5]
            out.append(("The call", "info", f"{a['analysts']} analysts asked {len(a['questions'])} questions"
                                            + (f"; most on {', '.join(t for t, _ in top)}" if top else "") + "."))
        if a.get("evasive"):
            out.append(("The call", "warn", f"{a['evasive']} answer{'s' if a['evasive'] != 1 else ''} declined to give "
                                            "the detail asked for (\"we don't break that out\", \"too early to say\")."))
        for q in a.get("quotes", [])[:3]:
            out.append(("Quotes", "info", f"{q['speaker']}: “{q['text']}”"))

    # Your thesis
    t = (research.thesis_get(ev["symbol"]) or {}).get("current") or {}
    if t.get("thesis") or t.get("kill"):
        if t.get("kill"):
            out.append(("Your thesis", "warn", f"You wrote you would be wrong if: {t['kill']} — check it against "
                                               "the numbers and the call above."))
        if t.get("thesis"):
            out.append(("Your thesis", "info", f"Your thesis: {t['thesis'][:400]}"))

    return {"ok": True, "symbol": ev["symbol"], "date": d,
            "lines": [{"section": s, "tone": tn, "text": x} for s, tn, x in out],
            "note": "Assembled by fixed rules from the figures on this page. For a written reading of the "
                    "documents, ask Claude (Report tab)."}


# --------------------------------------------------------------------------
# the Claude report
# --------------------------------------------------------------------------

SYSTEM = """You are an equity analyst writing for one private investor about a single company's quarterly earnings. The investor may own the stock; their position and their own thesis, if any, are in the material. They read your report after the results, the slides, the 10-Q and the call are out, and they want to know what happened and what it means for the company and the stock.

Work only from the material provided (and, if you were given web tools, from the company's own investor-relations pages). Where the material does not settle a point, say so rather than filling the gap. Separate what the company reported from your interpretation of it.

Tag every factual claim with its source in square brackets: [PR] press release, [Slides], [10-Q] or [10-K] the filed report, [Call] transcript, [Data] the figures computed by the investor's software (consensus, surprises, price reaction, options-implied move, history), [Thesis] the investor's own notes. Quote management sparingly and exactly.

Write in markdown, in this order:

# {Company} — {quarter}: {a verdict of at most twelve words}

**In short.** Three or four sentences: the result against expectations, the guidance, the one thing that changed, and what it means.

## Against expectations
A table: metric | reported | consensus | surprise | year on year. Revenue and EPS at least; add the two or three metrics this company is actually judged on (a segment, margin, a KPI) where the material gives them. Then two or three sentences on quality: one-offs, tax, share count, currency, what drove the beat or miss.

## Guidance and outlook
What they guided, against the consensus where known, and against what they said last quarter. Raised, cut, narrowed, reaffirmed, withdrawn.

## What changed in the story
The two to four developments that matter most for the long-term case: demand, pricing, margins, capital spending, competition, capital returns, management changes. Compare with last quarter where the material allows.

## The call
What management emphasised, what analysts pushed on, where answers were evasive or the tone shifted from the script.

## Risks and red flags
Only real ones from the material: accounting changes, working capital, guidance wording, customer concentration, one-off gains.

## What it means for the stock
The reaction against what options priced and against the stock's history. Whether the report changes the earnings path the valuation depends on. What looks priced in. Keep this analytical; do not tell the investor to buy or sell.

## Against your thesis
Only if a thesis is provided: which parts this quarter supports, which it strains, and whether any condition they said would prove them wrong was touched. Otherwise, one line saying no thesis was recorded.

## Next quarter: what to watch
Three to five concrete items with the number or statement that would confirm or break them.

End with a line listing which sources you had and which were missing.

Aim for 900 to 1,400 words. Plain, precise English; no filler, no disclaimers beyond the sources line."""


def _txt(label: str, body: str, meta: str = "") -> dict:
    return {"type": "text", "text": f'<document source="{label}"{(" " + meta) if meta else ""}>\n{body.strip()}\n</document>'}


def _gather(sym: str, d: str) -> dict:
    """Everything the report is written from, in a fixed order (the prompt cache needs it byte-stable)."""
    from . import earnings as E
    ev = E.resolve(sym, d)
    blocks, have, missing = [], [], []
    rel = E.release_parsed(ev["symbol"], d, ev.get("name")) or {}
    # 1. the press release and what was furnished with it
    if rel.get("ok") and rel.get("doc"):
        t = ed.release_text(rel["doc"]["url"])
        if t:
            blocks.append(_txt("PR", t, f'title="Press release, {rel["filing"]["form"]} filed {rel["filing"]["date"]}" url="{rel["doc"]["url"]}"'))
            have.append("press release")
        for x in rel.get("extras") or []:
            if x["kind"] == "slides" and x["doc"].lower().endswith(".pdf"):
                continue
            if x["kind"] in ("release", "supplement", "letter", "commentary", "slides", "exhibit"):
                t = ed.release_text(x["url"])
                if t:
                    tag = "Slides" if x["kind"] == "slides" else "PR"
                    blocks.append(_txt(tag, t, f'title="{x["kind"]}: {x.get("desc") or x["doc"]}" url="{x["url"]}"'))
                    have.append(x["kind"])
    else:
        missing.append("press release")
    # 2. slides: a PDF goes in as a PDF, so the charts are read too
    pdfs = [x["url"] for x in (rel.get("extras") or []) if x["doc"].lower().endswith(".pdf")]
    user = ed.list_sources(ev["symbol"], d)
    for u in user:
        if u["kind"] == "slides" and u.get("url") and u["url"].lower().split("?")[0].endswith(".pdf"):
            pdfs.append(u["url"])
    for url in pdfs[:2]:
        try:
            raw = ed.fetch(url)
            if ed.is_pdf(raw, url) and len(raw) <= MAX_PDF_BYTES:
                blocks.append({"type": "document", "title": "Slides",
                               "source": {"type": "base64", "media_type": "application/pdf",
                                          "data": base64.standard_b64encode(raw).decode("ascii")}})
                have.append("slides (PDF)")
        except Exception:
            missing.append(f"slides at {url} (could not be fetched)")
    for u in user:
        if u["kind"] == "slides" and u.get("body") and not (u.get("url") or "").lower().split("?")[0].endswith(".pdf"):
            blocks.append(_txt("Slides", u["body"], f'title="{u["title"]}"'))
            have.append("slides")
    if not any(h.startswith("slides") for h in have):
        missing.append("slides")
    # 3. the 10-Q's MD&A
    per = rel.get("periodic")
    if per:
        m = ed.mdna(per["url"])
        if m.get("found"):
            blocks.append(_txt(per["form"] if per["form"] in ("10-Q", "10-K") else "10-Q", m["text"], f'title="{per["form"]} MD&A, period {per.get("report")}" url="{per["url"]}"'))
            have.append(f"{per['form']} (MD&A)")
        else:
            missing.append(f"{per['form']} MD&A (the section could not be located)")
    else:
        missing.append("10-Q (not filed yet)")
    # 4. the call
    tr = E.transcript(ev["symbol"], d)
    if tr.get("ok"):
        t = tr["tr"]
        head = ""
        if t.get("participants"):
            head = "Participants: " + "; ".join(f"{p['name']} ({p['title']})" for p in t["participants"]) + "\n\n"
        blocks.append(_txt("Call", head + ed.transcript_plain(t), f'title="{t.get("title") or "Call transcript"}"'))
        have.append("call transcript")
    else:
        missing.append("call transcript")
    for u in user:
        if u["kind"] in ("release", "other") and u.get("body"):
            blocks.append(_txt("PR" if u["kind"] == "release" else "Other", u["body"], f'title="{u["title"]}"'))
            have.append(u["title"] or u["kind"])
    # 5. the figures this page computed, and the investor
    res = E.part(ev["symbol"], "results", d)
    pb = E.part(ev["symbol"], "playbook", d)
    ex = E.part(ev["symbol"], "expect", d)
    snap = E.snapshot(ev["symbol"], d) or {}
    data = {
        "company": ev.get("name"), "symbol": ev["symbol"], "report_date": d, "when": ev.get("when_label"),
        "consensus_before_report": res.get("consensus"),
        "next_quarter_consensus_before_report": snap.get("next"),
        "reported": {"eps": res.get("eps"), "eps_source": res.get("eps_source"),
                     "revenue": res.get("revenue"), "revenue_source": res.get("revenue_source"),
                     "revenue_yoy_pct": res.get("revenue_yoy"),
                     "release_eps": res.get("eps_release"), "filed_quarter_xbrl": res.get("filed")},
        "guidance_lines_parsed": [g["text"] for g in (res.get("guidance") or [])][:12],
        "last_quarter_guidance": [g["text"] for g in (ex.get("guidance_prev") or [])][:10],
        "price_reaction": res.get("reaction"),
        "options_implied_move_pct": ((res.get("consensus") or {}).get("implied") or {}).get("move"),
        "history_stats": pb.get("stats"),
        "history_last_8": (pb.get("history") or [])[-8:],
        "analyst_actions_since": res.get("analysts_after"),
        "sector": ev.get("sector"), "industry": ev.get("industry"), "market_cap": ev.get("market_cap"),
        "price": (ev.get("quote") or {}).get("price"), "currency": ev.get("currency"),
    }
    pos = ev.get("position")
    data["investor_position"] = ({"value_eur": pos.get("value_eur"), "weight_pct": pos.get("weight"),
                                  "listing": pos.get("as")} if pos else "not held")
    blocks.append(_txt("Data", json.dumps(data, indent=1, default=str), 'title="Figures computed by the investor\'s software"'))
    th = research.thesis_get(ev["symbol"]) or {}
    if th.get("current") or th.get("notes"):
        cur = th.get("current") or {}
        keep = {k: cur.get(k) for k in ("stance", "conviction", "target", "horizon", "thesis", "drivers", "risks", "kill",
                                        "created", "price_at") if cur.get(k)}
        notes = [{"date": n.get("created"), "note": n.get("body")} for n in (th.get("notes") or [])[:10]]
        blocks.append(_txt("Thesis", json.dumps({"thesis": keep, "notes": notes}, indent=1, default=str),
                           'title="The investor\'s own thesis and notes"'))
        have.append("your thesis")
    return {"ev": ev, "blocks": blocks, "have": have, "missing": missing}


# --------------------------------------------------------------------------
# who writes: Claude Code on your subscription, or the API on your key
# --------------------------------------------------------------------------

WRITERS = {"claude-code": "Claude Code — your Claude plan, no charge per report",
           # One setting for both write-ups: the earnings reports (ERN) and
           # the morning brief (ALRT).
           "api": "Anthropic API key — pay per report"}
CC_MODELS = {WRITE_MODEL: "Opus 5.5, high effort"}
CC_TIMEOUT = 20 * 60
TOOL_PHASE = {"Read": "reading the slides", "WebSearch": "searching the company's site",
              "WebFetch": "reading a page from the company's site"}
_CC = {"t": 0.0, "v": None}


def writer() -> str:
    w = store.get_setting("ern_writer") or "claude-code"
    return w if w in WRITERS else "claude-code"


def cc_model() -> str:
    """Claude Code's model: always WRITE_MODEL, never the plan's default."""
    return WRITE_MODEL


def cc_bases() -> list[str]:
    """
    Where the Claude desktop app keeps its copies of Claude Code.

    Two places, because the Microsoft Store build of the app is an MSIX
    package, and Windows gives a package a private AppData: what the app
    writes to %APPDATA%\\Claude really lands in
    %LOCALAPPDATA%\\Packages\\Claude_<id>\\LocalCache\\Roaming\\Claude. Programs
    started from inside the app see it under %APPDATA%; everything else — the
    server started by double-clicking start-portfolio.bat — sees only the
    real path. Looking in one place found Claude Code for a server started
    from the app and reported it missing for the same server started from
    Explorer.

    Since 20 October 2026 the app keeps its data in a folder of its own,
    %LOCALAPPDATA%\\Claude-Data\\claude_<id>, and leaves only an empty
    `.user-data-left.claude_<id>` marker in the two folders above; Claude Code
    runs from Claude-Data\\claude_<id>\\claude-code\\<version>\\<download>. The
    old places stay on the list for an app that has not moved yet.
    """
    import glob
    import os
    from pathlib import Path
    home = Path.home()
    roaming = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
    local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
    out = sorted(glob.glob(os.path.join(local, "Claude-Data", "*", "claude-code")))
    out += [os.path.join(roaming, "Claude", "claude-code")]
    out += sorted(glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude",
                                         "claude-code")))
    return [b for b in out if os.path.isdir(b)]


def cc_path() -> Optional[str]:
    """
    The Claude Code executable: one you named in Settings, one on PATH, or
    the newest copy the Claude desktop app keeps (a new version folder
    arrives with each update, so it is looked up every time).

    Two layouts. Up to October 2026 each version folder held claude.exe
    itself; since 2.1.293 it sits one folder deeper, in a folder named after
    the download (`2.1.295\\d2e29494ec83\\claude.exe`), next to a `.verified`
    marker. Looking only at the first layout reported Claude Code missing on
    every page the morning the app updated. Within one version, a verified
    copy wins over one still being downloaded, then the most recent.
    """
    import os
    import shutil
    p = (store.get_setting("claude_code_path") or "").strip()
    if p and os.path.isfile(p):
        return p
    w = shutil.which("claude")
    if w:
        return w

    def key(v):
        return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v))
    found = []
    for base in cc_bases():
        try:
            versions = os.listdir(base)
        except OSError:
            continue
        for v in versions:
            vdir = os.path.join(base, v)
            if not os.path.isdir(vdir):
                continue
            cands = [vdir]
            try:
                cands += [os.path.join(vdir, h) for h in os.listdir(vdir) if os.path.isdir(os.path.join(vdir, h))]
            except OSError:
                pass
            for d in cands:
                exe = os.path.join(d, "claude.exe")
                if os.path.isfile(exe):
                    found.append((key(v), d == vdir or os.path.isfile(os.path.join(d, ".verified")),
                                  os.path.getmtime(exe), exe))
    return max(found)[3] if found else None


def _cc_env() -> dict:
    """
    A clean environment. Anything a parent Claude session set (its proxy, its
    session ids) would make this run act as part of that session; and an
    ANTHROPIC_API_KEY in the environment would bill the key instead of using
    the subscription. Neither is wanted: this runs on your own sign-in.
    """
    import os
    return {k: v for k, v in os.environ.items()
            if not (k.upper().startswith("CLAUDE") or k.upper().startswith("ANTHROPIC"))}


def _no_window() -> int:
    import subprocess
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def cc_status(force: bool = False) -> dict:
    """Is Claude Code here, and signed in? Asked of the tool itself; kept a minute."""
    import subprocess
    if not force and _CC["v"] is not None and time.time() - _CC["t"] < 60:
        return _CC["v"]
    p = cc_path()
    out = {"found": bool(p), "path": p, "logged_in": False, "method": None, "version": None}
    if p:
        try:
            r = subprocess.run([p, "--version"], capture_output=True, timeout=30, env=_cc_env(),
                               creationflags=_no_window())
            out["version"] = r.stdout.decode("utf-8", "replace").strip().split(" ")[0] or None
            r = subprocess.run([p, "auth", "status", "--json"], capture_output=True, timeout=30,
                               env=_cc_env(), creationflags=_no_window())
            st = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
            out.update(logged_in=bool(st.get("loggedIn")), method=st.get("authMethod"),
                       subscription=st.get("subscriptionType"), email=st.get("email"),
                       provider=st.get("apiProvider"))
        except Exception as e:
            out["error"] = f"{e.__class__.__name__}: {e}"[:200]
    _CC.update(t=time.time(), v=out)
    return out


def cc_login() -> dict:
    """
    Open a console window running `claude auth login --claudeai`: it opens
    claude.ai in your browser, you sign in there, and Claude Code keeps the
    sign-in. Nothing here sees your password.
    """
    import subprocess
    p = cc_path()
    if not p:
        raise FileNotFoundError("Claude Code was not found on this computer")
    subprocess.Popen([p, "auth", "login", "--claudeai"], env=_cc_env(),
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    _CC["t"] = 0.0
    return {"opened": True, "path": p}


def _work_dir(sym: str, d: str) -> str:
    """One folder per report, next to the database: the only files Claude Code may read."""
    import os
    w = os.path.join(os.path.dirname(os.path.abspath(store.DB_PATH)), "ern_work", f"{sym}_{d}")
    os.makedirs(w, exist_ok=True)
    return w


def _prompt(sym: str, d: str, question: Optional[str], web: bool, g: dict) -> str:
    ev = g["ev"]
    if question:
        ask = (f"The investor has read your report on this quarter and asks:\n\n{question}\n\n"
               "Answer directly from the material, with the same source tags. As long as it needs, no longer.")
        prior = latest(sym, d, "report")
        if prior:
            ask = f"<your_report>\n{prior['body']}\n</your_report>\n\n" + ask
        return ask
    ask = (f"Write the earnings report for {ev.get('name')} ({ev['symbol']}), results of {d}."
           + (f" Sources missing from the material: {', '.join(g['missing'])}." if g["missing"] else ""))
    if web:
        ask += (" You have web search and fetch: use them only to find what is missing — the slides or the "
                "call transcript — on the company's own investor-relations site"
                + (f" ({ev['ir']})" if ev.get("ir") else "") + ". At most three searches.")
    return ask


def _save(sym: str, d: str, question: Optional[str], body: str, model_name: str, sources: dict,
          usage: dict, cost_usd: Optional[float]) -> int:
    store.init_db()
    with store.conn() as cn:
        cur = cn.execute("INSERT INTO earn_report (symbol,event_date,kind,question,created,model,body,sources,usage,cost_usd) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (sym, d, "answer" if question else "report", question,
                          datetime.now(timezone.utc).isoformat(timespec="seconds"), model_name, body,
                          json.dumps(sources), json.dumps(usage), cost_usd))
        return cur.lastrowid


def _run_api(job: dict, sym: str, d: str, question: Optional[str], web: bool, g: dict):
    import anthropic
    m = model()
    try:
        blocks = list(g["blocks"])
        # The last material block is the cache breakpoint: the same material,
        # asked a follow-up question within minutes, is read from the cache.
        if blocks:
            blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        ask = _prompt(sym, d, question, web, g)
        messages = [{"role": "user", "content": blocks + [{"type": "text", "text": ask}]}]
        kw = dict(model=m, max_tokens=32000, system=SYSTEM, messages=messages,
                  thinking={"type": "adaptive"}, output_config={"effort": WRITE_EFFORT})
        betas = []
        if m in FALLBACK_OK:
            betas.append("server-side-fallback-2026-07-01")
            kw["fallbacks"] = "default"
        if web and not question:
            kw["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3},
                           {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3}]
        client = _client()
        job["phase"] = "writing"
        usage_total, text_parts, served_by = {}, [], m
        for _round in range(4):
            with client.beta.messages.stream(betas=betas, **kw) as stream:
                for event in stream:
                    if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                        job["text"] += event.delta.text
                    elif event.type == "content_block_start" and getattr(event.content_block, "type", "") in (
                            "server_tool_use",):
                        job["phase"] = "searching the web"
                msg = stream.get_final_message()
            served_by = getattr(msg, "model", m) or m
            u = msg.usage
            for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                usage_total[k] = usage_total.get(k, 0) + (getattr(u, k, 0) or 0)
            stu = getattr(u, "server_tool_use", None)
            if stu is not None:
                usage_total["web_search_requests"] = usage_total.get("web_search_requests", 0) + \
                    (getattr(stu, "web_search_requests", 0) or 0)
            if msg.stop_reason == "refusal":
                raise RuntimeError("the model declined this request")
            text_parts += [b.text for b in msg.content if getattr(b, "type", "") == "text"]
            if msg.stop_reason == "pause_turn":
                kw["messages"] = messages + [{"role": "assistant", "content": msg.content}]
                job["phase"] = "writing (continuing after the web search)"
                continue
            if msg.stop_reason == "max_tokens":
                job["truncated"] = True
            break
        body = "\n".join(p for p in text_parts if p).strip()
        if not body:
            raise RuntimeError("the model returned no text")
        c = cost(served_by if served_by in MODELS else m, usage_total)
        _save(sym, d, question, body, served_by,
              {"have": g["have"], "missing": g["missing"], "web": web, "writer": "api"}, usage_total, c)
        job.update(state="done", phase="done", cost_usd=c, usage=usage_total, model=served_by)
    except anthropic.AuthenticationError:
        job.update(state="error", error="the Anthropic API key was refused — check it in Settings")
    except anthropic.PermissionDeniedError as e:
        job.update(state="error", error=f"the key may not use {m}: {getattr(e, 'message', e)}"[:300])
    except anthropic.RateLimitError:
        job.update(state="error", error="rate-limited by the API — try again in a minute")
    except anthropic.BadRequestError as e:
        job.update(state="error", error=f"the API refused the request: {getattr(e, 'message', e)}"[:400])
    except anthropic.APIStatusError as e:
        job.update(state="error", error=f"API error {e.status_code}: {getattr(e, 'message', e)}"[:300])
    except anthropic.APIConnectionError:
        job.update(state="error", error="could not reach the Anthropic API")


def cc_command(prompt_tools: str, resume: Optional[str] = None, system: Optional[str] = None) -> list[str]:
    """
    The Claude Code invocation: print mode, streamed JSON, this page's own
    system prompt in place of Claude Code's (it is writing a report, not
    code), only the tools the report needs, and none of your own Claude Code
    settings or hooks (--setting-sources project, and the folder has none).
    The morning brief passes its own system prompt.
    """
    p = cc_path()
    if not p:
        raise FileNotFoundError("Claude Code was not found on this computer")
    # No MCP servers (a signed-in account brings its claude.ai connectors, and
    # their tool definitions went with every run: a one-word test reply was
    # worth $0.65 at API prices), no Chrome integration, no skills list. All
    # of it is context a report never uses and your plan would pay for.
    cmd = [p, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--setting-sources", "project", "--effort", WRITE_EFFORT, "--model", WRITE_MODEL,
           "--strict-mcp-config", "--no-chrome", "--disable-slash-commands"]
    if prompt_tools:
        cmd += ["--tools", prompt_tools, "--allowedTools", prompt_tools]
    else:
        cmd += ["--tools", ""]
    if resume:
        cmd += ["--resume", resume]
    else:
        cmd += ["--system-prompt", system or SYSTEM]
    return cmd


def cc_stream(cmd: list[str], stdin_text: str, cwd: str, job: Optional[dict] = None,
              timeout: float = CC_TIMEOUT, phases: Optional[dict] = None) -> dict:
    """
    Run Claude Code and read its event stream: the text as it is written (for
    the page), which tool it is using, and the final result with the session
    id, the tokens and what the run would have cost at API prices.
    """
    import subprocess
    job = job if job is not None else {"text": "", "phase": ""}
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=_cc_env(), creationflags=_no_window())
    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    err_chunks: list = []
    threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True).start()
    result, model_name, session = None, None, None
    try:
        proc.stdin.write(stdin_text.encode("utf-8"))
        proc.stdin.close()
        for raw in proc.stdout:
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                model_name, session = ev.get("model"), ev.get("session_id")
                job["phase"] = "writing"
            elif t == "stream_event":
                e = ev.get("event") or {}
                if e.get("type") == "message_start":
                    job["text"] = ""            # a new turn: show the latest one
                elif e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                    job["text"] += e["delta"].get("text", "")
            elif t == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if b.get("type") == "tool_use":
                        job["phase"] = (phases or TOOL_PHASE).get(b.get("name"), f"using {b.get('name')}")
            elif t == "result":
                result = ev
        proc.wait(timeout=30)
    finally:
        killer.cancel()
    stderr = (err_chunks[0] if err_chunks else b"").decode("utf-8", "replace")
    if result is None:
        msg = stderr.strip() or "Claude Code stopped without an answer"
        if proc.returncode is not None and proc.returncode < 0 or "killed" in msg.lower():
            msg = f"Claude Code did not finish within {timeout // 60:.0f} minutes"
        raise RuntimeError(_cc_explain(msg))
    if result.get("is_error"):
        raise RuntimeError(_cc_explain(str(result.get("result") or result.get("subtype") or stderr)))
    return {"text": (result.get("result") or "").strip(), "session": result.get("session_id") or session,
            "model": model_name, "usage": result.get("usage") or {},
            "api_usd": result.get("total_cost_usd"), "turns": result.get("num_turns"),
            "ms": result.get("duration_ms")}


def _cc_explain(msg: str) -> str:
    low = msg.lower()
    if "not logged in" in low or "/login" in low or "please run" in low and "login" in low or "authenticat" in low:
        return "Claude Code is not signed in — press Sign in on the Report tab (a one-time step)"
    if "usage limit" in low or "rate limit" in low or "limit reached" in low:
        return "your Claude plan's usage limit is reached for now — " + msg[:200]
    return msg[:400]


def _run_cc(job: dict, sym: str, d: str, question: Optional[str], web: bool, g: dict):
    import os
    work = _work_dir(sym, d)
    ask = _prompt(sym, d, question, web, g)
    tools = "WebSearch,WebFetch" if web and not question else ""
    prior = latest(sym, d, "report") if question else None
    resume = ((prior or {}).get("sources") or {}).get("session") if question else None
    out = None
    if resume:
        # A follow-up continues the report's own conversation: the documents
        # are already in it and are not sent again.
        try:
            job["phase"] = "answering"
            out = cc_stream(cc_command("Read", resume), question_text(question), work, job)
        except RuntimeError as e:
            if "conversation" not in str(e).lower() and "session" not in str(e).lower():
                raise
            out = None                          # the session is gone: start afresh
    if out is None:
        parts, pdfs = [], []
        for b in g["blocks"]:
            if b["type"] == "text":
                parts.append(b["text"])
            elif b["type"] == "document" and b["source"].get("media_type") == "application/pdf":
                name = f"slides_{len(pdfs) + 1}.pdf"
                with open(os.path.join(work, name), "wb") as f:
                    f.write(base64.b64decode(b["source"]["data"]))
                pdfs.append(name)
        if pdfs:
            parts.append("The slides are PDF files in the current folder: " + ", ".join(pdfs) +
                         ". Read every page of them with the Read tool before you write — the charts carry "
                         "figures the text does not. [Slides] refers to them.")
            tools = ",".join(x for x in ("Read", tools) if x)
        stdin_text = "\n\n".join(parts) + "\n\n" + ask
        with open(os.path.join(work, "material.md"), "w", encoding="utf-8") as f:
            f.write(stdin_text)
        out = cc_stream(cc_command(tools), stdin_text, work, job)
    if not out["text"]:
        raise RuntimeError("Claude Code returned no text")
    name = f"Claude Code · {out['model'] or 'default model'}"
    usage = {**out["usage"], "api_equivalent_usd": out["api_usd"], "turns": out["turns"], "ms": out["ms"]}
    _save(sym, d, question, out["text"], name,
          {"have": g["have"], "missing": g["missing"], "web": web, "writer": "claude-code",
           "session": out["session"]}, usage, None)
    job.update(state="done", phase="done", cost_usd=None, api_usd=out["api_usd"], usage=usage, model=name)


def question_text(q: str) -> str:
    return (f"The investor has read your report on this quarter and asks:\n\n{q}\n\n"
            "Answer directly from the material, with the same source tags. As long as it needs, no longer.")


def _client():
    import anthropic
    # The base URL is pinned: a server started from inside another tool can
    # inherit an ANTHROPIC_BASE_URL meant for that tool, and the SDK reads it.
    return anthropic.Anthropic(api_key=api_key(), base_url="https://api.anthropic.com",
                               max_retries=2, timeout=900)


def _job_key(sym: str, d: str) -> str:
    return f"{sym}:{d}"


def _run(job: dict, sym: str, d: str, question: Optional[str], web: bool):
    try:
        job["phase"] = "gathering the documents"
        g = _gather(sym, d)
        job["have"], job["missing"] = g["have"], g["missing"]
        if job["writer"] == "claude-code":
            _run_cc(job, sym, d, question, web, g)
        else:
            _run_api(job, sym, d, question, web, g)
    except Exception as e:
        job.update(state="error", error=f"{e.__class__.__name__}: {e}"[:300])
    finally:
        job["finished"] = time.time()


def start(sym: str, d: str, question: Optional[str] = None, web: bool = False) -> dict:
    w = writer()
    if w == "api" and not api_key():
        raise PermissionError("no Anthropic API key — add one in Settings on the home screen, "
                              "or switch the report writer to Claude Code")
    if w == "claude-code":
        st = cc_status()
        if not st["found"]:
            raise PermissionError("Claude Code was not found on this computer — install the Claude desktop app, "
                                  "or switch the report writer to an API key in Settings")
        if not st["logged_in"]:
            st = cc_status(force=True)
            if not st["logged_in"]:
                raise PermissionError("Claude Code is not signed in yet — press Sign in on the Report tab")
    k = _job_key(sym, d)
    with _LOCK:
        j = JOBS.get(k)
        if j and j["state"] == "running":
            return _job_public(j)
        j = {"state": "running", "phase": "starting", "text": "", "started": time.time(), "finished": None,
             "question": question, "error": None, "writer": w,
             "model": (cc_model() or "default") if w == "claude-code" else model(), "web": web}
        JOBS[k] = j
    threading.Thread(target=_run, args=(j, sym, d, question, web), daemon=True, name=f"earnai-{sym}").start()
    return _job_public(j)


def _job_public(j: dict) -> dict:
    return {**j, "elapsed_s": round((j.get("finished") or time.time()) - j["started"])}


def stored(sym: str, d: str) -> list[dict]:
    store.init_db()
    with store.conn() as c:
        rows = c.execute("SELECT id,kind,question,created,model,body,sources,usage,cost_usd FROM earn_report "
                         "WHERE symbol=? AND event_date=? ORDER BY id", (sym, d)).fetchall()
    out = []
    for r in rows:
        x = dict(r)
        for k in ("sources", "usage"):
            try:
                x[k] = json.loads(x[k] or "{}")
            except Exception:
                x[k] = {}
        out.append(x)
    return out


def latest(sym: str, d: str, kind: str = "report") -> Optional[dict]:
    rs = [r for r in stored(sym, d) if r["kind"] == kind]
    return rs[-1] if rs else None


def delete(rid: int) -> bool:
    with store.conn() as c:
        return c.execute("DELETE FROM earn_report WHERE id=?", (rid,)).rowcount > 0


def report_state(sym: str, d: str) -> dict:
    j = JOBS.get(_job_key(sym, d))
    rs = stored(sym, d)
    m, w = model(), writer()
    return {"ok": True, "symbol": sym, "date": d, "writer": w, "writer_label": WRITERS[w],
            "has_key": bool(api_key()), "model": m,
            "model_label": MODELS[m][0], "models": {k: v[0] for k, v in MODELS.items()},
            "price": {"input": MODELS[m][1], "output": MODELS[m][2]},
            "cc": cc_status() if w == "claude-code" else None,
            "cc_model": cc_model(), "cc_model_label": CC_MODELS[cc_model()],
            "job": _job_public(j) if j else None,
            "reports": [r for r in rs if r["kind"] == "report"],
            "answers": [r for r in rs if r["kind"] == "answer"]}


