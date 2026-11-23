"""Read the scanned PTR filings that the PDF text parser cannot.

737 of 4,782 downloaded filings - one in six - are paper scans: one full-page
image per page, zero character objects, nothing for :mod:`parser` to read.  They
are not spread evenly.  They land on members who file on paper every time, and
33 of those members have no readable transaction at all, so the site shows them
with an empty record.  Ro Khanna is the largest: 80 filings, none readable, on
Armed Services and Ranking Member of the Select Committee on China.

The scans are 200 dpi and **1 bit per pixel** - already binarised by the
scanner, so there is no greyscale to recover and no more resolution to ask for.
Rendering at 350 dpi was tried and measured *worse* on dates, because it only
resamples the same pixels.

Approach: the House form is a printed grid, so use it as the schema instead of
asking OCR to infer layout from a whole page.

  1. :func:`find_grid` locates the table and its rules by morphology.
  2. Text cells - asset name, both dates - are cropped, enlarged 2x with cubic
     interpolation, lightly blurred, and read by Windows.Media.Ocr.  That recipe
     was chosen by measurement: against six date cells of known content it
     scored 6/6 where plain 2x scored 4/6 and a dilate-first variant scored 0/6.
  3. Tick cells - Purchase/Sale/Exchange and the eleven amount bands A-K - are
     never OCR'd.  The question is only "is there a mark here", which is a pixel
     count.  OCR on a lone glyph is exactly where an engine invents characters;
     a density test cannot.
  4. :mod:`ocr_template` repairs pages whose rules were too faint to all detect,
     by fitting them to the form the rest of the filing agrees on.

Measured on Khanna's 2026-08-07 filing (doc 9116267, 18 pages): 162
transactions recovered from 17 pages, 0 flagged ambiguous, page 1 being the
cover sheet which carries none.  Page 7 was checked cell by cell against the
rendered image - 19 of 19 rows correct in asset name, type, both dates and band.

Nothing here writes to ``transactions``.  Output goes to ``ocr_transactions``,
a staging table, because OCR'd rows carry a different and less certain
provenance than parsed ones and should be reviewed before they reach a number
the site publishes.

Requires ``winsdk`` (Windows.Media.Ocr), ``pypdfium2`` and ``opencv-python``.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

NATIVE_DPI = 200
COLUMNS = ["owner", "asset", "purchase", "sale", "exchange", "capgains",
           "partial", "tx_date", "notify_date",
           "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"]
BANDS = {
    "A": ("$1,001", "$15,000"), "B": ("$15,001", "$50,000"),
    "C": ("$50,001", "$100,000"), "D": ("$100,001", "$250,000"),
    "E": ("$250,001", "$500,000"), "F": ("$500,001", "$1,000,000"),
    "G": ("$1,000,001", "$5,000,000"), "H": ("$5,000,001", "$25,000,000"),
    "I": ("$25,000,001", "$50,000,000"), "J": ("Over $50,000,000", ""),
    "K": ("Spouse/DC asset over $1,000,000", ""),
}
TYPE_COLS = ["purchase", "sale", "exchange"]
TEXT_COLS = ["owner", "asset", "tx_date", "notify_date"]
INK_ON = 0.015
DOMINANCE = 1.6
EXAMPLE_RE = re.compile(r"example|mega\s*corp", re.I)
N_COLS = 20
TEMPLATE_TOL = 0.012

SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id        TEXT NOT NULL,
    page          INTEGER NOT NULL,
    seq           INTEGER NOT NULL,
    owner         TEXT,
    asset_name    TEXT,
    tx_type       TEXT,
    tx_date       TEXT,
    notification_date TEXT,
    amount_band   TEXT,
    amount_raw    TEXT,
    confident     INTEGER NOT NULL DEFAULT 1,
    repaired      INTEGER NOT NULL DEFAULT 0,
    read_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(doc_id, page, seq)
);
CREATE INDEX IF NOT EXISTS ix_ocr_doc ON ocr_transactions(doc_id);
"""


# --------------------------------------------------------------- OCR engine
def _engine_text(path: str) -> str:
    """Windows.Media.Ocr on one image file. Import is local: the module must be
    importable on the server, which has no display stack and never OCRs."""
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile

    async def run() -> str:
        f = await StorageFile.get_file_from_path_async(path)
        stream = await f.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        engine = (OcrEngine.try_create_from_language(Language("en-US"))
                  or OcrEngine.try_create_from_user_profile_languages())
        if engine is None:
            raise RuntimeError("no Windows OCR engine available")
        return (await engine.recognize_async(bitmap)).text

    return asyncio.run(run())


