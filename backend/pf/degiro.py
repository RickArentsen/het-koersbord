"""
DEGIRO CSV importers.

Two exports are needed and they are not interchangeable:

  Transactions.csv  -> the trades (buys/sells), with EUR value, FX rate,
                       AutoFX cost and transaction fees already broken out.
  Account.csv       -> the cash ledger: dividends, dividend withholding tax,
                       interest, fees, deposits/withdrawals and the paired
                       currency-conversion rows.

Both are parsed into the same normalised Txn shape so the rest of the system
never has to know DEGIRO exists.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional


# --------------------------------------------------------------------------
# Dutch-locale scalar parsing
# --------------------------------------------------------------------------

def parse_amount(raw: Optional[str]) -> Optional[float]:
    """'-1.234,56' -> -1234.56 ; '' / None -> None."""
    if raw is None:
        return None
    s = raw.strip().replace("\xa0", "").replace(" ", "")
    if not s:
        return None
    # thousands separator is '.', decimal separator is ','
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_dt(d: str, t: str = "") -> datetime:
    """'03-09-2026' + '16:30' -> datetime. Day-first, always."""
    d = (d or "").strip()
    t = (t or "").strip()
    if t:
        return datetime.strptime(f"{d} {t}", "%d-%m-%Y %H:%M")
    return datetime.strptime(d, "%d-%m-%Y")


def parse_d(d: str) -> Optional[date]:
    d = (d or "").strip()
    if not d:
        return None
    return datetime.strptime(d, "%d-%m-%Y").date()


# --------------------------------------------------------------------------
# Instrument classification
# --------------------------------------------------------------------------

_DERIV_TOKENS = (
    "TURBO", "WARRANT", "FACTOR", " CALL ", " PUT ",
    "SPRINTER", "DISCOUNT", "BONUS CERT", "LEV ",
)
_ETF_TOKENS = ("UCITS ETF", " ETF", " ETC", "ISHARES", "VANGUARD", "INVESCO PHYSICAL")

# issuer prefixes on Dutch/German structured products
_ISSUER_RE = re.compile(r"^(SG|BNP|ING|RBS|CITI|UBS|GS)\s+", re.I)
# everything from the first product-type keyword onwards is not the underlying
_PRODUCT_RE = re.compile(
    r"\s+(CLASSIC|BEST|TURBO|WARRANT|FACTOR|SPRINTER|MINI|DISCOUNT|BONUS)\b", re.I
)


def classify(name: str, isin: str) -> str:
    """EQUITY | ETF | DERIVATIVE."""
    n = f" {(name or '').upper()} "
    if any(tok in n for tok in _DERIV_TOKENS):
        return "DERIVATIVE"
    if any(tok in n for tok in _ETF_TOKENS):
        return "ETF"
    return "EQUITY"


# Leveraged products carry no sector of their own. They do carry the name of
# the share they track, which is the sector that actually matters to you.
UNDERLYING_TICKERS = {
    "ABBOTT LABS": "ABT",          "ABBVIE": "ABBV",
    "ADOBE": "ADBE",               "ADVANCED MICRO DEV": "AMD",
    "AMAZON": "AMZN",              "APPLE": "AAPL",
    "COINBASE": "COIN",            "EXELON CORP": "EXC",
    "FEDEX": "FDX",                "FIGMA": "FIG",
    "GENERAL MILLS": "GIS",        "IBM": "IBM",
    "INTUIT": "INTU",              "INTUITIVE SURGICAL": "ISRG",
    "META PLATFORMS": "META",      "NVIDIA": "NVDA",
    "SNDK": "SNDK",                "SNOWFLAKE": "SNOW",
    "TRADE DESK": "TTD",           "UPS": "UPS",
    "VALERO ENERGY": "VLO",        "VISTRA ENERGY": "VST",
    "ALPHABET": "GOOGL",           "MICROSOFT": "MSFT",
    "TESLA": "TSLA",               "NETFLIX": "NFLX",
    "BROADCOM": "AVGO",            "SALESFORCE": "CRM",
    "ORACLE": "ORCL",              "MICRON": "MU",
    "PALANTIR": "PLTR",            "SHOPIFY": "SHOP",
    "UBER": "UBER",                "WALMART": "WMT",
    "DISNEY": "DIS",               "BOEING": "BA",
    "PAYPAL": "PYPL",              "STARBUCKS": "SBUX",
    # added 16 October 2026: the Taiwanese one is the dollar ADR, which is what
    # SG's turbo is struck in (FN 51.0169 at a share near $440)
    "WESTERN DIGITAL": "WDC",      "TAIWAN SEMICONDUCTOR": "TSM",
    "SANDISK": "SNDK",             "ADVANCED MICRO DEVICES": "AMD",
    "ARM HOLDINGS": "ARM",         "ASML": "ASML",
    "COREWEAVE": "CRWV",           "CREDO TECHNOLOGY": "CRDO",
    "BLOOM ENERGY": "BE",          "SUPER MICRO COMPUTER": "SMCI",
    "APPLIED DIGITAL": "APLD",     "NEBIUS": "NBIS",
    "COHERENT": "COHR",            "ALIBABA": "BABA",
    "APPLOVIN": "APP",             "ROBINHOOD": "HOOD",
    "QUALCOMM": "QCOM",            "INTEL CORP": "INTC",
    "MASTERCARD": "MA",            "VISA": "V",
    "BERKSHIRE HATHAWAY": "BRK-B", "ELI LILLY": "LLY",
    "NOVO NORDISK": "NVO",         "UNITEDHEALTH": "UNH",
    "EXXON MOBIL": "XOM",          "CHEVRON": "CVX",
    "OCCIDENTAL": "OXY",           "CONOCOPHILLIPS": "COP",
    "MERCADOLIBRE": "MELI",        "SERVICENOW": "NOW",
    "CROWDSTRIKE": "CRWD",         "PALO ALTO NETWORKS": "PANW",
    "VERTIV": "VRT",               "CONSTELLATION ENERGY": "CEG",
    "STRATEGY": "MSTR",            "MICROSTRATEGY": "MSTR",
}


def underlying_ticker_of(underlying: Optional[str]) -> Optional[str]:
    """Best-effort underlying name -> quote symbol. Exact, then prefix match."""
    if not underlying:
        return None
    u = underlying.upper().strip(" .,-")
    if u in UNDERLYING_TICKERS:
        return UNDERLYING_TICKERS[u]
    for k, v in UNDERLYING_TICKERS.items():
        if u.startswith(k) or k.startswith(u):
            return v
    return None


def underlying_of(name: str) -> Optional[str]:
    """
    'SG META PLATFORMS CLASSIC TURBO LONG SL 205.23 ...' -> 'META PLATFORMS'
    'BNP IBM TURBO LONG SL 98.5 ...'                     -> 'IBM'
    Best-effort: used to roll leveraged products up to their underlying
    for exposure, never for pricing.
    """
    if not name:
        return None
    s = _ISSUER_RE.sub("", name.strip())
    m = _PRODUCT_RE.search(" " + s)
    if m:
        s = (" " + s)[: m.start()].strip()
    return s.strip(" ,.-") or None


def _unused():  # pragma: no cover
    pass


# --------------------------------------------------------------------------
# Normalised transaction
# --------------------------------------------------------------------------

@dataclass
class Txn:
    kind: str                      # BUY SELL DIVIDEND DIV_TAX FEE INTEREST FX DEPOSIT WITHDRAWAL
    trade_dt: datetime
    settle_date: Optional[date] = None
    isin: Optional[str] = None
    product: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None          # in `currency`
    currency: Optional[str] = None         # currency of price / local value
    local_value: Optional[float] = None    # signed, in `currency`
    value_eur: Optional[float] = None      # signed, EUR, before costs
    fx_rate: Optional[float] = None        # units of `currency` per 1 EUR
    autofx_cost_eur: float = 0.0           # always <= 0
    fee_eur: float = 0.0                   # always <= 0
    total_eur: Optional[float] = None      # value_eur + autofx + fee
    exchange: Optional[str] = None
    venue: Optional[str] = None
    order_id: Optional[str] = None
    balance: Optional[float] = None        # DEGIRO's own running Saldo
    balance_ccy: Optional[str] = None
    seq: Optional[int] = None              # row index in the export, newest = 0
    source: str = ""
    external_id: str = ""
    note: str = ""

    def as_dict(self):
        d = asdict(self)
        d["trade_dt"] = self.trade_dt.isoformat()
        d["settle_date"] = self.settle_date.isoformat() if self.settle_date else None
        return d


def _ext_id(source: str, order_id: Optional[str], *parts) -> str:
    """
    Stable id for idempotent re-import.

    DEGIRO's Order ID is NOT unique: a single order that fills in several
    tranches writes one row per fill, all sharing the order id, and some
    rows carry no order id at all. So the id is always a hash of the full
    row content, with the order id folded in as one more component.
    """
    payload = "|".join([source, order_id or ""] + [str(p) for p in parts])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


# --------------------------------------------------------------------------
# Transactions.csv
# --------------------------------------------------------------------------

TXN_HEADER_START = "Datum,Tijd,Product,ISIN,Beurs"


def parse_transactions(path: str) -> list[Txn]:
    """
    Columns (by index — the header has two unnamed currency columns):
      0 Datum  1 Tijd  2 Product  3 ISIN  4 Beurs  5 Uitvoeringsplaats
      6 Aantal (signed)  7 Koers  8 <ccy of Koers>
      9 Lokale waarde   10 <ccy of Lokale waarde>
     11 Waarde EUR  12 Wisselkoers  13 AutoFX Kosten
     14 Transactiekosten EUR  15 Totaal EUR  16 Order ID
    """
    out: list[Txn] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))

    if not rows:
        return out
    body = rows[1:] if rows[0] and rows[0][0].strip() == "Datum" else rows

    for r in body:
        if not r or not (r[0] or "").strip():
            continue
        r = r + [""] * (17 - len(r))

        qty = parse_amount(r[6])
        if qty is None:
            continue

        dt = parse_dt(r[0], r[1])
        value_eur = parse_amount(r[11])
        autofx = parse_amount(r[13]) or 0.0
        fee = parse_amount(r[14]) or 0.0
        total = parse_amount(r[15])
        price = parse_amount(r[7])

        t = Txn(
            kind="BUY" if qty > 0 else "SELL",
            trade_dt=dt,
            settle_date=dt.date(),
            isin=(r[3] or "").strip() or None,
            product=(r[2] or "").strip() or None,
            quantity=qty,
            price=price,
            currency=(r[8] or "").strip() or None,
            local_value=parse_amount(r[9]),
            value_eur=value_eur,
            fx_rate=parse_amount(r[12]),
            autofx_cost_eur=autofx,
            fee_eur=fee,
            total_eur=total,
            exchange=(r[4] or "").strip() or None,
            venue=(r[5] or "").strip() or None,
            order_id=(r[16] or "").strip() or None,
            source="degiro:transactions",
        )
        t.external_id = _ext_id(
            "dg-tx", t.order_id, dt.isoformat(), t.isin, qty, price, value_eur, total
        )
        out.append(t)

    out.sort(key=lambda x: x.trade_dt)
    return out


# --------------------------------------------------------------------------
# Account.csv
# --------------------------------------------------------------------------

# Omschrijving -> kind. Checked in order, so the most specific phrase wins.
#
# INTERNAL is the important one: DEGIRO sweeps idle cash to the flatex bank
# account and back, and holds a matching +/- pair against every iDEAL deposit.
# Both net to zero and neither is money entering or leaving the portfolio.
# Counting them as deposits would wreck every cashflow-based figure.
_DESC_MAP = [
    ("dividendbelasting", "DIV_TAX"),
    ("dividend", "DIVIDEND"),
    ("adr/gdr", "FEE"),
    ("transactiekosten", "FEE"),
    ("aansluitingskosten", "FEE"),
    ("connection fee", "FEE"),
    # "Processed Flatex Withdrawal" moves cash between DEGIRO and your own
    # flatex bank account — internal, and it appears twice (out, then back).
    # "flatex terugstorting" is the actual payout to your outside bank.
    ("processed flatex withdrawal", "INTERNAL"),
    ("terugstorting", "WITHDRAWAL"),
    ("flatex interest income", "INTEREST"),
    ("rente", "INTEREST"),
    ("degiro cash sweep transfer", "INTERNAL"),
    ("overboeking", "INTERNAL"),          # flatex bank sweep, descriptive leg
    ("reservation ideal", "INTERNAL"),    # the hold against a pending deposit
    ("ideal deposit", "DEPOSIT"),
    ("storting", "DEPOSIT"),
    ("valuta creditering", "FX"),
    ("valuta debitering", "FX"),
    ("valuta", "FX"),
    ("koop", "BUY_CASH"),
    ("verkoop", "SELL_CASH"),
]

# Cash-account pseudo-ISINs that are not instruments.
NON_INSTRUMENT_ISINS = {"NLFLATEXACNT"}


def _kind_from_desc(desc: str) -> str:
    d = (desc or "").strip().lower()
    for token, kind in _DESC_MAP:
        if token in d:
            return kind
    return "OTHER"


def parse_account(path: str) -> list[Txn]:
    """
    Columns (by index — again two unnamed amount columns):
      0 Datum  1 Tijd  2 Valutadatum  3 Product  4 ISIN  5 Omschrijving
      6 FX  7 Mutatie(ccy)  8 <amount>  9 Saldo(ccy)  10 <balance>  11 Order Id
    """
    out: list[Txn] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return out
    body = rows[1:] if rows[0] and rows[0][0].strip() == "Datum" else rows

    for idx, r in enumerate(body):
        if not r or not (r[0] or "").strip():
            continue
        r = r + [""] * (12 - len(r))

        desc = (r[5] or "").strip()
        kind = _kind_from_desc(desc)
        amount = parse_amount(r[8])
        if amount is None:
            continue

        dt = parse_dt(r[0], r[1])
        ccy = (r[7] or "").strip() or None

        t = Txn(
            kind=kind,
            trade_dt=dt,
            settle_date=parse_d(r[2]),
            isin=(r[4] or "").strip() or None,
            product=(r[3] or "").strip() or None,
            currency=ccy,
            local_value=amount,
            value_eur=amount if ccy == "EUR" else None,
            fx_rate=parse_amount(r[6]),
            balance=parse_amount(r[10]),
            balance_ccy=(r[9] or "").strip() or None,
            seq=idx,
            order_id=(r[11] or "").strip() or None,
            source="degiro:account",
            note=desc,
        )
        t.external_id = _ext_id(
            "dg-ac", t.order_id, dt.isoformat(), t.isin, desc, ccy, amount, r[10]
        )
        out.append(t)

    out.sort(key=lambda x: x.trade_dt)
    return out


# --------------------------------------------------------------------------
# Instrument registry built from whatever the imports saw
# --------------------------------------------------------------------------

@dataclass
class Instrument:
    isin: str
    name: str
    kind: str
    currency: Optional[str] = None
    exchange: Optional[str] = None
    underlying: Optional[str] = None
    underlying_ticker: Optional[str] = None
    ticker: Optional[str] = None
    sector: Optional[str] = None
    country: Optional[str] = None

    @property
    def domicile(self) -> str:
        """First two ISIN characters are the issuing country."""
        return (self.isin or "  ")[:2]


def build_instruments(txns: list[Txn]) -> dict[str, Instrument]:
    reg: dict[str, Instrument] = {}
    for t in txns:
        if not t.isin or t.isin in NON_INSTRUMENT_ISINS:
            continue
        inst = reg.get(t.isin)
        if inst is None:
            kind = classify(t.product or "", t.isin)
            und = underlying_of(t.product or "") if kind == "DERIVATIVE" else None
            reg[t.isin] = Instrument(
                isin=t.isin,
                name=t.product or t.isin,
                kind=kind,
                currency=t.currency,
                exchange=t.exchange,
                underlying=und,
                underlying_ticker=underlying_ticker_of(und),
            )
        else:
            # keep the longest product name we have seen; DEGIRO truncates some
            if t.product and len(t.product) > len(inst.name):
                inst.name = t.product
            if t.exchange and not inst.exchange:
                inst.exchange = t.exchange
            if t.currency and t.currency != "EUR" and inst.currency in (None, "EUR"):
                inst.currency = t.currency
    return reg
