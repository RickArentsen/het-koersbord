"""
Live tankers for the oil map.

Two sources:

  Digitraffic (Fintraffic, Finland)   every ship the Finnish coastal stations
      hear in the northern Baltic, keyless. That is the sea Russia's Primorsk
      and Ust-Luga exports sail through, and much of the "shadow fleet".
  aisstream.io   the world, from a volunteer network of receivers; free, with
      a key you create at aisstream.io and paste into Settings. Coverage is good
      along most coasts and thin mid-ocean and in parts of the Gulf, where many
      tankers also switch their transponders off.

A position report does not say what kind of ship sent it; the static report
(type, name, destination, draught), broadcast every six minutes, does. So a
tanker is an AIS ship type of 80 to 89 learned from its static report, and that
table is kept between sessions so the next start recognises them at once.

The stream only runs while someone is looking: the map asks for tankers every
minute, and ten minutes without a request closes the connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Optional

from . import store, net

log = logging.getLogger(__name__)

AIS_URL = "wss://stream.aisstream.io/v0/stream"
IDLE_S = 600                 # close the stream ten minutes after the last look
FRESH_S = 3 * 3600           # a position older than this is not shown
SAVE_S = 300
TANKER = range(80, 90)
WORLD = [[[-90, -180], [90, 180]]]

DT = "https://meri.digitraffic.fi/api/ais/v1/"
DT_HEAD = {"Digitraffic-User": "PersonalBloomberg/oil-map", "Accept": "application/json"}


def api_key() -> Optional[str]:
    k = (store.get_setting("aisstream_api_key") or "").strip()
    return k or None


def is_tanker(t) -> bool:
    try:
        return int(t) in TANKER
    except (TypeError, ValueError):
        return False


class Stream:
    """One aisstream.io connection, run in its own thread with its own event loop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.state, self.detail, self.error = "off", "", None
        self.last_touch = 0.0
        self.started = None
        self.msgs = 0
        self.rate = 0.0
        self.statics: dict[int, list] = {}       # mmsi -> [type, name, destination, draught, imo, seen]
        self.pos: dict[int, list] = {}           # mmsi -> [lat, lon, sog, cog, heading, navstat, ts]
        self._loaded = False
        self._saved = 0.0

    # -- bookkeeping ----------------------------------------------------------
    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        hit = store.cache_get("ais:statics") or {}
        for k, v in (hit.get("s") or {}).items():
            try:
                self.statics[int(k)] = v
            except ValueError:
                continue
        last = store.cache_get("ais:last") or {}
        for k, v in (last.get("p") or {}).items():
            try:
                self.pos[int(k)] = v
            except ValueError:
                continue

    def _save(self, force: bool = False):
        now = time.time()
        if not force and now - self._saved < SAVE_S:
            return
        self._saved = now
        with self.lock:
            s = {str(k): v for k, v in self.statics.items()}
            p = {str(k): v for k, v in self.pos.items() if now - v[6] < FRESH_S}
        try:
            store.cache_put("ais:statics", {"s": s})
            store.cache_put("ais:last", {"p": p})
        except Exception as e:
            log.warning("ais save: %s", e)

    # -- the message handler (pure, so it can be tested) ----------------------
    def handle(self, m: dict, now: Optional[float] = None) -> Optional[str]:
        now = now or time.time()
        if not isinstance(m, dict):
            return None
        if m.get("error"):
            raise PermissionError(str(m["error"]))
        t = m.get("MessageType")
        if t == "SubscriptionConfirmation":
            return t
        md = m.get("MetaData") or {}
        mmsi = md.get("MMSI") or md.get("MMSI_String")
        try:
            mmsi = int(mmsi)
        except (TypeError, ValueError):
            return None
        body = (m.get("Message") or {}).get(t) or {}
        if t == "ShipStaticData":
            typ = body.get("Type")
            if is_tanker(typ):
                dim = body.get("MaximumStaticDraught")
                with self.lock:
                    self.statics[mmsi] = [int(typ), (body.get("Name") or md.get("ShipName") or "").strip(),
                                          (body.get("Destination") or "").strip(), dim, body.get("ImoNumber"), now]
            else:
                with self.lock:
                    self.statics.pop(mmsi, None)
                    self.pos.pop(mmsi, None)
            return t
        if t in ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"):
            if mmsi not in self.statics:
                return t
            lat = md.get("Latitude", md.get("latitude", body.get("Latitude")))
            lon = md.get("Longitude", md.get("longitude", body.get("Longitude")))
            if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
                return t
            with self.lock:
                self.pos[mmsi] = [round(lat, 4), round(lon, 4), body.get("Sog"), body.get("Cog"),
                                  body.get("TrueHeading"), body.get("NavigationalStatus"), now]
            return t
        return t

    # -- the connection ---------------------------------------------------------
    def touch(self):
        self.last_touch = time.time()
        self._load()
        if not api_key():
            self.state, self.detail = "off", "no aisstream.io key in Settings"
            return
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._run, name="ais-stream", daemon=True)
            self.thread.start()

    def _run(self):
        backoff = 5
        while time.time() - self.last_touch < IDLE_S:
            key = api_key()
            if not key:
                self.state, self.detail = "off", "no aisstream.io key in Settings"
                break
            try:
                asyncio.run(self._session(key))
                backoff = 5
            except PermissionError as e:
                self.state, self.error = "error", f"aisstream.io refused the key: {e}"
                time.sleep(60)
            except Exception as e:
                self.state, self.error = "reconnecting", f"{e.__class__.__name__}: {e}"[:200]
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
        self.state = "off" if self.state != "error" else self.state
        self.detail = self.detail or "closed: nobody is looking at the map"
        self._save(force=True)

    async def _session(self, key: str):
        from websockets.asyncio.client import connect
        self.state, self.detail = "connecting", "opening the stream"
        async with connect(AIS_URL, open_timeout=20, ping_interval=20, ping_timeout=40,
                           max_size=2 ** 22, compression="deflate") as ws:
            await ws.send(json.dumps({"APIKey": key, "BoundingBoxes": WORLD,
                                      "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}))
            self.started = self.started or time.time()
            n0, t0 = self.msgs, time.time()
            while time.time() - self.last_touch < IDLE_S:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    self.detail = "connected, no messages for a minute"
                    continue
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if self.handle(m) == "SubscriptionConfirmation" or self.state != "streaming":
                    self.state, self.detail, self.error = "streaming", "receiving", None
                self.msgs += 1
                if time.time() - t0 >= 30:
                    self.rate = round((self.msgs - n0) / (time.time() - t0), 1)
                    n0, t0 = self.msgs, time.time()
                self._save()

    # -- what the page gets -----------------------------------------------------
    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            rows = []
            for mmsi, p in self.pos.items():
                if now - p[6] > FRESH_S:
                    continue
                s = self.statics.get(mmsi) or [None, "", "", None, None, None]
                rows.append([p[0], p[1], p[2], p[3], s[1], s[2], s[3], s[0], mmsi, round(now - p[6])])
            known = len(self.statics)
        return {"tankers": rows, "state": self.state, "detail": self.detail, "error": self.error,
                "rate": self.rate, "msgs": self.msgs, "known_tankers": known, "has_key": bool(api_key()),
                "since": self.started}


STREAM = Stream()


# ---- the Baltic, keyless ---------------------------------------------------

def _load_baltic_meta() -> dict:
    raw = json.loads(net.get(DT + "vessels", headers=DT_HEAD, timeout=60))
    out = {}
    for v in raw:
        if is_tanker(v.get("shipType")):
            out[str(v["mmsi"])] = [int(v["shipType"]), (v.get("name") or "").strip(),
                                   (v.get("destination") or "").strip(),
                                   (v.get("draught") or 0) / 10 if v.get("draught") else None, v.get("imo")]
    return {"m": out, "fetched_at": time.time()}


def parse_baltic(loc: dict, meta: dict, now_ms: Optional[float] = None) -> list[list]:
    now_ms = now_ms or time.time() * 1000
    rows = []
    for f in loc.get("features") or []:
        s = meta.get(str(f.get("mmsi")))
        if not s:
            continue
        pr = f.get("properties") or {}
        ts = pr.get("timestampExternal") or 0
        if now_ms - ts > FRESH_S * 1000:
            continue
        lon, lat = (f.get("geometry") or {}).get("coordinates") or [None, None]
        if lat is None:
            continue
        rows.append([round(lat, 4), round(lon, 4), pr.get("sog"), pr.get("cog"), s[1], s[2], s[3], s[0],
                     f.get("mmsi"), round((now_ms - ts) / 1000)])
    return rows


def _load_baltic() -> dict:
    meta = store.cache_through("oil:baltic:meta", 3600, _load_baltic_meta).get("m") or {}
    loc = json.loads(net.get(DT + "locations", headers=DT_HEAD, timeout=60))
    return {"tankers": parse_baltic(loc, meta), "fetched_at": time.time()}


def baltic() -> dict:
    return store.cache_through("oil:baltic", 120, _load_baltic)


def tankers() -> dict:
    """Everything live: the Baltic always, the world when the stream has a key."""
    STREAM.touch()
    out = {"world": STREAM.snapshot(), "fetched_at": time.time()}
    try:
        b = baltic()
        out["baltic"] = {"tankers": b.get("tankers") or [], "age_s": b.get("_age_s")}
    except Exception as e:
        out["baltic"] = {"tankers": [], "error": f"{e.__class__.__name__}: {e}"[:200]}
    return out