def available() -> bool:
    try:
        from winsdk.windows.media.ocr import OcrEngine  # noqa: F401
    except Exception:
        return False
    return True


# ------------------------------------------------------------ date recovery
_SLASHES = str.maketrans({"}": "/", "|": "/", "l": "/", "I": "/", "!": "/",
                          "\\": "/", ")": "/", "(": "/"})


def _valid_date(mm: str, dd: str, yy: str) -> str | None:
    try:
        m, d, y = int(mm), int(dd), int(yy)
    except ValueError:
        return None
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    full = y + 2000 if y < 100 else y
    if not (2000 <= full <= dt.date.today().year + 1):
        return None
    try:
        dt.date(full, m, d)
    except ValueError:
        return None
    return f"{m:02d}/{d:02d}/{full % 100:02d}"


def parse_date(raw: str) -> str | None:
    """Recover MM/DD/YY when the separator is unreliable.

    The engine reads the digits well and the slash badly: across tuning it came
    back as '1', '}', '|' or vanished, giving '08103/26', '07/02126',
    '08}03/26', '07116126'. So impose the structure rather than trust the
    punctuation, and validate the result as a real calendar date. Where a
    stray '1' could be either a digit or a slash and both readings validate,
    return None: a refused date costs one row, a wrong date corrupts a trade.
    """
    if not raw:
        return None
    s = raw.translate(_SLASHES)
    parts = "".join(c if c.isdigit() else " " for c in s).split()
    if len(parts) == 3:
        got = _valid_date(*parts)
        if got:
            return got
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) == 6:
        return _valid_date(digits[0:2], digits[2:4], digits[4:6])
    if len(digits) == 8:
        got = _valid_date(digits[0:2], digits[2:4], digits[4:8])
        if got:
            return got
        cands = set()
        for i in range(1, 7):
            if digits[i] != "1":
                continue
            for j in range(i + 1, 7):
                if digits[j] != "1":
                    continue
                t = digits[:i] + digits[i + 1:j] + digits[j + 1:]
                hit = _valid_date(t[0:2], t[2:4], t[4:6])
                if hit:
                    cands.add(hit)
        return cands.pop() if len(cands) == 1 else None
    if len(digits) == 7:
        cands = set()
        for i in range(1, 6):
            if digits[i] != "1":
                continue
            t = digits[:i] + digits[i + 1:]
            hit = _valid_date(t[0:2], t[2:4], t[4:6])
            if hit:
                cands.add(hit)
        return cands.pop() if len(cands) == 1 else None
    return None


def in_window(mmddyy: str, not_before: str | None, not_after: str | None) -> bool:
    """Is a recovered date plausible for a filing of this date?

    The year is the pair OCR gets wrong most often, and a wrong year is not
    obviously wrong: three rows of the sample filing came back as 2012 and 2002,
    which validate as dates and would have entered as real trades a decade out.
    """
    if not (not_before or not_after):
        return True
    m, d, y = (int(p) for p in mmddyy.split("/"))
    got = dt.date(2000 + y, m, d)
    if not_before and got < dt.date.fromisoformat(not_before):
        return False
    if not_after and got > dt.date.fromisoformat(not_after):
        return False
    return True


# --------------------------------------------------------------------- grid
def _binarize(gray: np.ndarray) -> np.ndarray:
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                 cv2.THRESH_BINARY_INV, 25, 15)


def _rules(bw: np.ndarray, axis: str, min_frac: float) -> np.ndarray:
    n = bw.shape[1] if axis == "h" else bw.shape[0]
    k = max(10, int(n * min_frac))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT,
                                       (k, 1) if axis == "h" else (1, k))
    return cv2.dilate(cv2.erode(bw, kernel), kernel)


