"""
The oil map's layers, in latitude and longitude. The page draws them on
static/land.json (Natural Earth, public domain) with a projection of its own,
so a port or a tanker lands exactly where it is.

    chokepoints  IMF PortWatch's 28, with the week's tanker transits
    ports        every PortWatch port that handles tankers, sized by the cargo
                 loaded and discharged over the last month of data
    production   each country's latest crude output, for the shading
    pipelines    the main crude export lines (schematic; oil_seed.json)
    platforms    offshore platforms: Norway's official list (Sodir) and
                 OpenStreetMap everywhere else — slow to fetch, so it loads in
                 the background and is kept for a month
    tankers      live: see ais.py
"""

from __future__ import annotations

import csv
import io
import json
import threading
import time
import urllib.parse
from datetime import date

from . import store, net, oil

PLATFORM_TTL = 30 * 86400
PARTIAL_TTL = 3600         # when OpenStreetMap did not answer: try again in an hour
OVERPASS = ["https://overpass.kumi.systems/api/interpreter",
            "https://overpass-api.de/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
OVERPASS_Q = '[out:json][timeout:280];nwr["man_made"="offshore_platform"];out center tags;'
SODIR = ("https://factpages.sodir.no/public?/Factpages/external/tableview/facility_fixed&rs:Command=Render"
         "&rc:Toolbar=false&rc:Parameters=f&IpAddress=not_used&CultureCode=en&rs:Format=CSV&Top100=false")


def part_map() -> dict:
    out = {"fetched_at": time.time(), "errors": {}}
    try:
        f = oil.part_flows()
        out["chokepoints"] = [{k: c.get(k) for k in ("id", "name", "lat", "lon", "w7", "y7", "vs_y7", "status",
                                                     "oil", "geo", "note", "end")} for c in f["chokepoints"]]
        out["flows_latest"] = f.get("latest")
    except Exception as e:
        out["errors"]["chokepoints"] = f"{e.__class__.__name__}: {e}"[:200]
    try:
        db, pm = oil.ports_db(), oil.port_month()
        days = 30
        if pm.get("latest") and pm.get("since"):
            days = max(1, (date.fromisoformat(pm["latest"]) - date.fromisoformat(pm["since"])).days + 1)
        ports = []
        by3 = oil.countries()["by3"]
        for pid, p in db.items():
            x = pm["ports"].get(pid)
            if not x or x[2] < 3 or p.get("lat") is None:
                continue
            ports.append({"id": pid, "name": p["portname"], "iso3": p.get("ISO3"), "iso": by3.get(p.get("ISO3")),
                          "country": p.get("country"),
                          "lat": round(p["lat"], 3), "lon": round(p["lon"], 3),
                          "exp": round(x[0] / days), "imp": round(x[1] / days), "calls": int(x[2])})
        out["ports"] = sorted(ports, key=lambda p: -(p["exp"] + p["imp"]))
        out["ports_window"] = {"since": pm.get("since"), "latest": pm.get("latest"), "days": days}
    except Exception as e:
        out["errors"]["ports"] = f"{e.__class__.__name__}: {e}"[:200]
    try:
        pr = oil.production()
        out["production"] = {iso: {"kbd": r.get("kbd"), "d12": r.get("d12"), "month": r.get("month"),
                                   "src": r.get("src"), "kind": r.get("kind"), "name": r.get("name")}
                             for iso, r in pr["rows"].items() if r.get("kbd")}
    except Exception as e:
        out["errors"]["production"] = f"{e.__class__.__name__}: {e}"[:200]
    out["pipelines"] = (oil.seed().get("pipelines") or {})
    return out


# ---- platforms ------------------------------------------------------------

def _dms(deg, mi, sec, code) -> float | None:
    try:
        v = float(deg) + float(mi or 0) / 60 + float(sec or 0) / 3600
    except (TypeError, ValueError):
        return None
    return -v if (code or "").strip().upper() in ("S", "W") else v


def parse_sodir(text: str) -> list[list]:
    """Norway's installations that stand above the sea and are in service."""
    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        if (r.get("fclPhase") or "").strip().upper() != "IN SERVICE":
            continue
        if (r.get("fclSurface") or "").strip().upper() not in ("Y", "YES"):
            continue
        lat = _dms(r.get("fclNsDeg"), r.get("fclNsMin"), r.get("fclNsSec"), r.get("fclNsCode"))
        lon = _dms(r.get("fclEwDeg"), r.get("fclEwMin"), r.get("fclEwSec"), r.get("fclEwCode"))
        if lat is None or lon is None:
            continue
        kind = (r.get("fclKind") or "").strip().lower()
        if "wind" in kind or "onshore" in kind:
            continue
        out.append([round(lat, 3), round(lon, 3), "sodir", "", (r.get("fclName") or "")[:40],
                    (r.get("fclCurrentOperatorName") or "")[:40], kind[:24]])
    return out


def parse_overpass(d: dict) -> list[list]:
    out = []
    for e in d.get("elements") or []:
        lat, lon = e.get("lat"), e.get("lon")
        if lat is None:
            c = e.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            continue
        t = e.get("tags") or {}
        sub = (t.get("substance") or t.get("product") or "").lower()
        kind = "gas" if "gas" in sub and "oil" not in sub else ("oil" if "oil" in sub else "")
        name = t.get("name") or t.get("seamark:name") or t.get("ref") or ""
        out.append([round(lat, 3), round(lon, 3), "osm", kind, name[:40], (t.get("operator") or "")[:40], ""])
    return out


def merge_platforms(osm: list[list], sodir: list[list], km: float = 2.0) -> list[list]:
    """Sodir's list wins in Norwegian waters: drop an OSM point within `km` of an official one."""
    cell = 0.05
    grid: dict = {}
    for p in sodir:
        grid.setdefault((int(p[0] / cell), int(p[1] / cell)), []).append(p)
    keep = []
    lim = (km / 111.0) ** 2
    for p in osm:
        gx, gy = int(p[0] / cell), int(p[1] / cell)
        near = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for q in grid.get((gx + dx, gy + dy), ()):
                    if (p[0] - q[0]) ** 2 + ((p[1] - q[1]) * 0.55) ** 2 < lim:
                        near = True
                        break
                if near:
                    break
            if near:
                break
        if not near:
            keep.append(p)
    return sodir + keep


def _load_platforms() -> dict:
    errs, osm, sodir = {}, [], []
    for ep in OVERPASS:
        # Two transports, not four: Overpass can take minutes either way, and
        # retrying the same slow query through urllib only adds to the wait.
        r = net.fetch(ep + "?data=" + urllib.parse.quote(OVERPASS_Q), headers={"Accept": "*/*"}, timeout=320,
                      order=["curl_cffi", "requests"])
        if r["ok"]:
            try:
                osm = parse_overpass(json.loads(r["body"]))
                if osm:
                    break
            except Exception as e:
                errs[ep] = f"{e.__class__.__name__}: {e}"[:120]
        else:
            errs[ep] = (r.get("error") or "")[:120]
    try:
        sodir = parse_sodir(net.get(SODIR, timeout=90).decode("utf-8", "replace"))
    except Exception as e:
        errs["sodir"] = f"{e.__class__.__name__}: {e}"[:120]
    if not osm and not sodir:
        raise RuntimeError("; ".join(errs.values()) or "no platform source answered")
    pts = merge_platforms(osm, sodir)
    # Without OpenStreetMap this is Norway alone: keep it, but only for an hour.
    return {"p": pts, "n_osm": len(osm), "n_sodir": len(sodir), "errors": errs, "partial": not osm,
            "fetched_at": time.time()}


_PL = {"running": False, "error": None}


def platforms() -> dict:
    """
    The platforms, from the cache. When there is none, or it is a month old, a
    background thread fetches them (OpenStreetMap's query takes minutes); the
    page asks again a little later.
    """
    from . import BUILD
    hit = store.cache_get("oil:platforms")
    ttl = PARTIAL_TTL if (hit or {}).get("partial") else PLATFORM_TTL
    fresh = bool(hit) and hit.get("_build") == BUILD and (hit.get("_age_s") or 0) < ttl
    if not fresh and not _PL["running"]:
        _PL["running"] = True

        def run():
            try:
                store.cache_put("oil:platforms", _load_platforms())
                _PL["error"] = None
            except Exception as e:
                _PL["error"] = f"{e.__class__.__name__}: {e}"[:200]
            finally:
                _PL["running"] = False
        threading.Thread(target=run, name="oil-platforms", daemon=True).start()
    if hit:
        return {**hit, "refreshing": not fresh}
    return {"loading": True, "error": _PL["error"]}