def _centres(mask: np.ndarray, axis: int, min_run: int, min_len: int) -> list[int]:
    """Collapse a line mask to one coordinate per rule.

    ``min_len`` is absolute. Thresholding at a fraction of the *longest* run on
    the page instead silently drops every shorter rule - at 200 dpi that lost
    two row separators and shifted every row band by one, so cells were read
    from the wrong row while still looking plausible.
    """
    profile = (mask > 0).sum(axis=axis)
    hits = profile > min_len
    out, start = [], None
    for i, v in enumerate(hits):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start + i - 1) // 2)
            start = None
    if start is not None:
        out.append((start + len(hits) - 1) // 2)
    merged: list[int] = []
    for c in out:
        if merged and c - merged[-1] < min_run:
            merged[-1] = (merged[-1] + c) // 2
        else:
            merged.append(c)
    return merged


def find_grid(img_bgr: np.ndarray):
    """Locate the table, then its rules. Returns (xs, ys, content, edges)."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    bw = _binarize(gray)
    h, w = bw.shape

    # Table edges are the outer verticals - the longest vertical rules. Taking
    # them from the horizontal rules instead was tried and was worse: interior
    # header sub-dividers are also long, so any threshold admitting them puts
    # the edge well inside the table and every column lands against the wrong
    # origin. The horizontal extent stays as a fallback only.
    v_long = _rules(bw, "v", 0.10)
    col_profile = (v_long > 0).sum(axis=0)
    x_lo = x_hi = None
    if col_profile.max() >= h * 0.05:
        strong = np.where(col_profile > col_profile.max() * 0.6)[0]
        if strong.size >= 2:
            x_lo, x_hi = int(strong.min()), int(strong.max())
    if x_lo is None or (x_hi - x_lo) < w * 0.3:
        h_long = _rules(bw, "h", 0.30)
        runs = (h_long > 0).sum(axis=1)
        if runs.max() < w * 0.25:
            return None
        cols = np.where(h_long[int(np.argmax(runs))] > 0)[0]
        if cols.size < 2:
            return None
        x_lo, x_hi = int(cols[0]), int(cols[-1])
    table_w = x_hi - x_lo
    if table_w < w * 0.3:
        return None

    ys = _centres(_rules(bw[:, x_lo:x_hi], "h", 0.30), axis=1,
                  min_run=max(4, h // 250), min_len=int(table_w * 0.35))
    if len(ys) < 3:
        return None
    y_lo, y_hi = min(ys), max(ys)

    xs = _centres(_rules(bw[y_lo:y_hi, :], "v", 0.25), axis=0,
                  min_run=max(4, w // 300), min_len=int((y_hi - y_lo) * 0.30))
    xs = [x for x in xs if x_lo - 5 <= x <= x_hi + 5]

    # Ink with the printed rules removed. Measuring on raw `bw` counts a sliver
    # of the cell's own border whenever a boundary is a pixel or two out, which
    # it always is after a repair: every empty amount column read 0.03-0.05
    # against a marked column's 0.07, so a correct mark was never a *clear* one
    # and 52 correct rows were flagged ambiguous. The removal kernels are much
    # longer than any glyph, so the marks survive.
    lines = cv2.bitwise_or(_rules(bw, "h", 0.025), _rules(bw, "v", 0.025))
    lines = cv2.dilate(lines, np.ones((3, 3), np.uint8))
    content = cv2.bitwise_and(bw, cv2.bitwise_not(lines))
    return xs, ys, content, (x_lo, x_hi)


# ----------------------------------------------------------------- template
def build_template(page_xs: list[list[int]]) -> list[float] | None:
    """Median relative column boundaries over pages that detected all 21."""
    import statistics
    good = []
    for xs in page_xs:
        if len(xs) != N_COLS + 1:
            continue
        span = xs[-1] - xs[0]
        if span > 0:
            good.append([(x - xs[0]) / span for x in xs])
    if not good:
        return None
    return [statistics.median(c) for c in zip(*good)]


def fit_template(xs: list[int], template: list[float],
                 edges: tuple[int, int]) -> list[int] | None:
    """Place all 21 boundaries on a page that detected only some.

    Each detected rule is matched to the template slot it is nearest, a least
    squares line through those pairs gives offset and scale, and the missing
    boundaries are interpolated. Geometry only - it never invents a mark or a
    character, and a bad fit shows up as rows failing their date and mark tests.
    """
    import statistics
    if not xs or len(xs) < 6:
        return None
    lo, hi = edges
    span = hi - lo
    if span <= 0:
        return None
    pairs = []
    for x in xs:
        rel = (x - lo) / span
        j = min(range(len(template)), key=lambda k: abs(template[k] - rel))
        if abs(template[j] - rel) <= TEMPLATE_TOL:
            pairs.append((template[j], x))
    if len(pairs) < 6:
        return None
    ts = [t for t, _ in pairs]
    exs = [x for _, x in pairs]
    tbar, xbar = statistics.fmean(ts), statistics.fmean(exs)
    denom = sum((t - tbar) ** 2 for t in ts)
    if denom <= 0:
        return None
    a = sum((t - tbar) * (x - xbar) for t, x in pairs) / denom
    b = xbar - a * tbar
    if a <= 0:
        return None
    fitted = [int(round(a * t + b)) for t in template]
    if max(abs(a * t + b - x) for t, x in pairs) > span * 0.01:
        return None
    if any(fitted[i] >= fitted[i + 1] for i in range(len(fitted) - 1)):
        return None
    return fitted


# ------------------------------------------------------------------- cells
def _prep(crop: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    big = cv2.resize(g, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return cv2.GaussianBlur(big, (0, 0), 0.8)


def _cell_text(img: np.ndarray, box, tmp: Path, tag: str) -> str:
    x0, y0, x1, y1 = box
    crop = img[y0 + 4:y1 - 4, x0 + 4:x1 - 4]
    if crop.size == 0 or min(crop.shape[:2]) < 6:
        return ""
    im = cv2.copyMakeBorder(_prep(crop), 40, 40, 40, 40,
                            cv2.BORDER_CONSTANT, value=255)
    p = tmp / f"_c{tag}.png"
    Image.fromarray(im).save(p)
    try:
        return " ".join(_engine_text(str(p)).split())
    except Exception:
        return ""
    finally:
        p.unlink(missing_ok=True)


def _ink(content: np.ndarray, box) -> float:
    """Dark-pixel share of a cell interior. The inset is a fraction of the cell,
    not a fixed pixel count: on a 64px band column a fixed 5px inset admits the
    neighbour's mark once a boundary is a few pixels out."""
    x0, y0, x1, y1 = box
    px = max(3, int((x1 - x0) * 0.16))
    py = max(3, int((y1 - y0) * 0.14))
    a, b, c, d = y0 + py, y1 - py, x0 + px, x1 - px
    if b <= a or d <= c:
        return 0.0
    return float((content[a:b, c:d] > 0).mean())


def _pick(marks: dict[str, float]) -> tuple[str | None, bool]:
    live = {k: v for k, v in marks.items() if v >= INK_ON}
    if not live:
        return None, False
    order = sorted(live.items(), key=lambda kv: -kv[1])
    if len(order) == 1:
        return order[0][0], True
    return order[0][0], order[0][1] >= order[1][1] * DOMINANCE


def read_page(path: str, template: list[float] | None = None,
              not_before: str | None = None, not_after: str | None = None,
              edges_hint: tuple[int, int] | None = None) -> dict:
    img = cv2.imread(path)
    if img is None:
        return {"status": "unreadable", "rows": []}
    found = find_grid(img)
    if not found:
        return {"status": "no_table", "rows": []}
    xs, ys, content, edges = found
    repaired = False
    if len(xs) != N_COLS + 1:
        fitted = None
        if template:
            for e in ([edges_hint, edges] if edges_hint else [edges]):
                fitted = fit_template(xs, template, e)
                if fitted:
                    break
        if not fitted:
            return {"status": f"cols={len(xs)-1}", "rows": [], "xs": xs,
                    "edges": edges}
        xs, repaired = fitted, True

    tmp = Path(path).parent
    rows, skipped = [], 0
    for r in range(len(ys) - 1):
        y0, y1 = ys[r], ys[r + 1]
        if y1 - y0 < max(12, img.shape[0] // 120):
            continue
        box = lambda i: (xs[i], y0, xs[i + 1], y1)  # noqa: E731
        marks = {c: _ink(content, box(i))
                 for i, c in enumerate(COLUMNS) if c not in TEXT_COLS}
        ttype, type_clear = _pick({k: marks[k] for k in TYPE_COLS})
        if ttype is None:
            skipped += 1
            continue
        tx_raw = _cell_text(img, box(COLUMNS.index("tx_date")), tmp, f"{r}t")
        tx = parse_date(tx_raw)
        if not tx or not in_window(tx, not_before, not_after):
            skipped += 1
            continue
        asset = _cell_text(img, box(COLUMNS.index("asset")), tmp, f"{r}a")
        if EXAMPLE_RE.search(asset):
            skipped += 1
            continue
        band, band_clear = _pick({k: marks[k] for k in BANDS})
        rows.append({
            "owner": (_cell_text(img, box(COLUMNS.index("owner")), tmp, f"{r}o")
                      .upper().replace(" ", "")[:2] or None),
            "asset": asset,
            "tx_type": ttype,
            "tx_date": tx,
            "notify_date": parse_date(
                _cell_text(img, box(COLUMNS.index("notify_date")), tmp, f"{r}n")),
            "band": band,
            "amount": " - ".join(x for x in BANDS[band] if x) if band else None,
            "confident": bool(type_clear and band_clear and band),
            "repaired": repaired,
        })
    return {"status": "ok", "rows": rows, "skipped": skipped, "xs": xs,
            "edges": edges, "repaired": repaired}


ROTATIONS = {0: None, 90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
             270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def best_rotation(png_paths: list[Path], sample: int = 4) -> int:
    """Which quarter-turn puts the form the right way up.

    The landscape PTR form is often scanned onto a portrait page, so the whole
    table arrives rotated: Khanna's 2020-2025 filings are all like this, and
    they returned zero rows because the rule morphology was looking along the
    wrong axis. Orientation is a property of the filing, so it is decided once
    from a sample of pages by whichever turn recovers the most complete grids.
    """
    grid_score = {deg: 0 for deg in ROTATIONS}
    for p in png_paths[:sample]:
        img = cv2.imread(str(p))
        if img is None:
            continue
        for deg, code in ROTATIONS.items():
            trial = img if code is None else cv2.rotate(img, code)
            found = find_grid(trial)
            if not found:
                continue
            n = len(found[0])
            grid_score[deg] += 10 if n == N_COLS + 1 else (1 if n >= 12 else 0)

    # Geometry cannot tell 90 from 270, or 0 from 180: a table rotated by half a
    # turn is still a perfectly good table, and picking the wrong one of the
    # pair leaves every page upside down. That reads as a clean grid whose text
    # cells are all empty - which is exactly how Khanna's 2020-2025 filings
    # failed, silently and with no error. So decide the half-turn by which
    # orientation the recogniser can actually read.
    best = max(grid_score.values())
    if best == 0:
        return 0
    finalists = [d for d, s in grid_score.items() if s == best]
    if len(finalists) == 1:
        return finalists[0]
    probe = png_paths[len(png_paths) // 2] if len(png_paths) > 1 else png_paths[0]
    img = cv2.imread(str(probe))
    readable = {}
    for deg in finalists:
        code = ROTATIONS[deg]
        trial = img if code is None else cv2.rotate(img, code)
        tmp = Path(str(probe) + f".rot{deg}.png")
        cv2.imwrite(str(tmp), trial)
        try:
            text = _engine_text(str(tmp))
        except Exception:
            text = ""
        finally:
            tmp.unlink(missing_ok=True)
        readable[deg] = sum(c.isalnum() for c in text)
    return max(readable, key=lambda d: readable[d])


def read_filing(pdf_path: str, dpi: int = NATIVE_DPI,
                not_before: str | None = None, not_after: str | None = None):
    """Two passes: read every page, then repair the ones that failed using the
    form the rest of the filing agrees on."""
    import statistics
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_path)
    pages: list[dict] = []
    with tempfile.TemporaryDirectory() as td:
        paths = []
        for i in range(len(pdf)):
            png = Path(td) / f"p{i+1}.png"
            pdf[i].render(scale=dpi / 72).to_pil().save(png)
            paths.append(png)

        rot = best_rotation(paths)
        if rot:
            for p in paths:
                img = cv2.imread(str(p))
                cv2.imwrite(str(p), cv2.rotate(img, ROTATIONS[rot]))
        for i, png in enumerate(paths):
            res = read_page(str(png), not_before=not_before, not_after=not_after)
            res["page"] = i + 1
            pages.append(res)

        template = build_template([p.get("xs") or [] for p in pages])
        clean = [p["edges"] for p in pages
                 if len(p.get("xs") or []) == N_COLS + 1 and p.get("edges")]
        hint = None
        if clean:
            hint = (int(statistics.median(e[0] for e in clean)),
                    int(statistics.median(e[1] for e in clean)))
        if template:
            for i, png in enumerate(paths):
                if pages[i]["status"] == "ok":
                    continue
                res = read_page(str(png), template=template, edges_hint=hint,
                                not_before=not_before, not_after=not_after)
                res["page"] = i + 1
                if res["status"] == "ok":
                    pages[i] = res

    rows = []
    for p in pages:
        for j, r in enumerate(p.get("rows", [])):
            r["page"] = p["page"]
            r["seq"] = j
            rows.append(r)
    for p in pages:
        p["rotation"] = rot
    return rows, pages


def store(conn: sqlite3.Connection, doc_id: str, rows: list[dict]) -> int:
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM ocr_transactions WHERE doc_id = ?", (doc_id,))
    conn.executemany(
        """INSERT INTO ocr_transactions
           (doc_id, page, seq, owner, asset_name, tx_type, tx_date,
            notification_date, amount_band, amount_raw, confident, repaired)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(doc_id, r["page"], r["seq"], r["owner"], r["asset"], r["tx_type"],
          r["tx_date"], r["notify_date"], r["band"], r["amount"],
          int(r["confident"]), int(r["repaired"])) for r in rows])
    conn.commit()
    return len(rows)
