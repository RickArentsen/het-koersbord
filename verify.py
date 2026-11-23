"""
Reconciliation checks — these must hold or the ledger is lying.

    python verify.py

Reads backend/portfolio.db next to this file. Override with PF_DB, and point
TXCSV / ACCSV at your DEGIRO exports to include the re-import check.
"""
import sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "backend"))
os.environ.setdefault("PF_DB", os.path.join(HERE, "backend", "portfolio.db"))

from pf import store, service
# Apply any schema added since this database was written, so an upgraded
# install verifies instead of tripping over a table that does not exist yet.
store.init_db()
from pf.engine import build_ledger, xirr
from datetime import date

txns=store.load_txns(); trades=[t for t in txns if t.kind in ("BUY","SELL")]
L=build_ledger(trades)
ok=lambda c: "PASS" if c else "**FAIL**"

# 1 realised: price + fx - fees == net, disposal by disposal. DEGIRO rounds each
#   side's total to the cent and quotes the exchange rate to four decimals, so one
#   disposal may be out by two cents plus half a basis point of the money moved.
#   The whole ledger used to be allowed a euro, and a sale booked with the wrong
#   sign (92 cents, see 132) fitted inside it.
lhs=L.realised_price_eur + L.realised_fx_eur - sum(d.fees_eur for d in L.disposals)
_over1=[d for d in L.disposals
        if abs(d.pnl_price_eur+d.pnl_fx_eur-d.fees_eur-d.pnl_eur) > 0.02+5e-5*(abs(d.cost_eur)+abs(d.proceeds_eur))]
_worst1=max((abs(d.pnl_price_eur+d.pnl_fx_eur-d.fees_eur-d.pnl_eur) for d in L.disposals), default=0.0)
print(f"1 realised attribution   {ok(not _over1 and abs(lhs-L.realised_eur)<1.0)}  price {L.realised_price_eur:9.2f} + fx {L.realised_fx_eur:8.2f} - fees {sum(d.fees_eur for d in L.disposals):7.2f} = {lhs:9.2f} vs net {L.realised_eur:9.2f}; "
      f"{len(L.disposals)} disposals, worst one out by {_worst1:.3f}")

# 2 quantities: sum(buys)-sum(sells) per isin == open qty
import collections
net=collections.defaultdict(float)
for t in trades: net[t.isin]+=t.quantity
bad=[(i,round(q,4),round(L.positions[i].qty,4) if i in L.positions else 0.0)
     for i,q in net.items() if abs(q-(L.positions[i].qty if i in L.positions else 0.0))>1e-6]
print(f"2 quantity conservation  {ok(not bad)}  {len(bad)} mismatches")

# 3 cash: sum(total_eur) == -(invested)
tot=sum(t.total_eur for t in trades if t.total_eur is not None)
print(f"3 net cash into market   {ok(abs(-tot-L.invested_eur)<0.01)}  EUR {L.invested_eur:,.2f}")

# 4 no euro is lost between the trades and the lots. What went into securities
#   is either still in an open lot or came back out through a disposal, so
#   open cost = invested + realised (plus what a sell with no matching buy
#   brought in). This used to compare against a figure typed in when the ledger
#   had 189 trades: it failed on the next trade and proved nothing before it.
s=sum(p.cost_eur for p in L.positions.values())
_lots4=sum(l.cost_eur_gross for p in L.positions.values() for l in p.lots)
_rhs4=L.invested_eur+L.realised_eur+L.orphan_proceeds_eur
print(f"4 open cost basis        {ok(abs(s-_lots4)<0.005 and abs(s-_rhs4)<0.005)}  EUR {s:,.2f} = invested {L.invested_eur:,.2f} + realised {L.realised_eur:,.2f}"
      + (f" + {L.orphan_proceeds_eur:,.2f} from {len(L.orphan_sells)} sell(s) with no buy" if L.orphan_sells else "")
      + (f"  (out by {s-_rhs4:+.2f})" if abs(s-_rhs4)>=0.005 else ""))

# 5 xirr solver sanity: +10% over 6m and 3.5y
from datetime import timedelta
d0=date(2026,3,4)
a=xirr([(d0,-10000),(d0+timedelta(days=183),11000)])
b=xirr([(date(2023,3,4),-10000),(date(2026,9,4),11000)])
print(f"5 XIRR solver            {ok(abs(a*100-21.0)<0.6 and abs(b*100-2.8)<0.2)}  6m {a*100:.1f}%  3.5y {b*100:.1f}%")

# 6 idempotency
try:
    from pf.degiro import parse_transactions, parse_account, build_instruments
    allt=(parse_transactions(os.environ.get('TXCSV', os.path.join(HERE,'Transactions.csv')))
          + parse_account(os.environ.get('ACCSV', os.path.join(HERE,'Account.csv'))))
    r=store.commit_import(allt, build_instruments(allt))
    print(f"6 re-import is a no-op   {ok(r['inserted']==0)}  {r}")
except FileNotFoundError:
    print("6 re-import is a no-op   SKIP  set TXCSV/ACCSV to your DEGIRO exports to run this one")

# 7 unrealised attribution on a live payload
d=service.dashboard('SP500')
worst=0
for r_ in d['positions']:
    lhs=r_['unreal_price_eur']+r_['unreal_fx_eur']
    rhs=r_['value_eur']-(r_['cost_eur'])
    worst=max(worst, abs(lhs-rhs)-abs(r_['cost_eur']*0.0)) if False else worst
    worst=max(worst, abs(lhs-rhs))
print(f"7 unreal price+fx vs MV-cost: max gap EUR {worst:.2f}  (gap == fees paid, expected)")
print(f"   total fees on open lots: EUR {sum(l.fees_eur for p in L.positions.values() for l in p.lots):.2f}")

# 8 cash ledger: internal transfers must not be counted as external capital.
#   Since build 45 an iDEAL payment still only reserved counts (it is in the
#   balance already); that part must be exactly the open reservations, so a
#   cash sweep still never reads as money put in.
T=d['totals']
_res8=sum((t.local_value or 0.0) for t in txns if t.kind=="INTERNAL" and not service._sweep(t)
          and (not t.currency or t.currency=="EUR"))
_pend8=T.get('pending_deposits_eur') or 0.0
print(f"8 net capital in         {ok(abs(T['net_deposits_eur']-(T['deposits_eur']+T['withdrawals_eur']+_pend8))<0.01 and abs(_pend8-_res8)<0.01)}"
      f"  deposits {T['deposits_eur']:,.2f} + withdrawals {T['withdrawals_eur']:,.2f} + reserved {_pend8:,.2f} = {T['net_deposits_eur']:,.2f}"
      f" (open reservations {_res8:,.2f})")

# 9 net worth identity
nw = T['value_eur'] + T['cash_eur']
print(f"9 net worth identity     {ok(abs(nw-T['net_worth_eur'])<0.02)}  securities {T['value_eur']:,.2f} + cash {T['cash_eur']:,.2f} = {T['net_worth_eur']:,.2f}")
print(f"   profit = net worth - capital in = EUR {T['profit_eur']:,.2f}")

# 10 open + closed covers every instrument ever traded
ever={t.isin for t in trades if t.isin}
seen={r['isin'] for r in d['positions']} | {r['isin'] for r in d['closed_positions']}
print(f"10 every traded name shown {ok(ever==seen)}  traded {len(ever)}, shown {len(seen)}")

# 11 turbos all mapped to an underlying
insts=store.load_instruments()
dv=[r for r in insts.values() if r['kind']=='DERIVATIVE']
print(f"11 turbos mapped          {ok(all(r['underlying_ticker'] for r in dv))}  {sum(1 for r in dv if r['underlying_ticker'])}/{len(dv)}")

# 12 every leveraged product's contract parses out of its own name
from pf.leveraged import parse, turbo_price, factor_price, implied_underlying
specs=[(r['name'], parse(r['name'])) for r in dv]
unp=[n for n,sp in specs if sp.family in ('TURBO','FACTOR') and not sp.priceable]
print(f"12 contracts parsed       {ok(not unp)}  {sum(1 for _,sp in specs if sp.priceable)}/{len(specs)} priceable"
      f" ({sum(1 for _,sp in specs if sp.family=='WARRANT')} warrants have no model by design)")

# 13 turbo formula inverts exactly
sp=parse("SG NVIDIA BEST TURBO LONG SL 103.448 FN 103.448 R 5")
rt=implied_underlying(sp, turbo_price(sp, 187.5))
print(f"13 turbo price/invert     {ok(abs(rt-187.5)<1e-9)}  round-trips to {rt}")

# 14 factor certificates decay on a flat share (they must not come back to par)
sp=parse("SG X FACTOR (MULTI) LONG LEV 5")
v=factor_price(sp, 100.0, "2026-01-01", {"2026-01-01":100.0,"2026-01-02":110.0,"2026-01-03":100.0})
print(f"14 volatility decay       {ok(v is not None and 81 < v < 83)}  share 100->110->100 leaves a 5x cert at {v:.2f}, not 100")

# 15a the leverage plausibility guard rejects an absurd calibration
from pf.leveraged import calibrate_turbo
_sp=parse("SG SNDK CLASSIC TURBO LONG SL 360.36 FN 325.683 R 100")
_m=calibrate_turbo(_sp, [('2026-07-13',12.418)], {'2026-07-13':325.81,'2026-09-08':348.0})
print(f"15a absurd model rejected {ok(not _m.ok and 'leverage' in _m.reason)}  {_m.reason[:64]}")

# 16 the equity curve has no holes: a name we cannot price is a known constant,
#    not missing data, and every traded instrument gets a ticker
_C=d['curve']
if _C.get('no_history'):
    print("16 curve continuity       SKIP  no price history cached yet (press Refresh online)")
else:
    print(f"16 curve continuity       {ok(_C.get('gaps')==0)}  gaps {_C.get('gaps')}, "
          f"{_C.get('unpriced_days')} unpriced days, "
          f"{len(_C.get('carried_at_last_trade') or [])} names carried at last trade")

# 17 cash comes from DEGIRO's own Saldo, newest row per currency. Several rows
#    share a timestamp and the export is newest-first, so ties break on row index.
#    A cash-sweep row is never that row: its balance is not one the account held (136).
_bal=[t for t in txns if t.balance is not None and t.balance_ccy and not service._sweep(t)]
_cash={}
for _c in {t.balance_ccy for t in _bal}:
    _rows=[t for t in _bal if t.balance_ccy==_c]
    _cash[_c]=min(_rows,key=lambda t:(-t.trade_dt.timestamp(), t.seq if t.seq is not None else 0)).balance
_missing=sum(1 for t in txns if t.source=="degiro:account" and t.balance is None)
print(f"17 cash balance ranking   {ok(_missing==0 and _cash==d['totals']['cash_by_ccy'])}  {_cash}, "
      f"{_missing} account rows missing a balance")

# 18 the curve is one unbroken run: every day carries a value
_S=d['curve'].get('series') or []
_holes=[x['d'] for x in _S if x.get('v') is None]
print(f"18 curve is unbroken      {ok(_S and not _holes)}  {len(_S)} days, {len(_holes)} without a value")

# 19 hand-entered transactions land in the same ledger and refuse bad input
import tempfile, shutil
_tmp=os.path.join(tempfile.gettempdir(),"pf_manualcheck.db")
shutil.copy(os.environ["PF_DB"], _tmp)
_orig=os.environ["PF_DB"]; os.environ["PF_DB"]=_tmp
import importlib
from pf import store as _st
_st.DB_PATH=_tmp
from pf import manual as _mn
_ok=[]
try:
    _isin=next(iter(L.positions))
    _held=_mn.held_on(_isin, date.today())
    _mn.add_transaction(kind="BUY", isin=_isin, trade_date=date.today().isoformat(),
                        quantity=1, price=100, currency="EUR", fee_eur=1)
    _ok.append(_mn.held_on(_isin, date.today()) == _held + 1)
    try:
        _mn.add_transaction(kind="SELL", isin=_isin, trade_date=date.today().isoformat(),
                            quantity=1e9, price=100, currency="EUR")
        _ok.append(False)
    except _mn.ManualError: _ok.append(True)
    try:
        _mn.add_transaction(kind="BUY", isin=_isin, trade_date=date.today().isoformat(),
                            quantity=1, price=100, currency="EUR", fee_eur=1)
        _ok.append(False)
    except _mn.ManualError: _ok.append(True)
    _rows=_mn.list_manual(); _mn.delete_manual(_rows[0]["id"])
    _ok.append(_mn.held_on(_isin, date.today()) == _held)
finally:
    os.environ["PF_DB"]=_orig; _st.DB_PATH=_orig
    try: os.remove(_tmp)
    except OSError: pass
print(f"19 manual entry           {ok(all(_ok))}  add / over-sell refused / duplicate refused / delete = {_ok}")

# 20 coverage is measured in euros, not in position count. One tiny unpriced
#    holding must not withhold the return figures for the whole portfolio.
_C2=d['curve']
if _C2.get('no_history') or not _C2.get('series'):
    print("20 coverage is value-weighted SKIP  no price history cached yet")
else:
    _q=[x.get('q') for x in _C2['series'] if x.get('q') is not None]
    _consistent = _C2.get('mean_coverage_pct') is not None and (
        (_C2['mean_coverage_pct'] < 50) == bool(_C2.get('mostly_carried')))
    print(f"20 coverage is value-weighted {ok(_consistent and _q)}  mean {_C2.get('mean_coverage_pct')}% of value priced, "
          f"withheld={_C2.get('mostly_carried')}")

# 15 the model reprices his own turbo trades (needs price history; skipped without)
lv=[r for r in d['positions'] if r.get('lev')]
mod=[r for r in lv if (r['lev'].get('model') or {}).get('ok')]
if mod:
    worst=max(r['lev']['model']['error_pct'] for r in mod)
    print(f"15 model reprices trades  {ok(worst<=12)}  {len(mod)}/{len(lv)} modelled, worst error {worst}%")
else:
    print(f"15 model reprices trades  SKIP  no underlying price history cached yet (press Refresh online)")

# ==========================================================================
# risk, attribution and discipline (build 7)
# ==========================================================================

from pf import risk as _rk, behaviour as _bh

_C = d.get("contribution") or {}
_K = d.get("costs") or {}
_B = d.get("discipline") or {}
_M = d.get("market_risk") or {}

# 21 contribution is an accounting identity, not a ranking. Every euro of
#    profit is either attributed to an instrument or named in the residual.
#    The residual is NOT expected to be zero — interest and the account fees
#    that never entered a cost basis belong to no instrument. What must be
#    ~zero is the part of it those do not explain.
_sum = round(sum(i["total_eur"] for i in _C.get("items", [])), 2)
print(f"21 contribution reconciles {ok(_C.get('reconciles') and abs(_sum-_C.get('named_eur',0))<0.02)}  "
      f"{len(_C.get('items',[]))} names = EUR {_C.get('named_eur')}; residual "
      f"{_C.get('residual_eur')} vs {_C.get('residual_expected_eur')} expected "
      f"-> {_C.get('unexplained_eur')} unexplained on {_C.get('profit_eur')} profit "
      f"(up to {_C.get('tolerance_eur')} allowed)")

# 22 round trips must not lose or double count a disposal: completed trips
#    plus P&L taken off still-open positions equals the ledger's realised.
_tr = (_B.get("total_realised_eur") or 0) + (_B.get("partial_sales_eur") or 0)
print(f"22 round trips complete    {ok(abs(_tr-L.realised_eur)<0.02)}  "
      f"{_B.get('n')} trips {_B.get('total_realised_eur')} + partial sales "
      f"{_B.get('partial_sales_eur')} = realised {round(L.realised_eur,2)}")

# 23 expectancy * trips == total P&L of those trips. Exact, by construction —
#    if this drifts, the hit rate and payoff are describing different samples.
_e = (_B.get("expectancy_eur") or 0) * (_B.get("n") or 0)
# The expectancy is rounded to the cent, so n trips can be n half-cents off
# (40 trips: EUR 20.13 x 40 = 805.20 against 805.38 on the ledger).
_tol23 = 0.005 * (_B.get("n") or 0) + 0.01
print(f"23 expectancy identity     {ok(abs(_e-(_B.get('total_realised_eur') or 0))<=_tol23)}  "
      f"EUR {_B.get('expectancy_eur')} x {_B.get('n')} = {round(_e,2)}")

# 24 fees are not double counted: DEGIRO writes trade commission to the cash
#    ledger too, so the account figure must CONTAIN the Transactions.csv one.
print(f"24 no double counted fees  {ok(_K.get('commission_eur',0) <= _K.get('account_fees_eur',0)+0.01)}  "
      f"commission {_K.get('commission_eur')} inside account fees {_K.get('account_fees_eur')}; "
      f"drag {_K.get('drag_bps')} bps/yr on {_K.get('avg_capital_eur')}")

# 25 the regression must refuse to answer rather than print noise. With no
#    price history there are no usable days, so beta has to be withheld.
_bm = _M.get("benchmarks") or []
_withheld = (not _M.get("enough")) and all(b.get("beta") is None for b in _bm)
_answered = _M.get("enough") and any(b.get("beta") is not None for b in _bm)
print(f"25 beta withheld or sound  {ok(_withheld or _answered)}  "
      + (f"{_M.get('n_days')} usable days — " + ", ".join(
          f"{b['key']} beta {b.get('beta')} R2 {b.get('r2')}" for b in _bm) if _answered
         else f"withheld: {_M.get('why')}"))

# 26 R2 is a proportion and the information ratio is the active return divided
#    by the tracking error — not an independently invented number.
if _answered:
    _bad = [b for b in _bm if b.get("r2") is not None and not (0 <= b["r2"] <= 1)]
    _ir = [b for b in _bm if b.get("information_ratio") is not None
           and b.get("tracking_error_pct") and
           abs(b["information_ratio"] - b["active_return_pct"]/b["tracking_error_pct"]) > 0.02]
    print(f"26 IR = active / TE        {ok(not _bad and not _ir)}  "
          f"{len(_bm)} benchmarks, {len(_bad)} bad R2, {len(_ir)} inconsistent IR")
else:
    print("26 IR = active / TE        SKIP  no price history cached yet")

# 27 a deposit is not a return. Feed the flow-neutraliser a curve with a
#    manufactured 50% deposit day and confirm it is not reported as a gain.
_fake = [{"d": f"2026-01-{i+1:02d}", "v": 10000.0, "s": 10000.0, "sf": 0.0,
          "q": 1.0, "f": 0.0} for i in range(5)]
_fake[3].update({"v": 15000.0, "s": 15000.0, "sf": 5000.0, "f": 5000.0})
_fake[4].update({"v": 15000.0, "s": 15000.0})
_r = _rk.curve_returns(_fake)
print(f"27 deposits are not gains  {ok(all(abs(v) < 1e-9 for v in _r.values()))}  "
      f"a 50% deposit day yields {[round(v,6) for v in _r.values()]}")

# ==========================================================================
# live prices (build 8)
# ==========================================================================

from pf import stream as _sm

# 28 the two legs of the day change ARE the change in euro value. Not
#    approximately — this is the identity the whole split rests on.
import random as _rnd
_rnd.seed(11); _worst = 0.0
for _ in range(500):
    _q  = _rnd.uniform(1, 200); _pv = _rnd.uniform(5, 900)
    _px = _pv * _rnd.uniform(.8, 1.2)
    _fp = _rnd.uniform(.8, 1.5); _fn = _fp * _rnd.uniform(.95, 1.05)
    _b = {"positions": [{"isin": "A", "ticker": "T", "qty": _q, "currency": "USD",
                         "price": _pv, "prev_close": _pv, "fx": _fn, "fx_prev": _fp,
                         "cost_eur": 0}],
          "fx_symbol": {"USD": "EURUSD=X"}, "cash_eur": 0, "cost_eur": 0}
    _t = _sm.fold(_b, {"T": {"price": _px, "prev_close": _pv},
                       "EURUSD=X": {"price": _fn, "prev_close": _fp}})["totals"]
    _worst = max(_worst, abs(_t["day_eur"] - (_q*_px/_fn - _q*_pv/_fp)))
print(f"28 price + FX = the change {ok(_worst < 0.02)}  500 random positions, worst gap EUR {_worst:.4f}")

# 29 the day percentage is against YESTERDAY's close. Against today's value it
#    would understate every gain and overstate every loss.
_t = _sm.fold({"positions": [{"isin": "A", "ticker": "T", "qty": 100, "currency": "EUR",
                              "price": 10, "prev_close": 10, "fx": 1, "fx_prev": 1,
                              "cost_eur": 800}],
               "fx_symbol": {}, "cash_eur": 0, "cost_eur": 800},
              {"T": {"price": 11, "prev_close": 10}})["totals"]
print(f"29 day % uses yesterday    {ok(abs(_t['day_pct']-10.0)<1e-6)}  "
      f"EUR 1,000 -> 1,100 reads {_t['day_pct']}% (not {100*100/1100:.1f}%)")

# 30 a tick that carries no previous close must not erase the one we have,
#    or every such tick would read as a 100% move.
_s = _sm.Streamer(); _s._put("T", 100.0, 95.0); _s._put("T", 101.0, None)
_v = _s.version; _s._put("T", 101.0, None)
print(f"30 live cache is sane      {ok(_s.snapshot()['quotes']['T']['prev_close']==95.0 and _s.version==_v)}  "
      f"prev close kept at 95.0; an unchanged price did not bump the version")

# 31 THE FREEZE GUARD. The SSE handler must never build the base itself.
#    live_base() replays the ledger and calls the quote API; run on the web
#    server's event loop it froze every other request — the page, the
#    stylesheet, the dashboard — for as long as the network took, once per
#    connection and again every five minutes. This asserts it stays off the
#    loop, because the symptom (an occasional freeze) is one nobody would
#    connect back to this line of code.
import inspect as _insp
from pf import api as _api
_src = _insp.getsource(_api.live)
_direct = "service.live_base(" in _src
_pooled = "run_in_threadpool" in _src
print(f"31 base is off the loop    {ok(not _direct and _pooled)}  "
      f"SSE handler calls live_base directly: {_direct}; uses a threadpool: {_pooled}")

# 32 the base is cached and shared, so a browser reconnecting in a loop costs
#    nothing instead of triggering one full rebuild per attempt.
import types as _ty
_s = _sm.Streamer(); _n = {"b": 0}
def _fake(self):
    _n["b"] += 1
    with self._base_lock:
        self._base = {"positions": [], "fx_symbol": {}, "symbols": [],
                      "cash_eur": 0, "cost_eur": 0}
        self._base_at = _t.time()
    return self._base
import time as _t
_s.refresh_base = _ty.MethodType(_fake, _s)
_before = _s.base()
_s.refresh_base(); [_s.base() for _ in range(50)]
print(f"32 base is cached          {ok(_before is None and _n['b'] == 1)}  "
      f"50 readers caused {_n['b']} rebuild; an unbuilt base reports None, not an empty portfolio")

# ==========================================================================
# news and the week ahead (build 10)
# ==========================================================================

from pf import news as _nw, brief as _bf
from pf import calendar as _cl

# 33 the macro feed is American-dated, and its XML clock is UTC. Reading 09-16-2026 as 16 September is
#    right; reading it as a 9th-of-the-16th-month is a crash, and reading it
#    as 9 April would put the Fed meeting in the wrong week entirely.
_FIX = ('<weeklyevents><event><title>Federal Funds Rate</title><country>USD</country>'
        '<date><![CDATA[09-16-2026]]></date><time><![CDATA[6:00pm]]></time>'
        '<impact><![CDATA[High]]></impact><forecast><![CDATA[4.00%]]></forecast>'
        '<previous><![CDATA[3.75%]]></previous><url /></event>'
        '<event><title>Bank Holiday</title><country>JPY</country>'
        '<date><![CDATA[09-15-2026]]></date><time><![CDATA[All Day]]></time>'
        '<impact><![CDATA[Holiday]]></impact><forecast /><previous /><url /></event>'
        '</weeklyevents>')
_ev = _cl.parse_macro_xml(_FIX)
_fed = next(e for e in _ev if e["title"] == "Federal Funds Rate")
print(f"33 macro feed parsed       {ok(_fed['date']=='2026-09-16' and _fed['time']=='20:00' and _fed['rank']==3)}  "
      f"MM-DD-YYYY -> {_fed['date']}, 6:00pm UTC -> {_fed['time']} Amsterdam, High -> rank {_fed['rank']}")

# 34 two reports of one event must not take both headline slots.
_now = _t.time()
_items = [
    _nw._item("Nvidia beats on data centre revenue", "n1", "Reuters", _now-600, "", ["NVDA"]),
    _nw._item("Nvidia tops estimates as data centre sales surge", "n2", "CNBC", _now-500, "", ["NVDA"]),
    _nw._item("Nvidia faces antitrust probe in China", "n3", "Bloomberg", _now-700, "", ["NVDA"]),
]
_r = _nw.rank(_items, {"NVDA": 12.0})
print(f"34 one story, one row      {ok(len(_r)==2)}  "
      f"3 headlines in, {len(_r)} out — the two earnings wires merged, the probe survived")

# 35 a 12% position outranks a 0.4% one. Without this the panel is a feed,
#    not a briefing.
_big = _nw.score(_nw._item("Big holding news", "a", "Reuters", _now-600, "", ["NVDA"]), {"NVDA":12.0,"TX":0.4})
_small = _nw.score(_nw._item("Small holding news", "b", "Reuters", _now-600, "", ["TX"]), {"NVDA":12.0,"TX":0.4})
print(f"35 weight drives ranking   {ok(_big['score'] > _small['score'])}  "
      f"12% position scores {_big['score']} vs {_small['score']} for a 0.4% one")

# 36 THE DEDUP RULE. Your biggest positions are usually also the day's biggest
#    movers; showing them twice wastes half the panel.
_pos = [{"isin":str(i),"ticker":t,"value_eur":v,"weight":w,"day_pct":p,"day_eur":p*v/100}
        for i,(t,v,w,p) in enumerate([
            ("MSFT",3000,30,0.4), ("AMZN",2000,20,9.1), ("AAPL",1500,15,-7.2),
            ("NVDA",1200,12,0.1), ("ADBE",1000,10,-0.2), ("VERI",300,3,6.4),
            ("BE",250,2.5,4.1), ("TX",200,2,-5.5), ("MP",150,1.5,-3.3)])]
_wc = _bf.what_changed(_pos)
_w = [r["ticker"] for r in _wc["weights"]]
_m = [r["ticker"] for r in _wc["movers"]]
_ups = sum(1 for r in _wc["movers"] if r["day_pct"] > 0)
print(f"36 movers never repeat     {ok(set(_w).isdisjoint(_m) and len(_m)==4 and _ups==2)}  "
      f"weights {_w}; movers {_m} — AMZN and AAPL moved most but are already listed")

# 37 with no key and no network nothing raises and nothing pretends. The check
#    takes both away itself. It used to assume them: it called the real feed and
#    passed only as long as no Finnhub key was saved. Two cases: no key at all,
#    and a key with the network down.
def _down37(*a, **k): raise OSError("network is down (simulated)")
class _Down37:
    def __init__(self, *a, **k): _down37()
try:
    import yfinance as _yf37
except Exception:
    _yf37 = None
_keep37 = (_nw.api_key, _nw.provider_name, _nw._get_json, getattr(_yf37, "Ticker", None))
try:
    if _yf37: _yf37.Ticker = _Down37
    _nw._get_json = _down37
    _nw.api_key = lambda: None
    _p = _nw.fetch(["NVDA", "MSFT"], {"NVDA": 12.0})
    _nw.api_key = lambda: "a-key"
    _nw.provider_name = lambda: "finnhub"
    _p2 = _nw.fetch(["NVDA", "MSFT"], {"NVDA": 12.0})
finally:
    _nw.api_key, _nw.provider_name, _nw._get_json = _keep37[:3]
    if _yf37: _yf37.Ticker = _keep37[3]
_b = _bf.BRIEF.get()
print(f"37 degrades honestly       {ok(_p['error'] and not _p['items'] and _p2['error'] and not _p2['items'] and isinstance(_b['news']['top'], list))}  "
      f"no key: {_p['provider']} says {str(_p['error'])[:42]!r}; a key but no network: {_p2['provider']} says {str(_p2['error'])[:38]!r}")

# ==========================================================================
# the market board (build 11)
# ==========================================================================

from pf import market as _mk
from datetime import datetime as _dt, timedelta as _td
from zoneinfo import ZoneInfo as _ZI

# 38 a yield is quoted in percent already, so its move is basis points. A 10y
#    going 4.00 -> 4.10 is +10bp; rendering "+2.5%" is true and useless.
_rate = {"2026-09-16": 4.00, "2026-09-17": 4.10}
_rs = _mk.row_stats(_rate, today=date(2026, 9, 17))
print(f"38 rates move in basis points {ok(abs(_rs['d_abs'] - 0.10) < 1e-9)}  "
      f"4.00 -> 4.10 is {_rs['d_abs']*100:.0f}bp (the page renders d_abs for rate rows)")

# 39 row stats must not shadow the row's own fields. "n" is the instrument
#    name in the board; row_stats returning "n" renamed every row to a count.
_h = {(date(2026, 9, 17) - _td(days=i)).isoformat(): 100 + i for i in range(60)}
_st = _mk.row_stats(_h, today=date(2026, 9, 17))
print(f"39 stats do not shadow names  {ok('n' not in _st and _st.get('obs') == 60)}  "
      f"observation count is obs={_st.get('obs')}, leaving n for the instrument name")

# 40 a live price replaces the stored close, and 1D is measured from the last stored close BEFORE the price's own
#    session (build 55). The history holds the session in progress since build 33, so measuring against the newest
#    stored close put every streamed row near 0.0% all day, and the whole board at 0.0% at a weekend. A quote dated
#    before the newest stored day is ignored; so is one the feed stopped updating half an hour ago.
_h40 = {d: v for d, v in _h.items() if d < "2026-09-17"}                    # stored through yesterday (101 on the 16th)
_lv = _mk.row_stats(_h40, last=103.0, today=date(2026, 9, 17))             # a tick today, no bar for it yet
_lv2 = _mk.row_stats(_h, last=102.0, today=date(2026, 9, 17))              # today's bar so far is stored (100)
_lv3 = _mk.row_stats(_h, last=99.0, today=date(2026, 9, 17), last_d="2026-09-16")   # a quote older than the history
_sat = _mk.row_stats(_h, today=date(2026, 9, 19))                          # a weekend: no quote, the last session's move
_q40 = (_mk.quote_day({"price": 1.0, "ts": 1_000_000.0, "seen": 1_000_000.0}, "America/New_York", now=1_000_000.0 + 3600),
        _mk.quote_day({"price": 1.0, "asof": "2026-09-17", "seen": 5.0}, "America/New_York", now=10.0),
        _mk.quote_day({"price": 1.0, "ts": 1_789_675_200.0}, "America/New_York", now=1_789_675_300.0))
print(f"40 a streamed price folds in  {ok(abs(_lv['d_abs'] - 2.0) < 1e-9 and abs(_lv2['d_abs'] - 1.0) < 1e-9 and _lv3['last'] == 100 and _lv3['d_abs'] == -1 and _sat['d_abs'] == -1 and _q40 == (None, '2026-09-17', '2026-09-17'))}  "
      f"103 against yesterday's 101 ({_lv['d_abs']:+.0f}), 102 against yesterday and not today's bar so far ({_lv2['d_abs']:+.0f}); "
      f"a stale quote ignored; a weekend shows the last session ({_sat['d_abs']:+.0f}); a quote unseen for an hour dropped")

# 41 sessions: open when open, shut at the weekend, and pointing at Monday.
_ams = _ZI("Europe/Amsterdam")
_thu = {s["k"]: s for s in _mk.sessions(_dt(2026, 9, 17, 14, 0, tzinfo=_ams))}
_sat = {s["k"]: s for s in _mk.sessions(_dt(2026, 9, 19, 12, 0, tzinfo=_ams))}
print(f"41 session clock is right     "
      f"{ok(_thu['AMS']['open'] and not _thu['TYO']['open'] and not any(s['open'] for s in _sat.values()) and _sat['AMS']['in_min'] > 40*60)}  "
      f"Thu 14:00 Amsterdam open / Tokyo shut; Saturday all shut, next bell in {_sat['AMS']['in_txt']}")

# 42 the risk read must be able to say "mixed". A score that always commits is
#    a score that is guessing.
_mix = _mk.risk_read({"SPX": {"d_pct": 1.2}, "VIX": {"d_pct": 2.0}, "GOLD": {"d_pct": 1.1}})
_on = _mk.risk_read({"SPX": {"d_pct": 1.2}, "SX5E": {"d_pct": .9}, "AEX": {"d_pct": 1.0},
                     "VIX": {"d_pct": -6.0}, "USHY": {"d_pct": -1.5}})
print(f"42 risk read can say mixed    {ok(_mix['verdict'] == 'mixed' and _on['verdict'] == 'risk-on')}  "
      f"equities up with vol and gold up = {_mix['verdict']!r}; a clean day = {_on['verdict']!r}")

# 43 every row Rick asked for is on the board, and derived rows point at legs
#    that actually exist.
_keys = [r["k"] for r in _mk.BOARD]
_want = ["AEX", "AMX", "SX5E", "DAX", "SPX", "NDX", "N225", "HSI",
         "US2Y", "US10Y", "US10_2", "EA10Y", "DFR", "FF",
         "EURUSD", "EURGBP", "DXY", "BRENT", "TTF", "GOLD", "COPPER",
         "VIX", "V2TX", "USHY"]
_calc_ok = all(all(leg in _keys for leg in r["s"])
               for r in _mk.BOARD if r["src"] == "calc")
print(f"43 board covers the request   "
      f"{ok(all(k in _keys for k in _want) and len(_keys) == len(set(_keys)) and _calc_ok)}  "
      f"{len(_keys)} rows, no duplicate keys, derived rows reference real legs")

# ==========================================================================
# position detail and the per-ticker page (build 12)
# ==========================================================================

# 44 one holding, reachable by ISIN or by ticker, with its own trades attached.
#    The chart is only worth looking at because your entries are on it.
_pd = service.position_detail(next(
    (r.get("ticker") for r in service.dashboard("SP500", light=True)["positions"]
     if r.get("ticker")), "MSFT"))
_by_isin = service.position_detail(_pd.get("isin", "")) if _pd.get("isin") else {}
print(f"44 position detail loads   "
      f"{ok(not _pd.get('error') and _pd.get('trades') and _by_isin.get('ticker') == _pd.get('ticker'))}  "
      f"{_pd.get('ticker')}: {len(_pd.get('trades') or [])} trades, "
      f"{len(_pd.get('history') or [])} sessions, same answer by ISIN")

# 45 an unknown symbol is refused with a reason, not a stack trace or an
#    empty page that looks like a bug.
_bad = service.position_detail("NOTATICKER")
print(f"45 unknown symbol refused  {ok(bool(_bad.get('error')) and not _bad.get('position'))}  "
      f"{_bad.get('error')}")

# 46 the Sources panel must read the stream's state NOW, not the state it had
#    when the board was built — which is the bug that made it permanently say
#    "unavailable" while the live dot said otherwise.
import inspect as _i2
from pf import brief as _bf2
_src = _i2.getsource(_bf2.Brief.market)
print(f"46 stream state read live  {ok('STREAM.snapshot()' in _src and 'sources' in _src)}  "
      f"brief.market() re-reads the streamer on every request")

# 47 the correlation grid explains itself when it cannot be built, instead of
#    returning empty and letting the page hide the panel.
from pf import market as _mk2
_thin = _mk2.correlations({"SPX": {"2026-09-01": 1.0}}, None)
print(f"47 empty correlation says why {ok(bool(_thin.get('why')) and _thin['keys'] == [])}  "
      f"{str(_thin.get('why'))[:66]}")

# ==========================================================================
# geopolitics, sentiment and the map (build 13)
# ==========================================================================
from pf import geo as _geo, sentiment as _sent
from datetime import date as _date, timedelta as _td

_pos = service.dashboard("SP500", light=True)["positions"]
_rx  = _geo.risk_exposure(_pos, [])
_book = sum(p.get("value_eur") or 0 for p in _pos if not p.get("excluded"))

# 48 a position is counted ONCE per risk. Without that rule a US-listed
#    technology name matches by ticker, by sector and by country, and the
#    "exposure" to a single risk comes out larger than the portfolio.
_over = [r["k"] for r in _rx if r["value_eur"] > _book + 0.01]
_split = [r["k"] for r in _rx
          if abs(r["named_eur"] + r["sector_eur"] + r["country_eur"] - r["value_eur"]) > 0.02]
print(f"48 exposure counted once  {ok(not _over and not _split)}  "
      f"no risk exceeds the {_book:,.0f} book; named+sector+country reconciles for all "
      f"{len(_rx)} risks")

# 49 involvement is not exposure. The register names the United States under
#    European energy security because it ships the LNG — and reading that as
#    exposure put 93% of this book against a risk it barely touches.
_en = next(r for r in _rx if r["k"] == "ENERGY")
_us = [p for p in _pos if p.get("country") == "United States" and not p.get("excluded")]
_leak = [p.get("ticker") for p in _us
         if _en["hits"].get((p.get("ticker") or "").upper()) == "country"]
print(f"49 involvement != exposure {ok('US' in _en['countries'] and 'US' not in _en['exposed'] and not _leak)}  "
      f"European energy security: {_en['pct']}% of the book, no US listing reached by country")

# 50 chokepoint pins land on the map, and near the water they describe.
import json as _json
_w = _json.loads(open(os.path.join(HERE, "backend", "static", "world.json"), encoding="utf-8").read())
_vb = [float(x) for x in _w["viewBox"].split()]
_cen = _w["centroids"]
_xy = {c["k"]: _geo.chokepoint_xy(c, _cen) for c in _geo.CHOKEPOINTS}
_inside = all(p and _vb[0] <= p[0] <= _vb[0]+_vb[2] and _vb[1] <= p[1] <= _vb[1]+_vb[3]
              for p in _xy.values())
_hz = _xy["HORMUZ"]
_near = (abs(_hz[0] - _cen["IR"][0]) < 120 and abs(_hz[0] - _cen["AE"][0]) < 120
         and abs(_xy["TAIWAN"][0] - _cen["TW"][0]) < 120)
print(f"50 chokepoints on the map {ok(_inside and _near)}  "
      f"{len(_xy)} pins inside the {int(_vb[2])}x{int(_vb[3])} frame; "
      f"Hormuz at {_hz} between Iran and the UAE")

# 51 every country the dashboard can report has a shape to shade. A missing
#    ISO is a country that silently disappears from the map.
_have = set(_w["paths"])
_off = [i for i, c in _cen.items()
        if not (_vb[0] <= c[0] <= _vb[0]+_vb[2] and _vb[1] <= c[1] <= _vb[1]+_vb[3])]
# Cayman, Jersey, Bermuda, Singapore: real domiciles, too small to be drawn at
# this scale. The money must not quietly vanish from the total because its
# country has no shape — every country is flagged with whether it can be shaded.
_ce = _geo.country_exposure(_pos, on_map=_have)
_unmapped = sorted(a["iso"] for a in _ce.values() if not a["on_map"])
_flagged = all(("on_map" in a) for a in _ce.values())
_sum = sum(a["value_eur"] for a in _ce.values())
_off_eur = sum(a["value_eur"] for a in _ce.values() if not a["on_map"])
print(f"51 map says what it omits {ok(_flagged and not _off and _sum <= _book + 0.01)}  "
      f"{len(_have)} shapes; {len(_ce)} countries held, "
      f"{len(_unmapped)} too small to draw ({_off_eur:,.0f} EUR, flagged not dropped)"
      + (f": {_unmapped}" if _unmapped else ""))

# 52 FedWatch arithmetic. A contract priced at the current rate is a hold; a
#    contract priced at a full 25bp cut is a cut. The month is part before the
#    meeting and part after, and getting that split wrong is the whole risk.
#    Since build 22 the arithmetic is pf.ratepath.fed_chain, which walks
#    every meeting on the strip instead of the next one only.
from pf import ratepath as _rp
_m = _sent.next_meeting(_sent.FOMC)
if _m:
    _t52, _cur = _date(2026, 10, 1), 3.83
    _m52 = [_date(2026, 10, 28), _date(2026, 12, 9)]
    _flat52 = _rp.fed_chain({(2026, 10): _cur, (2026, 11): _cur, (2026, 12): _cur, (2027, 1): _cur},
                            _m52, _cur, _t52)["meetings"]
    # A cut on the 28th: October averages 28 days at the old rate and 3 at the
    # new, November is all new. The chain must find the whole 25bp in October.
    _cut52 = _rp.fed_chain({(2026, 10): _cur * 28 / 31 + (_cur - .25) * 3 / 31, (2026, 11): _cur - .25,
                            (2026, 12): _cur - .25, (2027, 1): _cur - .25}, _m52, _cur, _t52)["meetings"]
    # Two meeting months in a row: there is no clean month to anchor on, so
    # the first is split inside its own contract.
    _own52 = _rp.fed_chain({(2026, 10): _cur * 14 / 31 + (_cur - .25) * 17 / 31, (2026, 11): _cur - .25},
                           [_date(2026, 10, 14), _date(2026, 11, 18)], _cur, _t52)["meetings"]
    _ok52 = (_flat52[0]["hold"] > 99 and _flat52[1]["hold"] > 99
             and _cut52[0]["cut"] > 99 and abs(_cut52[0]["change_bp"] + 25) < 0.6 and _cut52[1]["hold"] > 99
             and _own52[0]["how"] == "own" and abs(_own52[0]["change_bp"] + 25) < 0.6 and _own52[1]["hold"] > 99
             and _rp.odds(0.09)["hike"] == 36.0 and _rp.odds(-0.375)["second"] == 50.0)
    print(f"52 FedWatch arithmetic    {ok(_ok52)}  "
          f"flat strip = {_flat52[0]['hold']:.0f}% hold at both meetings; a priced cut = "
          f"{_cut52[0]['cut']:.0f}% cut, {_cut52[0]['change_bp']:+.0f}bp, then {_cut52[1]['hold']:.0f}% hold; "
          f"back-to-back meetings split inside the month ({_own52[0]['how']})")
else:
    print(f"52 FedWatch arithmetic    **FAIL**  the FOMC list has run out "
          f"(last updated {_sent.MEETINGS_AS_OF})")

# 53 the house fear & greed index# 53 the house fear & greed index: bounded, directional, and honest about how
#    many components it actually had.
_up = {d.isoformat(): 100 + i*0.3 for i, d in
       enumerate(_date(2025, 1, 1) + _td(days=n) for n in range(300))}
_dn = {d: 200 - (v - 100) for d, v in _up.items()}
_flat = {d: 50.0 for d in _up}
_a = _sent.house_fear_greed({"SPX": _up, "VIX": _flat, "GOLD": _flat}, {})
_b = _sent.house_fear_greed({"SPX": _dn, "VIX": _flat, "GOLD": _flat}, {})
_bounded = all(0 <= c["score"] <= 100 for c in _a["components"] + _b["components"])
print(f"53 house fear & greed     {ok(_bounded and _a['score'] > _b['score'] and _a['n'] >= 3)}  "
      f"a rising market scores {_a['score']:.0f}, a falling one {_b['score']:.0f}, "
      f"from {_a['n']} components, all inside 0-100")

# 54 every endpoint the GEO page calls is a route this app serves. A page that
#    is newer than the server it talks to fetches 404s and renders blank.
import re as _re
sys.path.insert(0, os.path.join(HERE, "backend"))
import portfolio_app as _app
from pf.api import router as _apirouter
# The API router already carries its own /api/pf prefix, and newer FastAPI
# keeps included routers lazy, so app.routes alone does not list them.
_routes = ({r.path for r in _app.app.routes if getattr(r, "path", None)}
           | {r.path for r in _apirouter.routes if getattr(r, "path", None)})
_html = open(os.path.join(HERE, "backend", "static", "geo.html"), encoding="utf-8").read()
# Every URL the page asks for, however it asks — fetch() directly, or through
# the page's own jget() helper.
_calls = {u.split("?")[0] for u in
          _re.findall(r'(?:fetch|jget)\(\s*"(/[^"]+)"', _html)}
_unknown = sorted(u for u in _calls
                  if u.startswith("/api/") and u not in _routes)
print(f"54 GEO calls real routes  {ok(not _unknown)}  "
      f"{len(_calls)} endpoints, all registered" + (f" — {_unknown}" if _unknown else ""))

# 55 the page is wired into the shell: a module marked live must have a file
#    behind it, or the command line sends you to a placeholder.
_js = open(os.path.join(HERE, "backend", "static", "term.js"), encoding="utf-8").read()
_live = set(_re.findall(r'code: "(\w+)",[^}]*?state: "live"', _js))
_files = {c: os.path.exists(os.path.join(HERE, "backend", "static", f))
          for c, f in _app.BUILT_MODULES.items()}
_gap = sorted(c for c in _live if c not in ("PORT",) and not _files.get(c))
print(f"55 live modules have pages {ok(not _gap)}  "
      f"{sorted(_live)} live; {sorted(_app.BUILT_MODULES)} served from disk"
      + (f" — missing {_gap}" if _gap else ""))

# 56 the build number the pages demand must be the build the server declares.
#    These two live in different files — pf/__init__.py and static/term.js —
#    and when they drift every screen shows the "restart the backend" bar
#    forever, because restarting cannot fix a number that was never bumped.
from pf import BUILD as _BUILD
_req = _re.search(r"REQUIRED_BUILD\s*=\s*(\d+)", _js)
print(f"56 build numbers agree    {ok(_req and int(_req.group(1)) == _BUILD)}  "
      f"pages require {_req.group(1) if _req else '?'}, server declares {_BUILD}")

# 57 every feed goes through pf.net, which carries certifi's CA bundle.
#    Python on Windows trusts nothing by default, so a module left on plain
#    urllib raises CERTIFICATE_VERIFY_FAILED against perfectly ordinary sites
#    while yfinance — which ships certifi — works in the same process. That
#    was the ECB and FRED failure, and it is invisible on Linux, so it needs a
#    check rather than a habit.
import glob as _glob
_raw = []
for _f in _glob.glob(os.path.join(HERE, "backend", "pf", "*.py")):
    if os.path.basename(_f) == "net.py":
        continue
    if "urlopen(" in open(_f, encoding="utf-8").read():
        _raw.append(os.path.basename(_f))
from pf import net as _net
_ca = _net.ca_source()
print(f"57 one HTTP client, one CA  {ok(not _raw and 'certifi' in _ca)}  "
      f"{_ca}; transports "
      f"{', '.join(f'{k} {v}' for k, v in _net.transports_available().items())}"
      + (f" — still on raw urllib: {_raw}" if _raw else ""))

# 58 the GPR file is an .xls whose first row is sometimes data, not a header.
#    pandas then returns a Timestamp where a column NAME should be, and the
#    first parser called .strip() on it. That AttributeError propagated out of
#    the whole sentiment build, so CNN and the Fed panel went on serving the
#    previous build's errors for hours. Four shapes, one parser.
import io as _io, pandas as _pd
_d = _pd.date_range("2025-01-01", periods=300)
_v = [100 + i * 0.3 for i in range(300)]
_full = _pd.DataFrame({"date": _d, "GPRD": _v, "GPRD_MA7": [1] * 300})


def _sheet(df, **kw):
    b = _io.BytesIO()
    with _pd.ExcelWriter(b) as w:
        df.to_excel(w, index=False, **kw)
    return b.getvalue()


_shapes = {
    "no header row": _sheet(_pd.DataFrame({0: _d, 1: _v}), header=False),
    "header on row 0": _sheet(_full),
    "header pushed down": _sheet(_full, startrow=3),
    "a CSV instead": _full.to_csv(index=False).encode(),
}
_parsed = {k: _sent._gpr_table(b) for k, b in _shapes.items()}
_ok58 = all(len(t["rows"]) == 300 and t["rows"][0][0] == "2025-01-01"
            for t in _parsed.values())
print(f"58 GPR parses every shape {ok(_ok58)}  "
      + ", ".join(f"{k}: {len(t['rows'])} rows via {t['vcol']}"
                  for k, t in _parsed.items())[:120])

# 59 one broken source must not discard the other three. This is the bug that
#    made three panels lie: the GPR parser raised, the exception escaped the
#    sentiment build, cache_put was never reached, and the page served answers
#    from the build before — errors and all — as though they were current.
import types as _types
from pf import brief as _brief
_boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the source exploded"))
_saved = (_sent.gpr, _sent.cnn_fear_greed, _sent.front_fed_funds)
try:
    _sent.gpr = _boom
    _payload = _brief.BRIEF._build_sentiment()
    _survived = (isinstance(_payload, dict)
                 and _payload.get("house") is not None
                 and "exploded" in str((_payload.get("gpr") or {}).get("error", "")))
finally:
    _sent.gpr, _sent.cnn_fear_greed, _sent.front_fed_funds = _saved
print(f"59 a broken source fails alone {ok(_survived)}  "
      f"gpr raised; the payload still carried "
      f"{sorted(k for k in (_payload or {}) if k in ('cnn','house','fed','gpr'))}")

# 60 a stale SUCCESS is fine; a stale FAILURE is a lie. Rates are fetched every
#    six hours, so a feed that failed once kept reporting itself broken for six
#    hours — straight through the restart that had fixed it.
_fail_states = {
    "its own error": {"error": "boom"},
    "a failed sub-source": {"cnn": {"ok": False, "error": "418"}, "house": {"ok": True}},
    "a failed entry under sources": {"sources": {"ecb": {"ok": False}, "fred": {"ok": True}}},
}
_clean = {"rows": [1], "error": None, "cnn": {"ok": True}}
_ok60 = (all(_brief.Brief._payload_failed(p) for p in _fail_states.values())
         and not _brief.Brief._payload_failed(_clean)
         and _brief.Brief.RETRY_FAILED <= 600)
print(f"60 failures retry in minutes {ok(_ok60)}  "
      f"{_brief.Brief.RETRY_FAILED}s for a failed payload vs "
      f"{_brief.TTL['rates']}s for a good one; detects "
      f"{len(_fail_states)} shapes of failure")

# 61 the contract must cover the MEETING's month. "ZQ=F" is the front month,
#    and for most of the year that is a different month from the next meeting:
#    on 17 September the front contract is September's while the meeting is in
#    November. Dividing one month's price by another month's calendar produced
#    a confident "100% probability of a hike" out of nothing.
from datetime import date as _date2
_sym = _sent.zq_symbol(_date2(2026, 11, 4))
# The decision of 28 October is read from NOVEMBER's contract (a month with
# no meeting in it, so one rate all month), never from the front month.
_ch61 = _rp.fed_chain({(2026, 10): 3.63, (2026, 11): 3.63, (2026, 12): 3.63},
                      [_date2(2026, 10, 28)], 3.63, _date2(2026, 10, 1))["meetings"][0]
# The real prices that once read as "100% hike": October's contract at 3.895
# and November's at 4.02 are 12.5bp apart, which is half a step, not a full one.
_real61 = _rp.fed_chain({(2026, 10): 100 - 96.105, (2026, 11): 100 - 95.98}, [_date2(2026, 10, 28)],
                        None, _date2(2026, 10, 1))["meetings"][0]
_ok61 = (_sym == "ZQX26.CBT" and _ch61["contract"] == "ZQX26.CBT" and _ch61["how"] == "next"
         and _ch61["hold"] > 99 and 0 < _real61["hike"] < 75)
print(f"61 the meeting's own contract {ok(_ok61)}  "
      f"28 Oct 2026 is read from {_ch61['contract']} ({_ch61['how']} month); "
      f"the prices that once said 100% hike say {_real61['hike']:.0f}%")

# 62 a baseline that predates the last meeting is flagged, not used silently.
#    The published effective rate lags the decision by a couple of days, so
#    just after a meeting it is still the OLD rate — and the arithmetic then
#    reads a move that has already happened as one that is about to.
import inspect as _i3
_av62 = {(2026, 10): 3.80, (2026, 11): 3.58, (2026, 12): 3.58}
_old62 = _rp._baseline(_av62, {"2026-10-27": 3.83}, _date2(2026, 10, 30))      # published before the 28th
_new62 = _rp._baseline(_av62, {"2026-10-29": 3.58}, _date2(2026, 10, 30))      # published after it
_ok62 = (_old62[2] is True and abs(_old62[0] - 3.58) < 1e-9 and "November" in _old62[1]
         and _new62[2] is False and _new62[0] == 3.58
         and "stale_baseline" in _i3.getsource(_rp.next_fed)
         and "ratepath.next_fed" in _i3.getsource(_brief.Brief._build_sentiment))
print(f"62 a stale rate is declared  {ok(_ok62)}  "
      f"a rate published before the decision is replaced by {_old62[1]} and flagged; "
      f"one published after it is used as it is")

# 63 a cached payload built by older code must not survive a build change.# 63 a cached payload built by older code must not survive a build change.
#    Cached answers outliving a restart is the point of them — but a payload
#    keeps the SHAPE of the build that wrote it, and a page reading a field
#    that build never wrote renders "no data" while looking perfectly healthy.
#    It happened twice in one evening, so the build is stamped on every entry.
import json as _j63
_k63 = "verify_build_probe"
store.cache_put(_k63, {"hello": 1})
_stamped = (store.cache_get(_k63) or {}).get("_build")
_b63 = _brief.Brief()
_fresh_due = _b63._due("news", "") if False else None      # names must be in TTL
store.cache_put("news", {"items": [1], "error": None})
_now_due = _b63._due("news", "")
with store.conn() as _c63:
    _v63 = _j63.loads(_c63.execute("SELECT v FROM cache WHERE k='news'").fetchone()["v"])
    _v63["_build"] = _BUILD - 1
    _c63.execute("UPDATE cache SET v=? WHERE k=?", (_j63.dumps(_v63), "news"))
_old_due = _b63._due("news", "")
print(f"63 old-build cache is rebuilt {ok(_stamped == _BUILD and not _now_due and _old_due)}  "
      f"entries stamped build {_stamped}; a current payload waits its TTL, "
      f"one from build {_BUILD - 1} rebuilds at once")

# ==========================================================================
# the day change (build 17)
# ==========================================================================
from pf import stream as _st, pricing as _pr

# 64 the stream's day change is measured against the DASHBOARD's yesterday.
#    Real numbers off Rick's screen, Monday 21 September: AMZN at 255.54, Yahoo's
#    previous close 253.71, and a stream quote carrying 255.25 as its "previous
#    close" — a same-day price. The page showed +0.11%. It was +0.72%.
_base64 = {"fx_symbol": {"USD": "EURUSD=X"}, "positions": [
    {"isin": "US0231351067", "ticker": "AMZN", "qty": 6, "currency": "USD",
     "price": 255.22, "prev_close": 253.71, "fx": 1.1489, "fx_prev": 1.1489,
     "cost_eur": 1200.0}]}
_q64 = {"AMZN": {"price": 255.54, "prev_close": 255.25, "src": "stream"},
        "EURUSD=X": {"price": 1.1489, "prev_close": 1.1489}}
_f64 = _st.fold(_base64, _q64)
_r64 = _f64["positions"][0]
print(f"64 one definition of yesterday {ok(abs(_r64['day_pct'] - 0.72) < 0.01 and _r64['prev_close'] == 253.71)}  "
      f"AMZN 255.54 vs {_r64['prev_close']} = {_r64['day_pct']:+.2f}% "
      f"(the stream's own 255.25 would have said +0.11%)")

# 65 the currency leg is measured against YESTERDAY's rate. live_base used to
#    copy today's rate into fx_prev, so the FX leg only ever saw the minutes
#    since the base was built.
_base65 = {"fx_symbol": {"USD": "EURUSD=X"}, "positions": [
    {"isin": "X", "ticker": "T", "qty": 10, "currency": "USD",
     "price": 100.0, "prev_close": 100.0, "fx": 1.15, "fx_prev": 1.14, "cost_eur": 800.0}]}
_f65 = _st.fold(_base65, {"T": {"price": 100.0}, "EURUSD=X": {"price": 1.15}})
_fx65 = _f65["positions"][0]["day_fx_eur"]
_want65 = round(10 * 100 * (1 / 1.15 - 1 / 1.14), 2)
_lb = _i3.getsource(service.live_base)
_carries65 = 'r.get("fx_prev") or' in _lb
print(f"65 FX leg uses yesterday's rate {ok(abs(_fx65 - _want65) < 0.01 and _carries65)}  "
      f"EUR/USD 1.14 -> 1.15 on $1,000 = {_fx65:+.2f} EUR (expected {_want65:+.2f}); "
      f"live_base now carries the row's fx_prev")

# 66 a quote rebuilt from the cache never claims the price IS the previous
#    close. That is the line that wrote a same-day price in as "yesterday"
#    after a single Yahoo failure tripped the circuit breaker.
_save66 = _pr.store.cached_prices
from datetime import datetime as _dt66
from zoneinfo import ZoneInfo as _zi66
# Monday 21 September at noon in New York, the session running: the stored
# 255.25 is that session so far, and yesterday is Friday's 253.71. (Since build
# 33 the answer depends on the time: after the close the day starts again.)
_noon66 = _dt66(2026, 9, 21, 12, 0, tzinfo=_zi66("America/New_York"))
try:
    _pr.store.cached_prices = lambda t: {"2026-09-18": 253.71, "2026-09-21": 255.25}
    _c66 = _pr._from_cache("AMZN", "cache", _noon66)
    _pr.store.cached_prices = lambda t: {"2026-09-21": 255.25}
    _c66b = _pr._from_cache("AMZN", "cache", _noon66)
finally:
    _pr.store.cached_prices = _save66
print(f"66 a cached quote keeps yesterday {ok(_c66['prev_close'] == 253.71 and _c66b['prev_close'] is None)}  "
      f"two stored days -> prev {_c66['prev_close']}; one stored day -> prev "
      f"{_c66b['prev_close']} (unknown, not the price)")

# 67 the poller never replaces a previous close the feed already holds with a
#    derived one from the cache.
_s67 = _st.Streamer()
_s67._put("AMZN", 255.30, 253.71, src="stream")
_src67 = _i3.getsource(_st.Streamer._poll_for)
_s67._put("AMZN", 255.40, None, src="poll")          # what the fixed poller passes
print(f"67 a known close is never overwritten {ok(_s67._quotes['AMZN']['prev_close'] == 253.71 and 'from_cache' in _src67)}  "
      f"after a cache-sourced poll the stream still holds {_s67._quotes['AMZN']['prev_close']}")

# 68 a feed revived mid-shutdown says what it is doing. A page reload drops
#    the listener count to zero for a few milliseconds: stop() wrote "off",
#    the thread never actually stopped, and the panel said "off · nobody is
#    watching" all day while prices ticked.
import threading as _th
_s68 = _st.Streamer()
_s68._set_state("streaming", "live from Yahoo, 63 symbols")
_s68._thread = _th.Thread(target=lambda: _th.Event().wait(2), daemon=True)
_s68._thread.start()
_s68._base_thread = _s68._thread
_s68.stop()
_was = _s68.state
_s68.start()
print(f"68 a revived feed is not 'off' {ok(_was == 'off' and _s68.state == 'streaming')}  "
      f"after a reload the label went {_was!r} -> {_s68.state!r}")
_s68._stop.set()

# ==========================================================================
# the research page, any symbol (build 18)
# ==========================================================================
from pf import sec as _sec, research as _rs, news as _nw9
from datetime import timedelta as _td9

# 69 a fourth quarter is the year minus nine months, and a cash-flow quarter is
#    this year-to-date minus the last one — but a per-share figure is NEVER
#    derived by subtraction: the year and the nine months can sit either side
#    of a split, which is how Nvidia's Q4 EPS once came out at -1.0.
_dur = {("2025-07-01", "2025-09-30"): {"v": 10.0, "filed": "2025-10-29"},
        ("2025-07-01", "2025-12-31"): {"v": 25.0, "filed": "2026-01-28"},
        ("2025-07-01", "2026-03-31"): {"v": 45.0, "filed": "2026-04-29"},
        ("2025-07-01", "2026-06-30"): {"v": 70.0, "filed": "2026-07-29"}}
_q69 = _sec._quarters(_dur, "flow")
_e69 = _sec._quarters(_dur, "eps")
_v69 = [round(_q69[k]["v"]) for k in sorted(_q69)]
print(f"69 quarters from year-to-date {ok(_v69 == [10, 15, 20, 25] and _q69['2026-06-30']['derived'] and len(_e69) == 1)}  "
      f"YTD 10/25/45/70 -> {_v69}; per-share keeps only the reported quarter")

# 70 per-share values are put on today's share basis by the splits AFTER the
#    filing each value came from — a figure restated after a split is not
#    divided again.
_sp = [["2024-06-10", 10.0]]
print(f"70 splits applied once      {ok(_rs._split_factor(_sp, '2024-05-29') == 10.0 and _rs._split_factor(_sp, '2025-05-28') == 1.0)}  "
      f"filed before the 10:1 split -> x10; filed after it -> x1")

# 71 the valuation history never uses a number before the market could know
#    it: each TTM point is stamped with the FIRST filing of its latest quarter.
_qs = [{"end": e, "filed": f, "revenue": 100.0, "eps": 1.0, "shares": 10.0, "ocf": 5.0, "capex": 1.0,
        "op_income": 30.0, "fcf": 4.0} for e, f in
       (("2025-09-30", "2025-10-29"), ("2025-12-31", "2026-01-28"), ("2026-03-31", "2026-04-29"),
        ("2026-06-30", "2026-07-29"))]
_p71 = _rs._ttm_points({"quarters": _qs}, [])
print(f"71 no look-ahead in the bands {ok(len(_p71) == 1 and _p71[0]['known'] == '2026-07-29' and _p71[0]['eps'] == 4.0)}  "
      f"TTM to 30/06 becomes usable on {_p71[0]['known'] if _p71 else '?'}, the day the 10-K was filed")

# 72 Finnhub links differ only in their query string. Stripping the query
#    made every one of them the same URL, and deduplication kept one story.
_u1, _u2 = "https://finnhub.io/api/news?id=aaa", "https://finnhub.io/api/news?id=bbb"
print(f"72 Finnhub stories distinct {ok(_nw9.url_key(_u1) != _nw9.url_key(_u2) and _nw9.url_key('https://x.com/a?utm_source=y') == 'https://x.com/a')}  "
      f"ids kept, tracking parameters dropped")

# 73 an upgrade "to Buy" is an analyst call, not an acquisition; a listicle is
#    opinion however many trillions it quotes.
_c73 = [_rs.classify({"headline": h, "summary": "", "source": "x", "ts": None}, "MSFT", "Microsoft")["cat"] for h in (
    "Stifel Upgrades Microsoft to Buy, Raises Price Target to $575",
    "Microsoft to acquire Contoso for $2.1 billion",
    "1 Unstoppable Stock to Buy Before It Joins Microsoft in the $3 Trillion Club")]
print(f"73 headlines classified     {ok(_c73 == ['analyst', 'deal', 'opinion'])}  {_c73}")

# 74 every session splits exactly into market + sector + company, and a jump
#    on an earnings day is put down to the company, with the report as cause.
import random as _rnd
_rnd.seed(7)
_days74, _bars74, _mk74, _sc74 = [], [], {}, {}
_d0 = date(2024, 1, 1)
_pS = _pM = _pX = 100.0
_i74 = 0
_jump = None
while len(_days74) < 400:
    _dd = _d0 + _td9(days=_i74)
    _i74 += 1
    if _dd.weekday() >= 5:
        continue
    _ds = _dd.isoformat()
    _m = _rnd.gauss(0, 0.01)
    _x = _m + _rnd.gauss(0, 0.006)
    _r = 1.2 * _m + 0.5 * (_x - _m) + _rnd.gauss(0, 0.008)
    if len(_days74) == 380:
        _jump = _ds
        _r += 0.08
    _pM *= 1 + _m
    _pX *= 1 + _x
    _pS *= 1 + _r
    _days74.append(_ds)
    _mk74[_ds] = _pM
    _sc74[_ds] = _pX
    _bars74.append([_ds, _pS, _pS * 1.01, _pS * .99, _pS, 1000000, _pS])
_prev_day = _days74[_days74.index(_jump) - 1]
_saved74 = (_rs.daily, _rs._closes, _rs.overview, _rs.street, _rs.news, _sec.cik_for, _nw9.api_key)
try:
    _rs.daily = lambda s: {"bars": _bars74, "divs": [], "splits": []}
    _rs._closes = lambda s: _mk74 if s == "^GSPC" else _sc74
    _rs.overview = lambda s: {"tz": "America/New_York", "short": "Test", "sector": "Technology", "industry": ""}
    _rs.street = lambda s: {"surprises": [{"date": _prev_day, "when": "after close", "eps_act": 2.0, "eps_est": 1.5,
                                           "surprise_pct": 33.3, "reaction": {"day": _jump}}], "actions": []}
    _rs.news = lambda s: {"items": []}
    _sec.cik_for = lambda s: None
    _nw9.api_key = lambda: None
    _mv = _rs._load_moves("TEST")
finally:
    (_rs.daily, _rs._closes, _rs.overview, _rs.street, _rs.news, _sec.cik_for, _nw9.api_key) = _saved74
_all74 = _mv["days"] + _mv["biggest"]
_gap74 = max(abs(x["r"] - (x["mkt"] + x["sec"] + x["idio"])) for x in _all74 if x["idio"] is not None)
_j74 = next(x for x in _mv["biggest"] if x["d"] == _jump)
print(f"74 moves decompose exactly  {ok(_gap74 < 0.02 and _j74['driver'] == 'company' and 'Earnings' in _j74['why'] and abs(_mv['model']['beta_market'] - 1.2) < 0.15)}  "
      f"max gap {_gap74:.3f}pp; beta {_mv['model']['beta_market']} (true 1.2); the +8% day: {_j74['driver']}, cause found")

# 75 the thesis is append-only: a second save is a second version, the first
#    is untouched, and only notes can be deleted. Run against a throwaway
#    database, never your ledger.
import tempfile as _tf
_db0 = store.DB_PATH
_sv75 = _rs._now_price
store.DB_PATH = os.path.join(_tf.mkdtemp(), "thesis-test.db")
try:
    store.init_db()
    _rs._now_price = lambda s: (100.0, "USD")
    _rs.thesis_save("TEST", {"stance": "bull", "thesis": "first", "conviction": 3})
    _rs._now_price = lambda s: (120.0, "USD")
    _rs.thesis_save("TEST", {"stance": "neutral", "thesis": "second"})
    _t75 = _rs.note_add("TEST", "a note")
    _del_v = _rs.note_delete(_t75["versions"][0]["id"])
    _del_n = _rs.note_delete(_t75["notes"][0]["id"])
    _t75b = _rs.thesis_get("TEST")
finally:
    _rs._now_price = _sv75
    store.DB_PATH = _db0
print(f"75 thesis is append-only    {ok(len(_t75b['versions']) == 2 and _t75b['versions'][1]['thesis'] == 'first' and _t75b['versions'][1]['price_at'] == 100.0 and not _del_v and _del_n and not _t75b['notes'])}  "
      f"two versions kept (the first at 100), a version cannot be deleted, a note can")

# 76 fiscal labels: Microsoft's September quarter is Q1 of the year ending the
#    next June; Apple's 52-week December quarter is Q1 of the year ending September.
_l76 = [_rs.fiscal_label("2025-09-30", "0630"), _rs.fiscal_label("2026-06-30", "0630"),
        _rs.fiscal_label("2025-12-27", "0927"), _rs.fiscal_label("2026-03-31", None),
        _rs.fiscal_label("2026-06-30", "0630", annual=True)]
print(f"76 fiscal quarters named    {ok(_l76 == ['Q1 FY26', 'Q4 FY26', 'Q1 FY26', 'Q1 FY26', 'FY26'])}  {_l76}")

# 77 the reverse DCF inverts: discounting at the growth it reports gives back
#    the enterprise value it was handed.
_g77 = _rs.implied_growth(100.0, 4000.0) / 100
_pv77, _f77 = 0.0, 100.0
for _t77 in range(1, 11):
    _f77 *= 1 + _g77
    _pv77 += _f77 / 1.09 ** _t77
_pv77 += _f77 * 1.03 / 0.06 / 1.09 ** 10
print(f"77 implied growth inverts   {ok(abs(_pv77 - 4000.0) < 1.0)}  FCF 100, EV 4,000 -> {_g77 * 100:.2f}% a year, "
      f"which values it at {_pv77:,.1f}")

# 78 a symbol with an exchange suffix, or an index, is not looked up at the
#    SEC at all.
print(f"78 SEC lookup scoped        {ok(_sec.cik_for('INGA.AS') is None and _sec.cik_for('^GSPC') is None)}  "
      f"INGA.AS and ^GSPC skip EDGAR without a network call")

# ==========================================================================
# the bond market page (build 19)
# ==========================================================================
from pf import bonds as _bd, feeds as _fd

# 79 FRED's keyless CSV silently returns only the first twelve ids of a longer
#    list. Asking for twenty-three must mean batches of ten or fewer, and every
#    series must come back.
_ids79 = [f"S{i:02d}" for i in range(23)]
_seen79 = []


def _fake_get79(url):
    import urllib.parse as _up
    q = _up.parse_qs(_up.urlparse(url).query)
    ids = q["id"][0].split(",")
    _seen79.append(len(ids))
    head = "observation_date," + ",".join(ids[:12])
    return (head + "\n2026-09-01," + ",".join("1.0" for _ in ids[:12]) + "\n").encode()


_sv79 = _fd._get
try:
    _fd._get = _fake_get79
    _r79 = _fd.fred(_ids79, days=30)
finally:
    _fd._get = _sv79
print(f"79 FRED asked ten at a time {ok(max(_seen79) <= 10 and len(_r79['series']) == 23)}  "
      f"{len(_seen79)} requests of {_seen79}, {len(_r79['series'])}/23 series back")

# 80 the curve's moves are named by who drove them: both up with the long end
#    most is a bear steepener, and so on for all six.
_rg = [(_bd.regime(a, b) or {}).get("k") for a, b in
       ((20, 40), (40, 20), (-40, -10), (-10, -40), (-20, 20), (20, -20), (1, -1))]
print(f"80 curve moves named        {ok(_rg == ['bear steepener', 'bear flattener', 'bull steepener', 'bull flattener', 'twist steepener', 'twist flattener', 'unchanged'])}  {_rg}")

# 81 series are stored as one date index plus columns; a round trip must give
#    back exactly what went in, gaps included.
_p81 = {"A": {"2026-01-02": 1.0, "2026-01-05": 2.0}, "B": {"2026-01-05": 3.5}}
print(f"81 compact storage exact    {ok(_bd._unpack(_bd._pack(_p81)) == _p81)}  two series, one gap, round-tripped")

# 82 an inversion is a stretch of twenty sessions or more below zero; a single
#    dip is not one, and the deepest point is reported in basis points.
_d82 = [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(80)]
_v82 = [0.3] * 10 + [-0.2, -0.5] + [-0.3] * 28 + [0.1] * 20 + [-0.1] + [0.2] * 19
_e82 = _bd._episodes(_bd.Ser(dict(zip(_d82, _v82))), since="2020-01-01")
print(f"82 inversions found         {ok(len(_e82) == 1 and _e82[0]['days'] == 30 and _e82[0]['low'] == -50 and _e82[0]['to'] == _d82[39])}  "
      f"{[(e['from'], e['to'], e['days'], e['low']) for e in _e82]}")

# 83 a maturity whose last print is weeks older than the day asked for is a
#    gap, not a value — the 20-year did not exist from 1987 to 1993.
_c83 = _bd.curve_on({"DGS10": {"1990-06-01": 8.4}, "DGS20": {"1986-12-31": 7.9}}, "1990-06-01")
print(f"83 missing tenors stay gaps {ok([p['t'] for p in _c83] == ['10Y'])}  {[(p['t'], p['v']) for p in _c83]}")

# 84 a euro investor's hedged Treasury yield is the yield minus the gap in
#    3-month rates, and a fund's yield is the curve at its maturity minus costs.
_us84 = {"DGS3MO": {"2026-09-25": 4.24}, "DGS5": {"2026-09-25": 5.0}, "DGS10": {"2026-09-25": 5.2}}
_ea84 = {"EA_ALL_3M": {"2026-09-24": 2.66}}
_h84 = _bd.hedge_cost(_us84, _ea84)
_y84 = _bd.fund_yield({"curve": ("UST_H", 7.5), "ter": 0.10}, _us84, _ea84, _h84)
print(f"84 hedged yield arithmetic  {ok(_h84['cost'] == 1.58 and _y84 == round(5.1 - 1.58 - 0.10, 2))}  "
      f"hedge {_h84['cost']}pp; 7.5y hedged fund {_y84}% (5.10 interpolated - 1.58 - 0.10)")

# 85 twelve-month scenarios: carry minus duration times the move, plus
#    convexity. With no move the return is the yield; +100bp on a 7-year
#    duration at 3% is about -3.8%.
_s85 = {s["shift"]: s["ret"] for s in _bd.scenarios(3.0, 7.0)}
print(f"85 rate scenarios           {ok(_s85[0] == 3.0 and abs(_s85[100] + 3.76) < 0.02 and _s85[-100] > -_s85[100])}  {_s85}")

# 86 a blend is the weighted sum of the two returns each day, and a known path
#    has a known drawdown.
_b86 = _bd.blend({"d1": 0.10, "d2": -0.10}, {"d1": 0.0, "d2": 0.02, "d3": 1.0}, 0.25)
_st86 = _bd._stats([0.1] * 30 + [-0.5] + [0.0] * 40,
                   [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(71)])
print(f"86 blend and drawdown       {ok(abs(_b86['d1'] - 0.075) < 1e-12 and abs(_b86['d2'] + 0.07) < 1e-12 and len(_b86) == 2 and _st86['maxdd'] == -50.0)}  "
      f"{_b86}; one -50% day -> max drawdown {_st86['maxdd']}%")

# 87 yfinance 1.x keeps (ticker, field) columns even for ONE symbol; reading
#    the flat layout silently returned no S&P 500 at all.
import pandas as _pd87
_df87 = _pd87.DataFrame([[5000.0], [5050.0]], index=_pd87.to_datetime(["2026-09-24", "2026-09-25"]),
                        columns=_pd87.MultiIndex.from_tuples([("^GSPC", "Close")]))
_flat87 = _pd87.DataFrame({"Close": [1.0, 2.0]}, index=_pd87.to_datetime(["2026-09-24", "2026-09-25"]))
print(f"87 single-ticker layouts    {ok(_bd._closes(_df87, '^GSPC', False) == {'2026-09-24': 5000.0, '2026-09-25': 5050.0} and len(_bd._closes(_flat87, 'X', False)) == 2)}  "
      f"grouped and flat frames both read")

# 88 your book's history: dollar holdings converted at EUR/USD, and a holding
#    that did not trade yet is left out with the rest re-weighted.
_px88 = {"A": {"2026-01-01": 100.0, "2026-01-02": 110.0, "2026-01-03": 110.0},
         "B": {"2026-01-02": 50.0, "2026-01-03": 55.0},
         "EURUSD=X": {"2026-01-01": 1.0, "2026-01-02": 1.0, "2026-01-03": 1.1}}
_r88, _ = _bd._book_returns({"rows": [{"t": "A", "w": 0.7, "ccy": "USD"}, {"t": "B", "w": 0.3, "ccy": "EUR"}]}, _px88)
_want88 = {"2026-01-02": 0.10, "2026-01-03": 0.7 * (1 / 1.1 - 1) + 0.3 * 0.10}
print(f"88 book returns in euros    {ok(all(abs(_r88[d] - v) < 1e-9 for d, v in _want88.items()) and len(_r88) == 2)}  "
      f"day 2 is A alone (B not trading), day 3 blends A's dollar loss with B's +10%")

# 89 Japan's MOF dates its older rows by imperial era; a reopened 10-year is
#    still the 10-year auction; percentiles read 1st, 2nd, 3rd, 11th, 22nd.
print(f"89 dates, terms, ordinals   {ok(_bd._mof_date('H31.4.26') == '2019-04-26' and _bd._mof_date('2026/8/3') == '2026-08-03' and _bd._term_key({'securityTerm': '9-Year 10-Month', 'originalSecurityTerm': '10-Year'}) == '10-Year' and [_bd._ord(n) for n in (1, 2, 3, 11, 22, 96)] == ['1st', '2nd', '3rd', '11th', '22nd', '96th'])}  "
      f"H31.4.26 -> {_bd._mof_date('H31.4.26')}")

# 90 a rate beta recovers a sensitivity planted in the data: a stock built to
#    lose 0.5% per +10bp, on top of 1.2x the market, is measured as such.
import random as _rn90
_rn90.seed(3)
_dy90, _mk90, _st90 = {}, {}, {}
for _i in range(300):
    _d = (date(2025, 1, 1) + timedelta(days=_i)).isoformat()
    _dy90[_d] = _rn90.gauss(0, 0.06)
    _mk90[_d] = _rn90.gauss(0, 0.01) - 0.2 * _dy90[_d] / 10
    _st90[_d] = 1.2 * _mk90[_d] - 0.05 * _dy90[_d] * 10 / 10 + _rn90.gauss(0, 0.004)
_rb90 = _bd.rate_beta(_st90, _dy90, _mk90)
print(f"90 rate beta recovered      {ok(abs(_rb90['per10bp_ex_mkt'] + 0.5) < 0.1 and abs(_rb90['beta_mkt'] - 1.2) < 0.1)}  "
      f"{_rb90['per10bp_ex_mkt']}% per +10bp beyond the market (true -0.5), market beta {_rb90['beta_mkt']} (true 1.2)")


# ==========================================================================
# the earnings page (build 20)
# ==========================================================================
from pf import earnings as _E, earndocs as _ed, earnai as _ai

# 91 symbols and sessions: BRK.B is BRK-B here, an Amsterdam line keeps its
#    suffix; 16:05 is after the close, 07:00 before the open, and 15:00 counts
#    as after the close (Yahoo's estimated dates past a US clock change).
print(f"91 symbols and sessions     {ok(_E.ysym('brk.b') == 'BRK-B' and _E.ysym('INGA.AS') == 'INGA.AS' and [_E.when_of(t) for t in ('16:05', '07:00', '15:00', '11:30')] == ['amc', 'bmo', 'amc', 'dmh'])}  "
      f"BRK.B -> {_E.ysym('brk.b')}; 16:05/07:00/15:00/11:30 -> {[_E.when_of(t) for t in ('16:05', '07:00', '15:00', '11:30')]}")

# 92 Amsterdam time for a New York release: six hours apart most of the year,
#    five in the week Europe has left summer time and the US has not.
_lt92 = [_E.local_time(d, "16:00") for d in ("2026-10-20", "2026-10-28", "2026-11-03")]
print(f"92 New York to Amsterdam    {ok(_lt92 == ['22:00', '21:00', '22:00'])}  20/10, 28/10, 03/11 at 16:00 ET -> {_lt92}")

# 93 filing figures: '$1,234.5', '(0.12)' is a loss, a dash is nothing, '~$79,750' is a guide.
_n93 = [_ed.num(x) for x in ("$1,234.5", "(0.12)", "—", "~$79,750", "12.4%")]
print(f"93 figures read from tables {ok(_n93 == [1234.5, -0.12, None, 79750.0, 12.4])}  {_n93}")

# 94 which column is the quarter: statements put it first, shareholder letters
#    run oldest to newest with next quarter's forecast last.
_c94a = _ed._cols({"rows": [["(in millions)", "Q2'25", "Q3'25", "Q4'25", "Q1'26", "Q2'26", "Q3'26 Forecast"]], "before": ""})
_c94b = _ed._cols({"rows": [["June 30,2026", "March 31,2026", "June 30,2025"]], "before": ""})
_c94c = _ed._cols({"rows": [["", "2026", "2025"]], "before": ""})
print(f"94 quarter, year ago, fcst  {ok((_c94a['cur'], _c94a['ya'], _c94a['fc']) == (4, 0, 5) and (_c94b['cur'], _c94b['ya']) == (0, 2) and (_c94c['cur'], _c94c['ya']) == (0, 1))}  "
      f"letter {(_c94a['cur'], _c94a['ya'], _c94a['fc'])}, statement {(_c94b['cur'], _c94b['ya'])}, years {(_c94c['cur'], _c94c['ya'])}")

# 95 a press release read end to end: revenue in dollars from a table in
#    millions, growth on the year, GAAP and adjusted EPS, and a guidance range.
_pr95 = b"""<html><body><p>Acme reports second quarter results.</p>
<p>Adjusted EPS was $1.40, up 20% on the year.</p>
<p>For the third quarter, the company expects revenue of $1.05 billion to $1.10 billion.</p>
<p>Second quarter revenue was in line with our forecast of $1.00 billion.</p>
<p>(In millions, except per share amounts)</p>
<table><tr><td></td><td>Three Months Ended June 30,</td></tr>
<tr><td></td><td>2026</td><td>2025</td></tr>
<tr><td>Revenue</td><td>$</td><td>1,000</td><td>$</td><td>900</td></tr>
<tr><td>Operating income</td><td>300</td><td>250</td></tr>
<tr><td>Net income</td><td>200</td><td>150</td></tr>
<tr><td>Diluted earnings per share</td><td>$</td><td>1.23</td><td>$</td><td>0.95</td></tr>
</table></body></html>"""
_p95 = _ed.parse_release(_pr95, 990e6, 1.35)
_g95 = [g for g in _p95["guidance"] if g.get("metric") == "revenue"]
print(f"95 press release read       {ok(_p95['revenue']['value'] == 1e9 and _p95['revenue']['yoy'] == 11.11 and _p95['eps_gaap']['value'] == 1.23 and _p95['eps_adj']['value'] == 1.40 and _p95['eps_basis'] == 'eps_adj' and len(_g95) == 1 and _g95[0]['low'] == 1.05e9 and _g95[0]['high'] == 1.10e9)}  "
      f"revenue {_p95['revenue']['value']:,.0f} ({_p95['revenue']['yoy']}% y/y), EPS {_p95['eps_gaap']['value']} GAAP / {(_p95['eps_adj'] or {}).get('value')} adjusted, "
      f"guide {[(g['low'], g['high']) for g in _g95]}; 'in line with our forecast' is not guidance")

# 96 a release in New Taiwan dollars is not compared with a dollar consensus.
_p96 = _ed.parse_release(b"<p>TSMC reported revenue of NT$1,270.38 billion. EPS of NT$27.25. NT$ NT$ NT$ NT$ NT$</p>")
print(f"96 foreign currency caught  {ok(_p96['currency'] == 'TWD' and any('TWD' in n for n in _p96['notes']))}  {_p96['currency']}: {_p96['notes']}")

# 97 European names to SEC filers: 'N.V.' and 'Groep' fall away, so ING Groep
#    N.V. matches ING GROEP NV; the filer check then refuses US look-alikes.
print(f"97 company names normalised {ok(_ed.norm_name('ING GROEP N.V.') == _ed.norm_name('ING GROEP NV') == 'ing' and _ed.norm_name('Shell plc') == 'shell')}  "
      f"{_ed.norm_name('ING GROEP N.V.')!r}, {_ed.norm_name('Shell plc')!r}")

# 98 a transcript read: the operator's introduction names the analyst's firm,
#    the Q&A starts at the first question, and a non-answer is flagged.
_t98 = _ed.transcript_from_text("""Operator: Welcome to the call.
Jane Doe: Revenue grew 12% and we expect revenue growth of about 10% next quarter.
Operator: Our first question comes from the line of Bob Smith with Big Bank.
Bob Smith: Can you size the pricing benefit in Europe?
Jane Doe: We don't break that out, but demand was strong.""")

_a98 = _ed.call_analytics(_t98)
print(f"98 call parsed              {ok([t['section'] for t in _t98['turns']] == ['prepared', 'prepared', 'qa', 'qa', 'qa'] and len(_a98['questions']) == 1 and _a98['questions'][0]['evasive'] and _a98['questions'][0]['answered_by'] == ['Jane Doe'] and len(_a98['guidance']) == 1)}  "
      f"{len(_a98['questions'])} question, evasive={_a98['questions'][0]['evasive']}, outlook lines {len(_a98['guidance'])}")

# 99 the playbook: beat rate, the median move on beats and misses, and beats
#    that still fell.
_r99 = [{"surprise": s, "move": m, "gap": None, "d5": None} for s, m in
        [(6, 4.0), (2, -3.0), (8, 6.0), (-4, -7.0), (1, 1.0), (3, -1.0)]]
_s99 = _E._stats(_r99)
print(f"99 what usually happens     {ok(_s99['n'] == 6 and _s99['beat_rate'] == 83 and _s99['beat']['median'] == 1.0 and _s99['miss']['median'] == -7.0 and _s99['beat_fell'] == 2 and _s99['big_beat']['n'] == 2)}  "
      f"beat {_s99['beat_rate']}%, median on beats {_s99['beat']['median']}%, on misses {_s99['miss']['median']}%, {_s99['beat_fell']} beats fell")

# 100 what a report costs: tokens at the model's price, cached reads at a tenth.
_c100 = _ai.cost("claude-opus-5", {"input_tokens": 100_000, "output_tokens": 5_000, "cache_read_input_tokens": 50_000})
print(f"100 report cost             {ok(_c100 == round(0.5 + 0.125 + 0.025, 4))}  100k in + 5k out + 50k cached on Claude Opus 5 -> ${_c100}")

# 101 the Fool's transcript addresses name ticker, quarter and year; old and
#     new slug styles both read.
_l101 = _ed._fool_links('<a href="/earnings/call-transcripts/2026/04/16/netflix-nflx-q1-2026-earnings-call-transcript/">'
                        '<a href="/earnings/call-transcripts/2026/04/22/isrg-q3-2024-earnings-transcript/">')
print(f"101 transcript addresses    {ok(sorted((l['ticker'], l['fq'], l['fy']) for l in _l101) == [('ISRG', 3, 2024), ('NFLX', 1, 2026)])}  "
      f"{[(l['ticker'], l['fq'], l['fy'], l['date']) for l in _l101]}")

# 102 the season opens with the banks in mid-January, April, July, October.
_ss102 = [_E.season_start(date(*x)).isoformat() for x in ((2026, 9, 28), (2026, 10, 13), (2026, 10, 9))]
print(f"102 reporting season        {ok(_ss102 == ['2026-07-08', '2026-10-08', '2026-07-08'])}  28/09, 13/10, 09/10 -> {_ss102}")

# 103 the calendar file: an after-the-close report at 16:05 New York on 28
#     October is 20:05 UTC.
_sv103 = _E.upcoming_mine
try:
    _E.upcoming_mine = lambda days=120: [{"sym": "MSFT", "name": "Microsoft", "date": "2026-10-28", "when": "amc",
                                          "eps_est": 4.72, "held": {"weight": 10.0, "value_eur": 2700}, "days": 30}]
    _ics103 = _E.ics()
finally:
    _E.upcoming_mine = _sv103
print(f"103 calendar file (.ics)    {ok('DTSTART:20261028T200500Z' in _ics103 and _ics103.count('BEGIN:VEVENT') == 1 and _ics103.endswith(chr(13) + chr(10)))}  "
      f"{[l for l in _ics103.splitlines() if l.startswith('DTSTART')]}")

# 104 Claude Code runs clean: nothing from a parent Claude session and no API
#     key reaches it (either would make it a borrowed or a billed run), and the
#     command is print mode, streamed, with this page's prompt and tools only.
_sv104 = dict(os.environ)
os.environ.update({"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_BASE_URL": "http://proxy", "CLAUDECODE": "1"})
try:
    _env104 = _ai._cc_env()
finally:
    os.environ.clear()
    os.environ.update(_sv104)
_sp104 = _ai.cc_path
try:
    _ai.cc_path = lambda: "claude.exe"
    _c104a = _ai.cc_command("Read")
    _c104b = _ai.cc_command("", resume="abc")
finally:
    _ai.cc_path = _sp104
print(f"104 Claude Code runs clean  {ok(not any(k.upper().startswith(('ANTHROPIC', 'CLAUDE')) for k in _env104) and 'PATH' in {k.upper() for k in _env104} and '-p' in _c104a and 'stream-json' in _c104a and '--system-prompt' in _c104a and _c104a[_c104a.index('--tools') + 1] == 'Read' and '--resume' in _c104b and '--system-prompt' not in _c104b and _c104a[_c104a.index('--setting-sources') + 1] == 'project' and '--strict-mcp-config' in _c104a and '--disable-slash-commands' in _c104a)}  "
      f"env without ANTHROPIC_*/CLAUDE*, flags {' '.join(x for x in _c104a[1:] if x.startswith('--'))[:120]}")

# 105 the event stream of a Claude Code run is read: the text as it is
#     written, the tool in use, and the result with its session id — tried
#     against a stand-in that speaks the same stream-json.
import json as _js105, subprocess as _sub105, tempfile as _tf105
_fake105 = r'''
import json, os, sys
sys.stdin.read()
def out(o): print(json.dumps(o), flush=True)
out({"type": "system", "subtype": "init", "model": "claude-test", "session_id": "s-105"})
out({"type": "stream_event", "event": {"type": "message_start"}})
out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "# Acme"}}})
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}})
out({"type": "result", "subtype": "success", "is_error": False, "session_id": "s-105", "num_turns": 2,
     "result": "# Acme\n\nclean" if "ANTHROPIC_API_KEY" not in os.environ else "LEAK",
     "total_cost_usd": 0.42, "usage": {"input_tokens": 36000, "output_tokens": 2500}})
'''
with _tf105.TemporaryDirectory() as _d105:
    _p105 = os.path.join(_d105, "fake_claude.py")
    open(_p105, "w").write(_fake105)
    os.environ["ANTHROPIC_API_KEY"] = "sk-test"
    try:
        _job105 = {"text": "", "phase": ""}
        _o105 = _ai.cc_stream([sys.executable, _p105], "material", _d105, _job105, timeout=60)
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
print(f"105 Claude Code stream read {ok(_o105['text'] == '# Acme' + chr(10) + chr(10) + 'clean' and _o105['session'] == 's-105' and _o105['model'] == 'claude-test' and _o105['api_usd'] == 0.42 and _job105['text'] == '# Acme' and _job105['phase'] == 'reading the slides')}  "
      f"result {_o105['text'][:20]!r}, session {_o105['session']}, ≈${_o105['api_usd']} at API prices, progress {_job105['text']!r} / {_job105['phase']!r}")

# ==========================================================================
# ALRT — the morning brief (build 21)
# ==========================================================================

from pf import morning as _mb
from datetime import datetime as _dt106

# 106 which morning a brief is for: today until 22:30, then the next weekday;
#     a weekend reads Monday's. Monday's brief looks back at Friday.
_tz = _mb.TZ
_bd = [_mb.brief_day(_dt106(2026, 9, 29, 7, 0, tzinfo=_tz)).isoformat(),
       _mb.brief_day(_dt106(2026, 9, 28, 23, 10, tzinfo=_tz)).isoformat(),
       _mb.brief_day(_dt106(2026, 10, 2, 22, 45, tzinfo=_tz)).isoformat(),
       _mb.brief_day(_dt106(2026, 10, 3, 9, 0, tzinfo=_tz)).isoformat(),
       _mb.prev_weekday(date(2026, 10, 5)).isoformat()]
print(f"106 which morning           {ok(_bd == ['2026-09-29', '2026-09-29', '2026-10-05', '2026-10-05', '2026-10-02'])}  "
      f"Tue 07:00 -> {_bd[0]}, Mon 23:10 -> {_bd[1]}, Fri 22:45 -> {_bd[2]}, Sat -> {_bd[3]}; Monday reviews {_bd[4]}")

# 107 the exchanges' own calendar: quarter-end, triple witching, Thanksgiving,
#     the bond market shut on Columbus Day while stocks trade.
_me = {d: [e["title"] for e in _mb.market_events(date.fromisoformat(d))]
       for d in ("2026-09-30", "2026-09-18", "2026-10-16", "2026-11-26", "2026-10-12", "2026-09-29")}
print(f"107 market calendar         {ok(any(t == 'Quarter-end' for t in _me['2026-09-30']) and any('triple' in t for t in _me['2026-09-18']) and any('Monthly options' in t for t in _me['2026-10-16']) and any('New York closed' in t for t in _me['2026-11-26']) and any('bond market closed' in t for t in _me['2026-10-12']) and not _me['2026-09-29'])}  "
      f"30/09 {_me['2026-09-30']}, 18/09 {len(_me['2026-09-18'])} event(s), 26/11 {_me['2026-11-26'][:1]}")

# 108 a day's move in euros: the share price and the currency, measured close
#     to close at the session the brief reviews. A dollar stock up 10% while
#     the euro rises from 1.10 to 1.20 dollars made less than 10% in euros,
#     and a stock whose exchange was shut that day made nothing.
_px108 = {"TST": [("2026-09-24", 90, 91, 89, 90.0, 90.0, 1e6), ("2026-09-25", 99, 101, 99, 100.0, 100.0, 1e6),
                  ("2026-09-28", 100, 111, 100, 110.0, 110.0, 4e6)],
          "SHUT.AS": [("2026-09-24", 10, 10, 10, 10.0, 10.0, 1e5), ("2026-09-25", 10, 10, 10, 10.0, 10.0, 1e5)],
          "EURUSD=X": [("2026-09-25", 0, 0, 0, 1.10, 1.10, 0), ("2026-09-28", 0, 0, 0, 1.20, 1.20, 0)]}
_dash108 = {"positions": [
    {"ticker": "TST", "name": "TEST CORP", "kind": "EQUITY", "currency": "USD", "qty": 10, "value_eur": 916.67, "weight": 90},
    {"ticker": "SHUT.AS", "name": "SHUT NV", "kind": "EQUITY", "currency": "EUR", "qty": 10, "value_eur": 100, "weight": 10}]}
_b108 = _mb._book(date(2026, 9, 29), date(2026, 9, 28), _dash108, _px108)
_t108 = next(x for x in _b108["positions"] if x["ticker"] == "TST")
_s108 = next(x for x in _b108["positions"] if x["ticker"] == "SHUT.AS")
_want = 10 * 110 / 1.20 - 10 * 100 / 1.10
print(f"108 a day in euros          {ok(abs(_t108['eur'] - _want) < 0.01 and abs(_t108['eur_price'] + _t108['eur_fx'] - _t108['eur']) < 0.02 and _t108['move'] == 10.0 and _t108['rvol'] and _t108['rvol'] > 3 and not _s108['traded'] and _s108['eur'] == 0 and _t108['high52'])}  "
      f"+10% in dollars = {_t108['eur']:+.2f} EUR (price {_t108['eur_price']:+.2f}, currency {_t108['eur_fx']:+.2f}); volume {_t108['rvol']}x; the shut exchange {_s108['eur']}")

# 109 minor central bankers are grouped: six Fed speakers in an afternoon is
#     one line, and a medium-impact release keeps its own.
_ev109 = [{"date": "2026-09-29", "time": t, "ts": None, "title": f"FOMC Member {n} Speaks", "currency": "USD", "rank": 1, "impact": "low"}
          for t, n in (("17:00", "Bowman"), ("18:40", "Barr"), ("19:00", "Goolsbee"))] + \
         [{"date": "2026-09-29", "time": "16:00", "ts": None, "title": "JOLTS Job Openings", "currency": "USD", "rank": 2,
           "impact": "medium", "forecast": "7.23M", "previous": "7.27M"},
          {"date": "2026-09-29", "time": "06:30", "ts": None, "title": "Cash Rate", "currency": "AUD", "rank": 3, "impact": "high"}]
_d109 = _mb._ff_day(date(2026, 9, 29), _ev109)
print(f"109 speakers grouped        {ok(len(_d109) == 2 and any(e['title'].startswith('Fed speakers: Bowman 17:00') and e['impact'] == 2 for e in _d109) and any(e['title'] == 'JOLTS Job Openings' for e in _d109))}  "
      f"{[e['title'][:40] for e in _d109]} — the Australian rate is not this book's")

# 110 the news, sorted: a headline naming a company you own is about it; one
#     that only tags it is not; clickbait is dropped; the world has its own list.
import time as _t110
_now110 = _t110.time()
_items110 = [
    {"headline": "Bloom Energy Sinks 8% as Sharp Run Unwinds", "url": "u1", "source": "Yahoo", "ts": _now110 - 3600, "tickers": ["BE"]},
    {"headline": "Is eBay Stock Priced Right Against Its Peers?", "url": "u2", "source": "Yahoo", "ts": _now110 - 3600, "tickers": ["AMZN"]},
    {"headline": "Trump says US will win Iran war 'very soon' - reuters.com", "url": "u3", "source": "Reuters", "ts": _now110 - 1800, "tickers": []},
    {"headline": "3 Stocks To Buy Before It's Too Late", "url": "u4", "source": "Motley", "ts": _now110 - 900, "tickers": ["BE"]},
]
_held110 = {"BE": {"name": "BLOOM ENERGY CORPORATION CLASS A", "weight": 2.7}, "AMZN": {"name": "AMAZON.COM INC", "weight": 5.0}}
from pf import news as _nw110
_saved110, _cg110 = _nw110.logged, _mb.store.cache_get
_nw110.logged = lambda since, until=None: _items110
_mb.store.cache_get = lambda k, *a, **kw: None if k == "news" else _cg110(k, *a, **kw)
try:
    _n110 = _mb._news(_now110 - 86400, _now110, _held110, {"BE": 2.7, "AMZN": 5.0})
finally:
    _nw110.logged, _mb.store.cache_get = _saved110, _cg110
_big110 = [x["headline"] for x in _n110["big"]]
_world110 = [x["headline"] for x in _n110["world"]]
_amzn110 = next((h for h in _n110["holdings"] if h["ticker"] == "AMZN"), None)
_ok110 = (_big110 == ["Bloom Energy Sinks 8% as Sharp Run Unwinds"]
          and _world110 == ["Trump says US will win Iran war 'very soon'"] and _amzn110 is None)
print(f"110 news sorted             {ok(_ok110)}  "
      f"holdings {_big110}, world {_world110}; the eBay story is not Amazon's, the listicle is gone")

# 111 why a position moved: a catalyst before a headline; a headline only if
#     it names the company and moves its way; clickbait never; the model's
#     own split when nothing else explains it.
_x111 = {"ticker": "BE", "name": "BLOOM ENERGY CORPORATION CLASS A", "move": -8.9}
_r111 = [
    _mb._reason(_x111, {"driver": "company", "catalysts": [{"type": "analyst", "text": "Jefferies downgrades to hold", "w": 3}]},
                [{"headline": "Bloom Energy Sinks 8%", "url": "a"}]),
    _mb._reason(_x111, {"driver": "company"}, [{"headline": "Stock Market Today: Dow Off Lows", "url": "b"},
                                               {"headline": "Bloom Energy Sinks 8% as Sharp Run Unwinds", "url": "c"}]),
    _mb._reason(_x111, {"driver": "mixed", "why": "-6.8pp beyond what the market and sector explain (-1.1σ). In the news: Is Bloom Energy Stock's Drop A Chance To Buy?"},
                [{"headline": "Is Bloom Energy Stock's Drop A Chance To Buy?", "url": "d"}]),
    _mb._reason({**_x111, "move": 1.7, "ticker": "NVDA", "name": "NVIDIA CORPORATION"}, None,
                [{"headline": "Stock Market Today: Dow Off Lows After Iran Report", "url": "e"}]),
]
print(f"111 why it moved            {ok(_r111[0]['kind'] == 'catalyst' and _r111[1]['kind'] == 'headline' and _r111[1]['url'] == 'c' and _r111[2]['kind'] == 'model' and 'In the news' not in _r111[2]['text'] and _r111[3] is None)}  "
      f"{[r and r['kind'] for r in _r111]}")

# 112 the short version reads right: "fell €350", not "fell −€350"; tickers
#     link to their pages; a futures roll is not quoted as a market move.
_b112 = {"date": "2026-09-29", "session": "2026-09-28", "spx_eur": -0.76,
         "book": {"eur": -350.0, "pct": -1.20, "eur_fx": 1.0, "n_traded": 2, "sectors": [],
                  "positions": [{"ticker": "BE", "traded": True, "eur": -68.0, "move": -8.9, "high52": False, "low52": False, "rvol": 1.0,
                                 "reason": {"kind": "headline", "text": "Bloom Energy Sinks 8%", "source": "Yahoo"}}]},
         "markets": [{"sym": "^GSPC", "name": "S&P 500", "pct": -0.77}, {"sym": "BZ=F", "name": "Brent crude", "pct": -5.5, "roll": True},
                     {"sym": "GC=F", "name": "Gold", "pct": -4.0}],
         "news": {"world": []}, "today": [], "earnings": {}, "overnight": {}, "watch": {}}
_s112 = " ".join(_mb.summary(_b112))
print(f"112 the short version       {ok('fell **€350**' in _s112 and '−€350' not in _s112 and '[BE](/t/BE)' in _s112 and 'Brent' not in _s112 and 'Gold −4.0%' in _s112)}  "
      f"{_s112[:150]}…")

# 113 a futures roll: Brent's front month rolled and printed -5.5% the day WTI
#     rose 1%. When the two crudes disagree that much the jump is flagged.
_px113 = {"BZ=F": [("2026-09-25", 0, 0, 0, 104.32, 104.32, 0), ("2026-09-28", 0, 0, 0, 98.56, 98.56, 0)],
          "CL=F": [("2026-09-25", 0, 0, 0, 92.41, 92.41, 0), ("2026-09-28", 0, 0, 0, 93.29, 93.29, 0)]}
_m113 = {m["sym"]: m for m in _mb._markets(date(2026, 9, 29), date(2026, 9, 28), _px113)}
print(f"113 a futures roll          {ok(_m113['BZ=F'].get('roll') and not _m113['CL=F'].get('roll'))}  "
      f"Brent {_m113['BZ=F']['pct']:+.2f}% flagged as a roll; WTI {_m113['CL=F']['pct']:+.2f}% kept")

# 114 one registry: the decision journal is gone, the module cards carry no
#     descriptions, ALRT is live and has its own page, on every screen.
_term = open(os.path.join(HERE, "backend", "static", "term.js"), encoding="utf-8").read()
_home = open(os.path.join(HERE, "backend", "static", "index.html"), encoding="utf-8").read()
_modh = open(os.path.join(HERE, "backend", "static", "module.html"), encoding="utf-8").read()
import re as _re114
_mods = _term[_term.index("const MODULES"):_term.index("];", _term.index("const MODULES"))]
import portfolio_app as _pa114
_ok114 = ("JRN" not in _mods and "blurb" not in _term and "blurb" not in _home and "JRN" not in _modh
          and 'class="bl"' not in _home and bool(_re114.search(r'code: "ALRT".*state: "live"', _mods))
          and _pa114.BUILT_MODULES.get("ALRT") == "brief.html")
print(f"114 one module registry     {ok(_ok114)}  "
      f"{len(_re114.findall('code:', _mods))} modules, no JRN, no descriptions; ALRT -> {_pa114.BUILT_MODULES.get('ALRT')}")

# 115 the brief is kept: stored per morning, read back whole, listed for the
#     archive, and the home screen's line comes from it.
_b115 = {"date": "2099-12-31", "session": "2099-12-30", "built_ts": 946972800.0, "built_at": "2099-12-31T06:30:00+01:00",
         "summary": ["Your book rose **€1**.", "Today: nothing."], "book": {"eur": 1.0, "pct": 0.01}, "today": []}
_mb.save(_b115, "morning")
_r115 = _mb.stored("2099-12-31")
_l115 = [x["d"] for x in _mb.dates(1000)]
with store.conn() as _c115:
    _c115.execute("DELETE FROM brief_day WHERE d='2099-12-31'")
print(f"115 briefs are kept         {ok(_r115 and _r115['summary'][0] == 'Your book rose **€1**.' and '2099-12-31' in _l115)}  "
      f"stored, read back, listed among {len(_l115)} kept (the test row is removed again)")

# 116 Windows does not get in the way unseen. The Microsoft Store build of the
#     Claude app keeps Claude Code in its package folder, which a server started
#     from Explorer must look in; and a library Windows refuses to load (Smart
#     App Control blocked pandas 3.0.6) is named, not silently missing.
#     Since 2.1.293 the app keeps each version one folder deeper, beside a
#     .verified marker; a copy still downloading (no marker) and a version
#     folder with no claude.exe yet must not be picked.
#     Since 20 October 2026 the app keeps its data in %LOCALAPPDATA%\Claude-Data
#     and leaves only a marker behind: a newer copy there is the one taken.
import shutil as _sh116, tempfile as _tf116
from pf import api as _api116
with _tf116.TemporaryDirectory() as _d116:
    _pkg = os.path.join(_d116, "Local", "Packages", "Claude_test", "LocalCache", "Roaming", "Claude", "claude-code")
    os.makedirs(os.path.join(_pkg, "2.1.9"))
    open(os.path.join(_pkg, "2.1.9", "claude.exe"), "w").close()
    for _h, _mark in (("d2e2", True), ("ffff", False)):
        os.makedirs(os.path.join(_pkg, "2.1.10", _h))
        open(os.path.join(_pkg, "2.1.10", _h, "claude.exe"), "w").close()
        if _mark:
            open(os.path.join(_pkg, "2.1.10", _h, ".verified"), "w").close()
    os.makedirs(os.path.join(_pkg, "2.1.11", "0a0a"))
    os.makedirs(os.path.join(_d116, "Roaming"))
    _env116 = {k: os.environ.get(k) for k in ("APPDATA", "LOCALAPPDATA")}
    _which116, _gs116 = _sh116.which, _ai.store.get_setting
    os.environ.update(APPDATA=os.path.join(_d116, "Roaming"), LOCALAPPDATA=os.path.join(_d116, "Local"))
    _sh116.which = lambda *a, **k: None
    _ai.store.get_setting = lambda k, d=None: None if k == "claude_code_path" else _gs116(k, d)
    try:
        _p116 = _ai.cc_path()
        for _x116 in (os.path.join(_d116, "Roaming", "Claude"), os.path.dirname(_pkg)):
            os.makedirs(_x116, exist_ok=True)
            open(os.path.join(_x116, ".user-data-left.claude_test"), "w").close()
        _new116 = os.path.join(_d116, "Local", "Claude-Data", "claude_test", "claude-code", "2.1.12", "c17a")
        os.makedirs(_new116)
        open(os.path.join(_new116, "claude.exe"), "w").close()
        open(os.path.join(_new116, ".verified"), "w").close()
        _q116 = _ai.cc_path()
    finally:
        _sh116.which, _ai.store.get_setting = _which116, _gs116
        for _k, _v in _env116.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
_blk116 = _api116.blocked_imports()
try:
    import pandas as _pd116
    _pd_ok = True
except Exception:
    _pd_ok = False
print(f"116 Windows in the way      {ok(_p116 and _p116.endswith(os.path.join('2.1.10', 'd2e2', 'claude.exe')) and 'Packages' in _p116 and _pd_ok
           and _q116 and _q116.endswith(os.path.join('Claude-Data', 'claude_test', 'claude-code', '2.1.12', 'c17a', 'claude.exe')) and not any(b['module'] == 'pandas' for b in _blk116))}  "
      f"Claude Code found in the Store app's package folder ({os.path.relpath(_p116, _pkg) if _p116 else 'none'}: the newest version, its verified copy; then Claude-Data's, once the app has moved there); "
      f"blocked libraries: {[b['module'] for b in _blk116] or 'none'}; pandas {_pd116.__version__ if _pd_ok else 'BLOCKED'}")

# 117 a session Yahoo dropped is put back. Once Tuesday's session opens,
#     Yahoo's daily history for an Amsterdam line can skip Monday; the brief
#     then read the holding as shut on Monday. The gap is filled when the
#     exchange was open that day — from a close stored after the session
#     before a price stored during it — and left alone on a holiday there.
import yfinance as _yf117
_bars117 = lambda: [("2026-09-24", 0, 0, 0, 31.5, 31.5, 1), ("2026-09-25", 0, 0, 0, 31.8, 31.8, 1),
                    ("2026-09-29", 0, 0, 0, 32.0, 32.0, 1)]
_px117 = {"INGA.AS": _bars117(), "NN.AS": _bars117(),
          "SHUT.AS": [("2026-12-23", 0, 0, 0, 10.0, 10.0, 1), ("2026-12-24", 0, 0, 0, 10.0, 10.0, 1),
                      ("2026-12-28", 0, 0, 0, 10.2, 10.2, 1)]}
_st117 = {("INGA.AS", "2026-09-28"): (31.88, "2026-09-28T21:30:00"),     # 23:30 in Amsterdam: after the close
          ("NN.AS", "2026-09-28"): (79.10, "2026-09-28T10:00:00"),       # noon: mid-session
          ("SHUT.AS", "2026-12-25"): (99.0, "2026-12-26T09:00:00")}
_sc117, _tk117 = _mb.stored_close, _yf117.Ticker
_mb.stored_close = lambda s, d: _st117.get((s, d), (None, None))
_yf117.Ticker = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))
try:
    _f117 = _mb.fill_gaps(_px117, ["INGA.AS", "NN.AS"], date(2026, 9, 28), date(2026, 9, 29))
    _h117 = _mb.fill_gaps(_px117, ["SHUT.AS"], date(2026, 12, 25), date(2026, 12, 28))
finally:
    _mb.stored_close, _yf117.Ticker = _sc117, _tk117
_b117 = _mb._session(_px117["INGA.AS"], "2026-09-29")
_ok117 = (_f117.get("INGA.AS") == "the close stored after the session" and _b117[0][0] == "2026-09-28"
          and _b117[0][4] == 31.88 and _b117[1][0] == "2026-09-25"
          and _f117.get("NN.AS") == "a price stored during the session" and not _h117)
print(f"117 a dropped session       {ok(_ok117)}  "
      f"INGA.AS Monday put back at {_b117[0][4]} ({_f117.get('INGA.AS')}); NN.AS from {_f117.get('NN.AS')} "
      f"once hourly bars fail; Christmas in Amsterdam left alone")

# 118 the same gap in the day change. Measured against the last daily bar
#     Yahoo still lists (Friday's), Tuesday's day change for a European line
#     carried Monday's move as well. A close stored for Monday is used instead.
import pandas as _pd118
from pf import pricing as _pr118
_t118 = "GAPTEST.AS"
store.put_prices(_t118, [("2026-09-28", 31.88)], None, "verify")
_fake118 = _pd118.DataFrame({"Close": [31.515, 31.795, 31.955]},
                            index=_pd118.to_datetime(["2026-09-24", "2026-09-25", "2026-09-29"]))
_yf118, _fd118 = _pr118._yf, _pr118.feed_down
_pr118._yf = lambda: type("Y", (), {"download": staticmethod(lambda *a, **k: _fake118)})
_pr118.feed_down = lambda: False
try:
    # Tuesday at noon, Amsterdam open. Since build 33 "the last close" depends
    # on the time (pf/sessions.py), so the check says which time it means.
    from datetime import datetime as _dt118
    from zoneinfo import ZoneInfo as _zi118
    _q118 = _pr118.get_quotes([_t118], fresh=True, now=_dt118(2026, 9, 29, 12, 0, tzinfo=_zi118("Europe/Amsterdam"))).get(_t118) or {}
finally:
    _pr118._yf, _pr118.feed_down = _yf118, _fd118
    with store.conn() as _c118:
        _c118.execute("DELETE FROM price WHERE ticker=?", (_t118,))
_d118 = (_q118.get("price", 0) / _q118["prev_close"] - 1) * 100 if _q118.get("prev_close") else None
print(f"118 day change across a gap {ok(_q118.get('prev_close') == 31.88 and _q118.get('asof') == '2026-09-29')}  "
      f"previous close {_q118.get('prev_close')} (Monday, stored) not 31.795 (Friday): day "
      f"{_d118:+.2f}% instead of {(31.955 / 31.795 - 1) * 100:+.2f}%" if _d118 is not None else "no quote")

# ==========================================================================
# build 22: the rate path, risk by holding, rotation, options, the supply chain
# ==========================================================================

# 119 two markets that close at different hours. A New York share closes at
#     22:00 Amsterdam and a Frankfurt tracker at 17:30, so part of each day's
#     move lands in the tracker's NEXT day. Daily returns then show a beta far
#     below the truth; five-day windows contain both halves.
import random as _rnd119
from pf import risk as _risk119
_rnd119.seed(7)
_days119 = [(_date(2024, 1, 1) + _td(days=i)).isoformat() for i in range(420)]
_m119 = [_rnd119.gauss(0, 0.011) for _ in _days119]
_bench119, _asset119, _b, _a = {}, {}, 100.0, 100.0
for _i, _d in enumerate(_days119):
    _late = _m119[_i - 1] if _i else 0.0
    _b *= 1 + _m119[_i]
    # true beta 1.2, but only 40% of the market's day arrives the same day
    _a *= 1 + 1.2 * (0.4 * _m119[_i] + 0.6 * _late) + _rnd119.gauss(0, 0.004)
    _bench119[_d], _asset119[_d] = _b, _a
_d1 = _risk119.ols(*_risk119.price_windows(_asset119, _bench119, 1))
_d5 = _risk119.ols(*_risk119.price_windows(_asset119, _bench119, _risk119.HORIZON))
print(f"119 closing-time mismatch   {ok(_d1 and _d5 and _d1['beta'] < 0.75 and 0.95 < _d5['beta'] < 1.3 and _risk119.HORIZON == 5)}  "
      f"true beta 1.20: daily returns say {_d1['beta']:.2f}, five-day windows {_d5['beta']:.2f}")

# 120 every holding's share of the risk: the shares add to 100%, a holding
#     that moves against the rest comes out below zero, and N holdings that
#     have nothing to do with each other are N bets.
import numpy as _np120
from pf import riskmap as _rm120
_S120 = _np120.array([[0.04, 0.018, -0.006], [0.018, 0.09, -0.004], [-0.006, -0.004, 0.01]])
_c120 = _rm120.contributions(_S120, [0.5, 0.3, 0.2])
_I120 = _rm120.contributions(_np120.eye(4) * 0.04, [0.25] * 4)
_bets120 = (0.2 / (_I120["var"] ** 0.5)) ** 2          # (average holding's swing / book's swing) squared
print(f"120 shares of risk          {ok(abs(float(_c120['share'].sum()) - 1) < 1e-9 and _c120['share'][2] < 0 and abs(_bets120 - 4) < 1e-9)}  "
      f"shares {[round(float(x) * 100, 1) for x in _c120['share']]} add to {float(_c120['share'].sum()) * 100:.0f}%; "
      f"four unrelated holdings = {_bets120:.0f} bets")

# 121 holdings that move together are grouped; one that does not stays alone.
_C121 = _np120.array([[1, .8, .7, .05], [.8, 1, .75, .0], [.7, .75, 1, .1], [.05, .0, .1, 1]])
_g121 = sorted(sorted(g) for g in _rm120.clusters(_C121))
print(f"121 what moves together     {ok(_g121 == [[0, 1, 2], [3]])}  {_g121}")

# 122 the rotation graph: a share that outruns its benchmark and then falls
#     behind goes right of 100 and comes back left of it; each quadrant has
#     its name; and a benchmark against itself has no reading at all.
from pf import rotation as _rot122
_rnd119.seed(3)
_b122 = [100 * 1.001 ** i for i in range(160)]
_p122 = []
for _i in range(160):
    _rel = 1.004 ** min(_i, 100) * 0.994 ** max(0, _i - 100)
    _p122.append(_b122[_i] * _rel * (1 + _rnd119.gauss(0, 0.004)))
_x122, _y122 = _rot122.rotate(_p122, _b122)
_self122 = _rot122.rotate(_b122, _b122)[0]
_q122 = [_rot122.quadrant(101, 101), _rot122.quadrant(101, 99), _rot122.quadrant(99, 99), _rot122.quadrant(99, 101)]
print(f"122 rotation                {ok(_x122[99] > 100 and _x122[-1] < 100 and all(v is None for v in _self122) and _q122 == ['leading', 'weakening', 'lagging', 'improving'])}  "
      f"trend {_x122[99]:.1f} while outrunning, {_x122[-1]:.1f} after falling behind; quadrants {_q122}")

# 123 an option's price and its volatility are two readings of one number:
#     price at 32%, solve back, get 32%. And a price below what the option is
#     worth exercised today has no volatility; it is refused, not guessed.
from pf import options as _op123
_F, _T = 230.0, 30 / 365
_rt123 = [(_k, _c, _op123.implied(_op123.black(_F, _k, _T, 0.32, _c), _F, _k, _T, _c))
          for _k, _c in ((200.0, False), (230.0, True), (260.0, True))]
_par123 = _op123.black(_F, 240.0, _T, 0.32, True) - _op123.black(_F, 240.0, _T, 0.32, False)
print(f"123 Black-76 round trip     {ok(all(v is not None and abs(v - 0.32) < 1e-6 for _, _, v in _rt123) and abs(_par123 - (_F - 240.0)) < 1e-9 and _op123.implied(9.0, _F, 220.0, _T, True) is None)}  "
      f"32% in, {[round(v * 100, 2) for _, _, v in _rt123]}% out; call minus put = forward minus strike")

# 124 the forward is read off the quotes themselves (strike + call - put), so
#     a dividend the terminal does not know about is already in it.
_df124, _F124 = 0.99, 101.5
def _row124(k, call):
    m = _df124 * _op123.black(_F124, k, 0.25, 0.3, call)
    return [k, round(m - 0.01, 4), round(m + 0.01, 4), m, 0, 10, 100]
_e124 = {"calls": [_row124(k, True) for k in range(80, 125, 5)], "puts": [_row124(k, False) for k in range(80, 125, 5)]}
_f124 = _op123.parity_forward(_e124, 100.0, _df124, 1.0)
print(f"124 forward from parity     {ok(abs(_f124 - _F124) < 0.05)}  spot 100, the quotes imply {_f124:.2f} (true {_F124})")

# 125 the range the prices imply: with one volatility at every strike the
#     distribution is the textbook one, median just below the forward and
#     two outcomes in three inside one standard deviation.
import math as _m125
_s125 = _op123.fit_smile([(k / 20, 0.40, 1.0) for k in range(-10, 11)], 0.25)
_q125 = _op123.distribution(_s125, 100.0)["q"]
_med125, _sd125 = 100 * _m125.exp(-0.4 ** 2 * 0.25 / 2), 0.4 * 0.5
print(f"125 implied distribution    {ok(abs(_q125[2] - _med125) < 0.6 and abs(_q125[1] - _med125 * _m125.exp(-_sd125)) < 1.2 and abs(_q125[3] - _med125 * _m125.exp(_sd125)) < 1.2 and _q125 == sorted(_q125))}  "
      f"40% for three months: 16% / 50% / 84% at {_q125[1]:.1f} / {_q125[2]:.1f} / {_q125[3]:.1f} "
      f"(theory {_med125 * _m125.exp(-_sd125):.1f} / {_med125:.1f} / {_med125 * _m125.exp(_sd125):.1f})")

# 126 the supply chain is read from sentences, so the sentences have to be
#     cut right: "Inc." does not end one, a page break does not split one, a
#     table of "Customer A | 49" stays whole, and a product name is not a
#     company unless the sentence says a list of companies is coming.
from pf import supply as _sp126
_txt126 = """We utilize a fabless model.
In fiscal year 2026, we exclusively used Taiwan Semiconductor Manufacturing Company Limited (TSMC) for wafer production and test at Amkor Technology Inc. and others in Asia.
Our principal competitors with respect to our products include Broadcom Ltd. (Broadcom),

14

Table of Contents

Marvell and Astera, as well as various cable suppliers that are much larger than we are today.
Year Ended
Revenue | May 2, 2026 | May 3, 2025
Customer A | 49 | 67
Customer B | 32 | *
We currently outsource all of our manufacturing to TSMC and rely on the Bluebird design for the 3nm node from them.
Sales to our top 10 customers accounted for approximately 90% of our total revenue."""
_ps126 = _sp126.passages_of(_txt126, {"credo"}, _sp126.name_rx(["Micron", "Nvidia"]))
_t126 = [x["text"] for x in _ps126]
_ok126 = (any("Taiwan Semiconductor" in t and "Amkor" in t for t in _t126)
          and any("Broadcom" in t and "Marvell" in t and "Astera" in t for t in _t126)            # joined across the page break
          and any(t.count("Customer") == 2 and "49" in t and "Revenue" in t for t in _t126)       # the table, whole
          and any("outsource all of our manufacturing to TSMC" in t for t in _t126)               # the alias, once defined
          and _sp126.org_names("We rely on the Bluebird design and on Acme Devices Inc. for it.", set()) == ["Acme Devices Inc"]
          and _sp126.name_rx(["Micron", "Nvidia"]).search("fast micron-scale LEDs") is None
          and _sp126.name_rx(["Micron", "Nvidia"]).search("built on NVIDIA GPUs") is not None)
print(f"126 sentences from a filing {ok(_ok126)}  {len(_ps126)} passages from a twelve-line report: "
      f"the foundry, the rivals across a page break, the customer table in one piece")

# 127 a reader cannot put words in a filing's mouth. Whatever comes back, a
#     line is kept only if its passage exists and names the company, a share
#     only if the passage states that figure, and the quote is the passage.
_M127 = {"docs": [{"form": "10-K", "filed": "2026-02-17", "url": "https://example.test/k.htm"}],
         "items": [{"n": 1, "d": 0, "text": "We are primarily reliant upon our merchant silicon vendor, Broadcom, for our switching chips."},
                   {"n": 2, "d": 0, "text": "Sales to Microsoft represented 26% of our total revenue."},
                   {"n": 3, "d": 0, "text": "Join Arista and Anthropic for a webinar on security."}]}
_N127 = {"AVGO": ["Broadcom"], "MSFT": ["Microsoft"], "NVDA": ["Nvidia"], "~anthropic": ["Anthropic"]}
_a127 = _sp126.accept("ANET", _M127, {"links": [
    {"p": 1, "other": "Broadcom", "key": "AVGO", "kind": "supplier", "what": "Switching chips"},
    {"p": 2, "other": "Microsoft", "key": "MSFT", "kind": "customer", "what": "a customer", "pct": 26, "basis": "revenue"},
    {"p": 2, "other": "Microsoft", "key": "MSFT", "kind": "customer", "what": "inflated", "pct": 40, "basis": "revenue"},
    {"p": 1, "other": "Nvidia", "key": "NVDA", "kind": "supplier", "what": "GPUs"},              # not in the passage
    {"p": 9, "other": "Broadcom", "key": "AVGO", "kind": "supplier", "what": "no such passage"},
    {"p": 3, "other": "Anthropic", "key": "~anthropic", "kind": "partner", "what": "a webinar"}]}, _N127)
_l127 = {(l["a"], l["b"], l["kind"], l["pct"]) for l in _a127["links"]}
print(f"127 a reader cannot invent  {ok(_l127 == {('AVGO', 'ANET', 'supply', None), ('ANET', 'MSFT', 'supply', 26.0), ('ANET', 'MSFT', 'supply', None)} and all(l['quote'] in [i['text'] for i in _M127['items']] for l in _a127['links']) and len(_a127['dropped']) == 3 and _a127['links'][0]['what'] == 'switching chips')}  "
      f"{len(_a127['links'])} of 6 lines kept; dropped: {sorted(d['why'] for d in _a127['dropped'])}; "
      f"a 40% the passage does not state is kept without its figure")

# 128 the map that ships is whole: every link points at a document, every
#     company in it has a name, a share says what it is a share of, and no
#     quote is empty. The links were read by Claude once; this is the check
#     that the file was not damaged afterwards.
_seed128 = _sp126.seed()
_L128, _D128, _N128 = _seed128["links"], _seed128["docs"], _seed128["names"]
_bad128 = [l for l in _L128 if l["kind"] not in _sp126.KINDS or l.get("doc") not in _D128 or not (l.get("quote") or "").strip()
           or l["a"] == l["b"] or ((l.get("pct") is not None) != bool(l.get("basis")))
           or (l.get("basis") and l["basis"] not in _sp126.BASIS_WORDS)]
_nameless128 = sorted({k for l in _L128 for k in (l["a"], l["b"]) if k not in _N128})
print(f"128 the shipped map         {ok(len(_L128) > 400 and not _bad128 and not _nameless128)}  "
      f"{len(_L128)} sentences from {len(_D128)} documents, {len(_N128)} names; {len(_bad128)} damaged, "
      f"{len(_nameless128)} without a name" + (f" {_nameless128[:6]}" if _nameless128 else ""))

# 129 the chain is drawn left to right by who sells to whom: in A -> B -> C a
#     customer sits one step right of its supplier, and where two companies
#     sell to each other the heavier link decides which is drawn first.
_lv129 = _sp126.levels(["A", "B", "C"], [("A", "B", 1.0), ("B", "C", 1.0)])
_cy129 = _sp126.levels(["X", "Y"], [("X", "Y", 3.0), ("Y", "X", 1.0)])
print(f"129 upstream to downstream  {ok(abs(_lv129['B'] - _lv129['A'] - 1) < 1e-6 and abs(_lv129['C'] - _lv129['B'] - 1) < 1e-6 and _lv129['A'] == 0 and _cy129['X'] < _cy129['Y'])}  "
      f"A, B, C at {_lv129['A']:.1f}, {_lv129['B']:.1f}, {_lv129['C']:.1f}; a two-way pair at {_cy129['X']:.2f} and {_cy129['Y']:.2f}")

# 130 every page added in build 22 asks only for routes this server has, the
#     way check 54 does for GEO. Template URLs are matched by their fixed part.
_pat130 = _re.compile(r'fetch\(\s*[`"](/api/pf/[A-Za-z_/\-]+)')
_fixed130 = {_re.sub(r"\{[^}]+\}.*", "", r) for r in _routes}
_miss130 = {}
for _f in ("supply.js", "options.js", "rrg.js", "bonds.js"):
    _src = open(os.path.join(HERE, "backend", "static", _f), encoding="utf-8").read()
    for _u in set(_pat130.findall(_src)):
        _u = _u.rstrip("/")
        if not any(_u == r.rstrip("/") or _u.startswith(r) or r.startswith(_u + "/") for r in _fixed130 if r.startswith("/api/pf/")):
            _miss130.setdefault(_f, []).append(_u)
print(f"130 new pages call real routes {ok(not _miss130)}  supply, options, rotation and bonds pages"
      + (f" — unknown: {_miss130}" if _miss130 else ", every endpoint registered"))

# 131 the command line reaches them: OPT, SPLC and RRG are modules with a
#     page behind them, a code takes a ticker either way round, and the pages
#     for one company are served for any symbol.
_ok131 = (all(c in _live and _files.get(c) for c in ("OPT", "SPLC", "RRG"))
          and all(r in _routes for r in ("/m/OPT/{symbol}", "/m/SPLC/{symbol}"))
          and all(w in _js for w in ('"OPT|OMON|OVDV|SKEW|IVOL"', '"SPLC|CHAIN|SUPPLY"', '"/m/MKT/bonds#s-path"')))
print(f"131 the shell knows them    {ok(_ok131)}  OPT, SPLC, RRG live with pages; OPT NVDA and NVDA OPT both open the page")

# ==========================================================================
# the ledger's own money (checks 4, 21 and 37 looked into, 1 October 2026)
# ==========================================================================

# 132 a sale can cost more than it brings in. A knocked-out turbo sold for 4
#     cents with a 50 cent commission has a total of -0.46, and the loss is the
#     whole stake plus that. Taking the size of the total booked 46 cents as
#     received, so realised P&L read 92 cents too high and check 4's identity
#     was out by the same.
from pf.degiro import Txn as _Txn132
from datetime import datetime as _dt132
_L132 = build_ledger([
    _Txn132(kind="BUY", trade_dt=_dt132(2026, 1, 28, 10, 0), isin="X132", quantity=44, price=8.585,
            currency="EUR", fee_eur=-0.5, total_eur=-378.24),
    _Txn132(kind="SELL", trade_dt=_dt132(2026, 6, 12, 10, 0), isin="X132", quantity=-44, price=0.001,
            currency="EUR", fee_eur=-0.5, total_eur=-0.46)])
_d132 = _L132.disposals[0]
print(f"132 a sale below its own fee {ok(abs(_L132.realised_eur + 378.70) < 0.005 and abs(_d132.proceeds_eur + 0.46) < 0.005 and abs(_L132.invested_eur + _L132.realised_eur) < 0.005 and not _L132.positions)}  "
      f"44 bought for 378.24, sold for -0.46 after costs: realised {_L132.realised_eur:.2f}, proceeds {_d132.proceeds_eur:.2f} "
      f"(price leg {_d132.pnl_price_eur:.2f} - fees {_d132.fees_eur:.2f})")

# 133 a trade entered by hand brings its own money. DEGIRO's cash balance never
#     paid for it, so what a hand-entered buy cost is capital paid in, and what a
#     hand-entered sale brought in is capital taken out — neither is profit.
#     Tried on a copy of the ledger. "profit - unrealised - realised" equals
#     cost + cash - capital - realised, which no price can move, so it has to
#     sit exactly where it was after a buy (with a fee) and after a sale, and
#     the contribution has to reconcile as before. Counted the old way, typing
#     in a position added its whole cost to profit.
if T.get("basis") != "account":
    print("133 hand-entered money      SKIP  no account statement imported, so returns are on cost basis")
else:
    import sqlite3 as _sq133
    from pf import manual as _mn133
    _dir133 = tempfile.mkdtemp(prefix="pf_capitalcheck_")
    _tmp133 = os.path.join(_dir133, "ledger.db")
    # SQLite's own backup, not a file copy: the app may be writing to the ledger.
    _a133 = _sq133.connect(os.environ["PF_DB"]); _b133 = _sq133.connect(_tmp133)
    _a133.backup(_b133); _b133.close(); _a133.close()
    _orig133 = os.environ["PF_DB"]
    os.environ["PF_DB"] = _tmp133; store.DB_PATH = _tmp133
    def _k133():
        _dd = service.dashboard("SP500", light=True); _tt = _dd["totals"]
        return {"k": _tt["profit_eur"] - _tt["unreal_eur"] - _tt["realised_eur"], "cap": _tt["capital_eur"],
                "cash": _tt["cash_eur"], "un": (_dd.get("contribution") or {}).get("unexplained_eur") or 0.0,
                "friction": (_dd.get("costs") or {}).get("friction_eur") or 0.0}
    try:
        _i133 = next(iter(L.positions)); _day133 = date.today().isoformat()
        _k0 = _k133()
        _mn133.add_transaction(kind="BUY", isin=_i133, trade_date=_day133, quantity=2, price=100, currency="EUR", fee_eur=1.5)
        _k1 = _k133()
        _mn133.add_transaction(kind="SELL", isin=_i133, trade_date=_day133, quantity=1, price=120, currency="EUR", fee_eur=1)
        _k2 = _k133()
    finally:
        os.environ["PF_DB"] = _orig133; store.DB_PATH = _orig133
        shutil.rmtree(_dir133, ignore_errors=True)
    _ok133 = (abs(_k1["k"] - _k0["k"]) < 0.05 and abs(_k2["k"] - _k0["k"]) < 0.05
              and abs(_k1["cap"] - _k0["cap"] - 201.5) < 0.02 and abs(_k2["cap"] - _k1["cap"] + 119.0) < 0.02
              and _k1["cash"] == _k0["cash"] == _k2["cash"]
              and abs(_k1["un"] - _k0["un"]) < 0.5 and abs(_k2["un"] - _k0["un"]) < 0.5
              and abs(_k2["friction"] - _k0["friction"] - 2.5) < 0.02)
    print(f"133 hand-entered money      {ok(_ok133)}  a buy of 201.50 then a sale for 119.00, typed in: capital "
          f"{_k1['cap'] - _k0['cap']:+.2f} then {_k2['cap'] - _k1['cap']:+.2f}, cash unmoved, profit beyond the trades' own P&L "
          f"{_k1['k'] - _k0['k']:+.2f} and {_k2['k'] - _k0['k']:+.2f}, unexplained {_k0['un']:+.2f} -> {_k2['un']:+.2f}, "
          f"their fees in friction {_k2['friction'] - _k0['friction']:+.2f}")

# 134 capital in is the deposits less the withdrawals plus what the hand-entered
#     trades in THIS ledger cost, and profit is net worth less that capital.
if T.get("basis") != "account":
    print("134 capital in              SKIP  no account statement imported")
else:
    _hand134 = [t for t in trades if t.source == "manual" and t.total_eur is not None]
    _out134 = sum(-t.total_eur for t in _hand134)
    print(f"134 capital in              {ok(abs(T['outside_eur'] - _out134) < 0.02 and abs(T['capital_eur'] - (T['net_deposits_eur'] + _out134)) < 0.02 and abs(T['profit_eur'] - (T['net_worth_eur'] - T['capital_eur'])) < 0.02)}  "
          f"deposits {T['net_deposits_eur']:,.2f} + {len(_hand134)} hand-entered trade(s) {_out134:,.2f} = {T['capital_eur']:,.2f}; "
          f"net worth {T['net_worth_eur']:,.2f} less that = profit {T['profit_eur']:,.2f}")

# 135 the curve has no weekend days, so money dated on one goes on the Monday
#     after. DEGIRO never trades on a weekend, but a deposit can land on a
#     Saturday and a trade typed in by hand can carry any date. Keyed to a day
#     the curve never visits, a flow was never neutralised and a position never
#     arrived: EUR 40 deposited on a Saturday read as a 40% Monday.
_tr135 = [
    _Txn132(kind="BUY", trade_dt=_dt132(2026, 1, 2, 10, 0), isin="X135", quantity=10, price=10.0,
            currency="EUR", total_eur=-100.0),
    _Txn132(kind="BUY", trade_dt=_dt132(2026, 1, 10, 0, 0), isin="X135", quantity=5, price=10.0,
            currency="EUR", total_eur=-50.0, source="manual")]
_cv135 = service.build_curve(
    _tr135, {"X135": {"currency": "EUR", "kind": "EQUITY"}}, {}, {}, date(2026, 1, 2), date(2026, 1, 16), "none",
    external_flows=[(date(2026, 1, 2), 100.0), (date(2026, 1, 3), 40.0), (date(2026, 1, 10), 50.0)],
    cash_series={"2026-01-02": 0.0, "2026-01-03": 40.0}, cash_eur=40.0)
_s135 = {x["d"]: x for x in _cv135["series"]}
_twr135 = service.window_twr(_cv135["series"], "2026-01-02")[0]
print(f"135 weekend money           {ok(_s135['2026-01-05']['f'] == 40.0 and _s135['2026-01-12']['f'] == 50.0 and _s135['2026-01-12']['v'] == 190.0 and _twr135 == 0.0)}  "
      f"a Saturday deposit of 40 and a Saturday hand-entered buy of 50: flows on the Mondays "
      f"{_s135['2026-01-05']['f']:.0f} and {_s135['2026-01-12']['f']:.0f}, value {_s135['2026-01-12']['v']:.0f}, return {_twr135}%")

# ==========================================================================
# year by year, and one option worked out (build 23)
# ==========================================================================

# 136 DEGIRO's Saldo column is a running sum of the rows, except on the
#     cash-sweep rows. The statement prints a balance on those that the
#     account was never in, and the rows after them carry on from the balance
#     before. Checked here on your own statement, row by row and per currency:
#     if a new kind of row ever breaks the sum, the daily cash line is wrong
#     from that day on, and this says where.
_rows136 = sorted((t for t in txns if t.source == "degiro:account" and t.balance is not None and t.balance_ccy),
                  key=lambda t: (t.trade_dt, -(t.seq if t.seq is not None else 0)))
if not _rows136:
    print("136 the balance adds up     SKIP  no account statement imported")
else:
    _prev136, _breaks136, _n136 = {}, [], 0
    for _x136 in _rows136:
        if service._sweep(_x136) or _x136.currency != _x136.balance_ccy:
            continue
        _n136 += 1
        if _x136.balance_ccy in _prev136 and abs((_x136.balance - _prev136[_x136.balance_ccy]) - (_x136.local_value or 0.0)) > 0.015:
            _breaks136.append(_x136)
        _prev136[_x136.balance_ccy] = _x136.balance
    _sw136 = sum(1 for _x136 in _rows136 if service._sweep(_x136))
    print(f"136 the balance adds up     {ok(not _breaks136)}  {_n136} rows are one running balance once {_sw136} cash-sweep rows are left out; "
          f"{len(_breaks136)} breaks" + (f", first on {_breaks136[0].trade_dt:%d/%m/%Y %H:%M} ({_breaks136[0].note[:30]})" if _breaks136 else ""))

# 137 the day's cash is the newest state of the day. Three ways it was not: a
#     purchase in dollars writes four rows in one instant, newest first, and
#     the oldest of them (dollars owed, before the conversion) was read as the
#     day's balance; a sweep row as the last row of a day put the day out by
#     the whole transfer; and a deposit was dated on the row that records it,
#     a day or two after the money was reserved and already in the balance.
def _row137(kind, when, amt, ccy, bal, seq, note):
    return _Txn132(kind=kind, trade_dt=when, local_value=amt, currency=ccy, balance=bal, balance_ccy=ccy,
                   seq=seq, source="degiro:account", note=note)
_a137, _b137, _c137, _e137 = (_dt132(2026, 9, 1, 0, 43), _dt132(2026, 9, 2, 1, 32), _dt132(2026, 9, 3, 16, 30), _dt132(2026, 9, 3, 18, 0))
_tx137 = [
    _row137("INTERNAL", _a137, 1000.0, "EUR", -1000.0, 9, "Reservation iDEAL"),           # the money is in: -2000 -> -1000
    _row137("INTERNAL", _b137, -1000.0, "EUR", -2000.0, 8, "Reservation iDEAL"),          # reversed in one instant ...
    _row137("DEPOSIT", _b137, 1000.0, "EUR", -1000.0, 7, "iDEAL Deposit"),                # ... with the deposit itself
    _row137("INTERNAL", _b137, 1000.0, "EUR", 0.0, 6, "Degiro Cash Sweep Transfer"),      # a balance never held
    _row137("BUY_CASH", _c137, -660.0, "USD", -660.0, 4, "Koop 4 @ 165 USD"),             # one purchase, four rows,
    _row137("FEE", _c137, -2.0, "EUR", -1002.0, 3, "DEGIRO Transactiekosten"),            # one timestamp,
    _row137("FX", _c137, -569.26, "EUR", -1571.26, 2, "Valuta Debitering"),               # newest first
    _row137("FX", _c137, 660.0, "USD", 0.0, 1, "Valuta Creditering"),
    _row137("INTERNAL", _e137, 500.0, "EUR", -1071.26, 0, "Degiro Cash Sweep Transfer"),  # the day's last row is a sweep
]
_got137 = service.cash_account(list(reversed(_tx137)))
print(f"137 the day's cash          {ok(_got137['now'] == {'EUR': -1571.26, 'USD': 0.0} and _got137['days']['2026-09-03'] == {'EUR': -1571.26, 'USD': 0.0} and _got137['days']['2026-09-02'] == {'EUR': -1000.0} and _got137['flows'] == [(date(2026, 9, 1), 1000.0)])}  "
      f"after a dollar purchase {_got137['now']}; the day of the deposit {_got137['days']['2026-09-02']}; "
      f"money in on {[f'{a:%d/%m}' for a, _ in _got137['flows']]}, the day it was reserved")

# 138 each year stands on its own and the years add up. A year's result is
#     the value at the end less the value at the start less what was paid in,
#     and it has to equal realised + change in unrealised + dividends +
#     interest and fees + credits, to the euro. That identity is what found
#     the cash line reading the wrong rows: one year was hundreds short. The years
#     chain to the TWR, the last column is the Year to date tile, and the
#     distance to the Total return tile is the one the table states.
_Y138 = d.get("years") or {}
if not _Y138.get("rows"):
    print("138 the years add up        SKIP  no daily curve to read the years off yet")
else:
    _cols138 = _Y138["rows"] + [_Y138["total"]]
    _res138 = max(abs(r["residual_eur"]) for r in _cols138)
    _ch138 = 1.0
    for _y138 in _Y138["rows"]:
        _ch138 *= 1 + _y138["twr_pct"] / 100
    _H138, _last138 = _Y138["headline"], _Y138["rows"][-1]
    _ytd138 = T.get("ytd_twr_pct") is None or _last138["year"] != str(date.today().year) or abs(_last138["twr_pct"] - T["ytd_twr_pct"]) < 0.011
    _ok138 = (_res138 < 1.0 and abs((_ch138 - 1) * 100 - d["curve"]["twr_pct"]) < 0.06
              and abs(sum(r["result_eur"] for r in _Y138["rows"]) - _Y138["total"]["result_eur"]) < 0.05
              and abs(_Y138["total"]["result_eur"] - _H138["gap_eur"] - T["profit_eur"]) < 0.05 and _ytd138
              and abs(_Y138["total"]["paid_in_eur"] - T["capital_eur"]) < 0.05)
    print(f"138 the years add up        {ok(_ok138)}  " + ", ".join(f"{r['year']} {r['result_eur']:+,.0f} ({r['twr_pct']:+.1f}%)" for r in _Y138["rows"])
          + f"; worst year out by {_res138:.2f}; chained {(_ch138 - 1) * 100:.2f}% vs TWR {d['curve']['twr_pct']}%; "
          f"table {_Y138['total']['result_eur']:,.0f} less {_H138['gap_eur']:,.0f} of valuation = the tile's {T['profit_eur']:,.0f}")

# 139 a day's return, and the fall from a high. Money that arrives is not a
#     return (27), and on the first day the return is on the money that came
#     in, because there is no yesterday. The fall from a high is measured on
#     the return index: in euros a deposit fills in the fall it arrives during.
_r139 = [x if x is not None else float("nan") for x in
         (service._day_return(2498.81, 2550.0, None), service._day_return(150.0, 50.0, 100.0), service._day_return(110.0, 0.0, 100.0))]
_cv139 = service.build_curve(
    [_Txn132(kind="BUY", trade_dt=_dt132(2026, 1, 5, 10, 0), isin="X139", quantity=10, price=10.0, currency="EUR", total_eur=-100.0),
     _Txn132(kind="BUY", trade_dt=_dt132(2026, 1, 7, 10, 0), isin="X139", quantity=1, price=8.0, currency="EUR", total_eur=-8.0)],
    {"X139": {"currency": "EUR", "kind": "EQUITY"}}, {}, {}, date(2026, 1, 5), date(2026, 1, 9), "none",
    external_flows=[(date(2026, 1, 5), 100.0), (date(2026, 1, 7), 58.0)],
    cash_series={"2026-01-05": 0.0, "2026-01-07": 50.0}, cash_eur=50.0)
def _fall139(vals):
    top, worst = vals[0], 0.0
    for x in vals:
        top = max(top, x)
        worst = min(worst, x / top - 1)
    return round(worst * 100, 2)
_x139, _v139 = [p["x"] for p in _cv139["series"]], [p["v"] for p in _cv139["series"]]
# ... and the figure under your own curve is that fall, not the one in euros
_dd139 = d["curve"].get("max_drawdown_pct")
_own139 = _dd139 is None or abs(_dd139 - _fall139([1.0] + [p["x"] for p in d["curve"]["series"]])) < 0.011
print(f"139 returns and falls       {ok(abs(_r139[0] + 0.02007) < 1e-4 and abs(_r139[1]) < 1e-12 and abs(_r139[2] - 0.10) < 1e-12 and _fall139(_x139) == -20.0 and _fall139(_v139) == 0.0 and _own139)}  "
      f"first day {_r139[0] * 100:.2f}% on the money put in; a share falls a fifth while 58 is paid in: "
      f"the account goes {_v139[0]:.0f} to {_v139[-1]:.0f} in euros, the fall is {_fall139(_x139)}% on returns and {_fall139(_v139)}% on euros"
      + (f"; yours, worst {_dd139}%" if _dd139 is not None else ""))

# 140 an option's price and how it moves are one formula read five ways. A
#     call less a put is the share less the strike, both discounted; their
#     deltas differ by exactly the carry; gamma and vega are shared; and each
#     figure is the slope it claims to be.
_o140 = _op123
_S140, _K140, _T140, _v140, _rr140, _q140 = 100.0, 105.0, 0.25, 0.40, 0.04, 0.01
_c140, _p140 = (_o140.worth(_S140, _K140, _T140, _v140, _rr140, _q140, x) for x in (True, False))
_gc140, _gp140 = (_o140.greeks(_S140, _K140, _T140, _v140, _rr140, _q140, x) for x in (True, False))
_w140 = lambda s=_S140, vv=_v140: _o140.worth(s, _K140, _T140, vv, _rr140, _q140, True)
_ok140 = (abs((_c140 - _p140) - (_S140 * _m125.exp(-_q140 * _T140) - _K140 * _m125.exp(-_rr140 * _T140))) < 1e-9
          and abs(_gc140["delta"] - _gp140["delta"] - _m125.exp(-_q140 * _T140)) < 1e-9
          and abs(_gc140["gamma"] - _gp140["gamma"]) < 1e-12 and abs(_gc140["vega"] - _gp140["vega"]) < 1e-12
          and abs((_w140(_S140 + 0.01) - _w140(_S140 - 0.01)) / 0.02 - _gc140["delta"]) < 1e-5
          and abs((_w140(_S140 + 0.01) - 2 * _c140 + _w140(_S140 - 0.01)) / 1e-4 - _gc140["gamma"]) < 1e-4
          and abs((_w140(vv=_v140 + 0.005) - _w140(vv=_v140 - 0.005)) - _gc140["vega"]) < 1e-5
          and _gc140["theta"] < 0 and _gp140["theta"] < 0)
print(f"140 how an option moves     {ok(_ok140)}  100 share, 105 strike, 3 months, 40%: call {_c140:.2f} put {_p140:.2f}; "
      f"delta {_gc140['delta']:.3f} / {_gp140['delta']:.3f}, gamma {_gc140['gamma']:.4f}, a point of volatility {_gc140['vega']:.3f}, a day {_gc140['theta']:.3f}")

# 141 one contract, worked out from a chain whose prices are known: every
#     option priced at 40%. The contract view has to hand back that 40%, a
#     model price equal to the price it started from, break-even at strike
#     plus premium, a payoff of nothing at break-even, odds that agree with
#     the formula, and the same sentences turned round for the seller.
import time as _t141
_now141 = _t141.time()
_exp141 = (date.today() + _td(days=90)).isoformat()
_T141 = (_o140._expiry_ts(_exp141) - _now141) / (_o140.YEAR * 86400)
_F141, _df141 = 100.0 * _m125.exp(0.04 * _T141), _m125.exp(-0.04 * _T141)
def _leg141(call):
    out = []
    for _k in range(60, 145, 5):
        _px = _df141 * _o140.black(_F141, float(_k), _T141, 0.40, call)
        out.append([float(_k), _px * 0.99, _px * 1.01, _px, int(_now141) - 600, 100, 1000])
    return out
_ch141 = {"sym": "TEST", "fetched": _now141, "state": "REGULAR", "spot": 100.0, "spot_ts": _now141, "prev_close": 100.0,
          "div_yield": 0.0, "earnings_ts": None, "name": "Test", "currency": "USD",
          "exps": [{"exp": _exp141, "calls": _leg141(True), "puts": _leg141(False)}]}
_keep141 = (_o140.reading, _o140._rate)
try:
    _o140.reading = lambda sym, force=False: (_ch141, None)
    _o140._rate = lambda T: 0.04
    _c141 = _o140.contract("TEST", _exp141, 105.0, True)
    _p141 = _o140.contract("TEST", _exp141, 95.0, False)
finally:
    _o140.reading, _o140._rate = _keep141
_px141 = _df141 * _o140.black(_F141, 105.0, _T141, 0.40, True)
_s141 = 0.40 * _m125.sqrt(_T141)
_itm141 = _o140.ncdf((_m125.log(_F141 / 105.0) - 0.5 * _s141 * _s141) / _s141)
_be141 = next(s for s in _c141["scenarios"] if s["mark"] == "break-even")
_ok141 = (abs(_c141["price"] - _px141) < 1e-3 and abs(_c141["iv"] - 0.40) < 0.003 and abs(_c141["model"] - _c141["price"]) < 1e-3
          and abs(_c141["premium"] - _c141["price"] * 100) < 0.02 and abs(_c141["breakeven"] - (105.0 + _c141["price"])) < 0.01
          and _c141["max_gain"] is None and abs(_p141["max_gain"] - (95.0 - _p141["price"]) * 100) < 0.02
          and abs(_be141["pl_end"]) < 0.01 and any(s["mark"] == "strike" for s in _c141["scenarios"])
          and abs(_c141["p_itm"] - _itm141) < 0.03 and _c141["p_profit"] < _c141["p_itm"] and _p141["p_profit"] < _p141["p_itm"]
          and "right" in _c141["words"]["bought"]["what"] and "deliver" in _c141["words"]["sold"]["what"]
          and "must buy" in _p141["words"]["sold"]["what"] and len(_c141["words"]["bought"]["lines"]) >= 7
          and len(_c141["curve"]["x"]) == 91 and abs(_c141["curve"]["end"][0] + _c141["price"]) < 1e-3)
print(f"141 one contract            {ok(_ok141)}  the 105 call of a chain priced at 40%: {_c141['price']:.2f} a share, read back as "
      f"{_c141['iv'] * 100:.1f}%, break-even {_c141['breakeven']:.2f}, in the money {_c141['p_itm'] * 100:.0f}% (the formula says {_itm141 * 100:.0f}%), "
      f"makes money {_c141['p_profit'] * 100:.0f}%")

# 142 the pages ask for what this server now has: the contract route, the
#     year table in the dashboard, the return index the drawdown strip reads.
_pt142 = open(os.path.join(HERE, "backend", "static", "portfolio.html"), encoding="utf-8").read()
_oj142 = open(os.path.join(HERE, "backend", "static", "options.js"), encoding="utf-8").read()
_ok142 = ("/api/pf/options/{sym}/contract" in _routes and "yearPanel()" in _pt142 and "DATA.years" in _pt142
          and "p.x??p.v" in _pt142 and all(w in _oj142 for w in ("openContract(", "howSurface()", "howChain()", "/contract?exp="))
          and "years" in d and (not d["curve"].get("series") or "x" in d["curve"]["series"][-1]))
print(f"142 the pages are wired     {ok(_ok142)}  PORT draws the year table and reads the return index; OPT opens a contract from the chain")

# --------------------------------------------------------------------------
# OIL (build 24): the oil market page
# --------------------------------------------------------------------------
from pf import oil as _oil, oilmap as _oilmap, ais as _ais
import io as _io143, json as _json153
from datetime import datetime as _dt148

# 143 the futures curve: a month leaves the curve on its exchange's expiry
#     day, not when Yahoo stops quoting it, and the day's change is measured on
#     one contract. WTI Nov 26 expires 20 Oct (the 25th is a Sunday), Brent
#     Dec 26 on 30 Oct; a Brent month expired a week ago but still quoted must
#     not be the front. A curve whose front month is missing, or that jumps
#     over a hole of months (Yahoo had Aug 25 and then nothing until Dec 26),
#     is not drawn, and a spread "a year later" is a calendar year, not the
#     twelfth point.
_e143 = [_oil.expiry("CL", 2026, 11).isoformat(), _oil.expiry("BZ", 2026, 12).isoformat(),
         _oil.expiry("CL", 2026, 12).isoformat(), _oil.expiry("BZ", 2027, 1).isoformat()]
_days143 = ["2026-10-0%d" % i for i in (1, 2, 5, 6, 7)]
_px143 = {"BZX26.NYM": {d: 103.5 for d in _days143},                  # expired 30 Sep, still quoted
          "BZZ26.NYM": dict(zip(_days143, [100.0, 101.0, 102.0, 103.0, 104.0])),
          "BZF27.NYM": dict(zip(_days143, [97.0, 98.0, 99.0, 100.0, 100.5]))}
for _i143 in range(2, 14):                                             # months Feb 27 .. Jan 28
    _y143, _mo143 = 2027 + (_i143 - 1) // 12, (_i143 - 1) % 12 + 1
    _px143[_oil.contract_sym("BZ", _y143, _mo143)] = {d: 100.0 - _i143 for d in _days143}
_b143 = _oil.Book(_px143, "BZ")
_f143 = _b143.front()
_c143 = _b143.curve("2026-10-07", 13)
# Dec 26, then nothing until Jun 27, then eight months: long enough to be a
# curve, so only the hole can stop it being drawn.
_hole143 = _oil.Book({k: v for k, v in _px143.items()
                      if k == "BZZ26.NYM" or k in {_oil.contract_sym("BZ", *ym) for ym in
                                                   [(2027, m) for m in range(6, 13)] + [(2028, 1)]}}, "BZ").curve("2026-10-07")
_nofront143 = _oil.Book({k: v for k, v in _px143.items() if k != "BZZ26.NYM"}, "BZ").curve("2026-10-07")
_gap143 = _oil.ym_spread([("2026-12", 104.0), ("2027-01", 100.5), ("2028-01", 87.0)], 12)
_ok143 = (_e143 == ["2026-10-20", "2026-10-30", "2026-11-20", "2026-11-30"]
          and _hole143 == [] and _nofront143 == [] and _gap143 is None
          and _f143 and _f143["sym"] == "BZZ26.NYM" and abs(_f143["chg"] - 1.0) < 1e-9
          and len(_c143) == 13 and abs((_c143[0]["px"] - _c143[12]["px"]) - (104.0 - 88.0)) < 1e-9
          and _oil.spreads({"2026-10-07": [(p["ym"], p["px"]) for p in _c143]})[12][0][1] == 16.0)
print(f"143 the curve's months       {ok(_ok143)}  expiries {_e143[:2]}; front {(_f143 or {}).get('lab')} "
      f"({(_f143 or {}).get('chg')} on the day, not the roll); Dec 26 less Dec 27 = {(_c143[0]['px'] - _c143[12]['px']) if len(_c143) > 12 else '?'}; "
      f"a curve with a hole: {'not drawn' if not _hole143 else len(_hole143)}")

# 144 crack spreads: products are quoted a gallon, crude a barrel of 42.
_k144 = _oil.crack_series({"CL=F": {"2026-10-02": 60.0}, "BZ=F": {"2026-10-02": 65.0},
                           "RB=F": {"2026-10-02": 2.0}, "HO=F": {"2026-10-02": 2.5}})
_v144 = {k: _k144[k]["2026-10-02"] for k in ("c321", "diesel", "gasoline")}
print(f"144 crack spreads            {ok(abs(_v144['c321'] - 31.0) < 1e-9 and abs(_v144['diesel'] - 40.0) < 1e-9 and abs(_v144['gasoline'] - 19.0) < 1e-9)}  "
      f"3-2-1 {_v144['c321']:.2f}, diesel over Brent {_v144['diesel']:.2f}, gasoline {_v144['gasoline']:.2f} (60 WTI, 65 Brent, $2.00 and $2.50 a gallon)")

# 145 JODI and the EIA outlook are read the way they are written: JODI's "-"
#     is a figure nobody reported, not a zero; the outlook's release month can
#     sit two columns from its label.
_j145 = {}
_jodi145 = ("REF_AREA,TIME_PERIOD,ENERGY_PRODUCT,FLOW_BREAKDOWN,UNIT_MEASURE,OBS_VALUE,ASSESSMENT_CODE\n"
            "SA,2026-07,CRUDEOIL,INDPROD,KBD,8135.0968,1\nSA,2026-07,CRUDEOIL,TOTEXPSB,KBD,4124.6,3\n"
            "SA,2026-07,CRUDEOIL,INDPROD,KTONS,33000,1\nSA,2026-07,NGL,INDPROD,KBD,1500,1\n"
            "IQ,2026-07,CRUDEOIL,INDPROD,KBD,-,3\nSA,2026-07,CRUDEOIL,CLOSTLV,KBBL,150000,1\n")
_n145 = _oil.parse_jodi("﻿" + _jodi145, _j145)
import openpyxl as _ox145
_wb145 = _ox145.Workbook()
_ws145 = _wb145.active; _ws145.title = "Dates"
_ws145.append(["Forecast Month -", "September 2026"])
_ws145.append(["Table Beginning Month--- ", None, 202201])
_ws145.append(["Last Historical Month--- ", None, 202202])
for _t145 in _oil.STEO_TABS:
    _s145 = _wb145.create_sheet(_t145)
    _s145.append(["Table"]); _s145.append([None]); _s145.append(["Forecast date:", None, 2022])
    _s145.append([None, None, "Jan", "Feb", "Mar"])
    if _t145 == "3dtab":
        _s145.append(["copr_sa", "Saudi Arabia", 10.1, 10.2, 10.3])
        _s145.append(["Forecast date: x", "a label with spaces", 1, 2, 3])
_bio145 = _io143.BytesIO(); _wb145.save(_bio145)
_st145 = _oil.parse_steo(_bio145.getvalue())
_sa145 = _st145["tabs"]["3dtab"]["rows"].get("copr_sa") or {}
_ok145 = (_n145 == 3 and _j145["SA"]["prod"] == {"2026-07": 8135.0968} and "IQ" not in _j145
          and _j145["SA"]["stock"] == {"2026-07": 150000.0}
          and _st145["meta"].get("last_hist_ym") == "2022-02" and _sa145 == {"2022-01": 10.1, "2022-02": 10.2, "2022-03": 10.3}
          and len(_st145["tabs"]["3dtab"]["rows"]) == 1)
print(f"145 JODI and the outlook     {ok(_ok145)}  {_n145} JODI rows kept of 6 (crude, kb/d, reported); outlook history to "
      f"{_st145['meta'].get('last_hist_ym')}, Saudi {list(_sa145.values())}")

# 146 tanker traffic: a week's average against the same week a year earlier,
#     and no answer from a week with most days missing.
_d146 = {}
for _i146 in range(0, 400):
    _day146 = (date(2026, 9, 27) - __import__("datetime").timedelta(days=_i146)).isoformat()
    _d146[_day146] = 2.0 if _i146 < 7 else 50.0
_fs146 = _oil.flow_stats(_d146)
_gap146 = {k: v for k, v in _d146.items() if k > "2026-09-24"}            # three days of a week
_w146 = _oil.window(_gap146, "2026-09-27", 7)
_ok146 = (abs(_fs146["w7"] - 2.0) < 1e-9 and abs(_fs146["y7"] - 50.0) < 1e-9 and abs(_fs146["vs_y7"] + 96.0) < 1e-6
          and _w146 is None and _oil._status(_fs146["vs_y7"]) == "shut"
          and _oil._status(-30) == "reduced" and _oil._status(5) == "normal" and _oil.BBL_PER_T == 7.33)
print(f"146 tanker traffic           {ok(_ok146)}  {_fs146['w7']} a day against {_fs146['y7']} a year ago = {_fs146['vs_y7']}% "
      f"({_oil._status(_fs146['vs_y7'])}); three days of seven give {'no average' if _w146 is None else f'an average of {_w146:.2f}'}")

# 147 OPEC's press releases are read for who met, what they decided and when
#     they meet next; the typed-in targets are flagged once OPEC publishes a newer table.
_html147 = ("<html><body><p>The seven OPEC+ countries, which previously announced additional voluntary adjustments in "
            "April and November 2023, namely Saudi Arabia, Russia, Iraq, Kuwait, Kazakhstan, Algeria, and Oman met virtually "
            "on 4 October 2026, to review global market conditions and outlook.</p><p>The seven participating countries "
            "decided to maintain September 2026 required production for November 2026 as detailed in the table below.</p>"
            "<p>The seven OPEC+ countries will continue to hold monthly meetings to review market conditions. The next meeting "
            "will be held on 1 November 2026.</p></body></html>")
_r147 = _oil.parse_release("616-4-october-2026", _html147)
_sd147 = _oil.seed().get("opec_targets") or {}
_newer147 = _oil.targets_newer([{**_r147, "slug": "620-1-november-2026", "date": "2026-11-01"}], _sd147)
_same147 = _oil.targets_newer([_r147], _sd147)
_ok147 = (_r147["kind"] == "voluntary" and _r147["group_size"] == "seven" and len(_r147["members"]) == 7
          and _r147["members"][-1] == "Oman" and _r147["next"] == "2026-11-01" and _r147["date"] == "2026-10-04"
          and _r147["has_table"] and _newer147 is not None and _same147 is None
          and len(_sd147.get("kbd") or {}) == 7 and all(500 <= v <= 12000 for v in (_sd147.get("kbd") or {}).values()))
print(f"147 OPEC's decisions         {ok(_ok147)}  {_r147['group_size']} members, {_r147['members'][0]!r} to {_r147['members'][-1]!r}; next meeting {_r147['next']}; "
      f"targets for {_sd147.get('month')} flagged when a newer table appears")

# 148 pump prices: the bulletin is in euros per thousand litres, with and
#     without taxes, one column per country and fuel.
_wb148 = _ox145.Workbook()
_w148 = _wb148.active; _w148.title = "Prices with taxes"
_w148.append(["Consumer prices", "CTR", "NL_price_with_tax_euro95", "NL_price_with_tax_diesel", "CTR", "DE_price_with_tax_euro95"])
_w148.append([None]); _w148.append(["Date"])
_w148.append([_dt148(2026, 9, 28), "NL_", 2464.6, 2526.0, "DE_", 2345.0])
_w148b = _wb148.create_sheet("Prices wo taxes")
_w148b.append(["Consumer prices", "CTR", "NL_price_wo_tax_euro95", "NL_price_wo_tax_diesel"])
_w148b.append([None]); _w148b.append(["Date"])
_w148b.append([_dt148(2026, 9, 28), "NL_", 1183.0, 1540.9])
_bio148 = _io143.BytesIO(); _wb148.save(_bio148)
_p148 = _oil.parse_eu_bulletin(_bio148.getvalue())
_nl148 = _p148.get("NL") or {}
_tax148 = 1 - _nl148.get("diesel_net", {}).get("2026-09-28", 0) / _nl148.get("diesel_tax", {}).get("2026-09-28", 1)
_ok148 = (_nl148.get("diesel_tax") == {"2026-09-28": 2.526} and _nl148.get("euro95_tax") == {"2026-09-28": 2.4646}
          and (_p148.get("DE") or {}).get("euro95_tax") == {"2026-09-28": 2.345} and abs(_tax148 - 0.39) < 0.005)
print(f"148 pump prices              {ok(_ok148)}  Dutch diesel €{_nl148.get('diesel_tax', {}).get('2026-09-28')} a litre, "
      f"{_tax148 * 100:.0f}% of it tax; Euro 95 €{_nl148.get('euro95_tax', {}).get('2026-09-28')}")

# 149 who trades: positions net out long against short; a customs line whose
#     weight cannot be right for crude (a tonne is five to eight barrels) is
#     read from its value instead; Comtrade's own country codes resolve.
_row149 = _oil._cot_row("2026-09-29", "1878576", "615887", "296348", "113558", "575715", "209028", "129436",
                        "152323", "122452", "79850", "46695")
_good149, _how1 = _oil.flow_kbd(80770283624.0 / 1000 * 1000, 43389398132.0, 70.0)    # Saudi to China, 2025
_bad149, _how2 = _oil.flow_kbd(400e9, 30e9, 75.0)                                     # weight ten times too big
_cn149 = _oil.countries()["byn"]
_ok149 = (_row149["mm_net"] == 79592 and _row149["pm_net"] == 319539 and _row149["sw_net"] == -462157
          and _how1 == "weight" and 1500 < _good149 < 1700 and _how2 == "value" and abs(_bad149 - 30e9 / 75 / 365 / 1000) < 1e-6
          and _cn149.get("842") == "US" and _cn149.get("251") == "FR" and _cn149.get("699") == "IN" and _cn149.get("490") == "TW"
          and _cn149.get("682") == "SA")
print(f"149 who trades               {ok(_ok149)}  WTI managed money net {_row149['mm_net']:+,}; Saudi crude to China "
      f"{_good149:,.0f} kb/d by weight; an implausible weight falls back to value ({_bad149:,.0f} kb/d)")

# 150 your book and oil: five-day returns find a sensitivity that daily
#     returns miss when the two close at different times. A share that moves
#     half as much as Brent, but a day later, has a daily beta near nothing.
import random as _rnd150
_rnd150.seed(150)
_b150, _s150, _pb, _ps = {}, {}, 100.0, 100.0
_prev150 = 0.0
_d0150 = date(2024, 1, 1)
for _i150 in range(520):
    _day150 = (_d0150 + __import__("datetime").timedelta(days=_i150)).isoformat()
    _r150 = _rnd150.gauss(0, 0.02)
    _pb *= 1 + _r150
    _ps *= 1 + 0.5 * _prev150 + _rnd150.gauss(0, 0.004)
    _prev150 = _r150
    _b150[_day150], _s150[_day150] = _pb, _ps
_five150 = _oil._beta_to(_s150, _b150)
from pf import risk as _risk150
_ys150, _xs150 = _risk150.price_windows(_s150, _b150, h=1)
_daily150 = _risk150.ols(_ys150, _xs150)
_ok150 = bool(_five150 and 0.3 <= _five150["beta"] <= 0.6 and _daily150 and abs(_daily150["beta"]) < 0.15)
print(f"150 oil and your book        {ok(_ok150)}  a share moving half as much as Brent a day later: five-day beta "
      f"{(_five150 or {}).get('beta')}, daily {(_daily150 or {}).get('beta', 0):.2f}")

# 151 live tankers: a tanker is AIS type 80-89 learned from its static report,
#     only tankers' positions are kept, a refused key is an error not silence,
#     and Finland's feed drops stale and non-tanker ships.
_st151 = _ais.Stream(); _st151._loaded = True
_st151.handle({"MessageType": "ShipStaticData", "MetaData": {"MMSI": 538010447},
               "Message": {"ShipStaticData": {"Type": 80, "Name": "ATLANTIC PRESTIGE", "Destination": "RU ULU", "MaximumStaticDraught": 14.2}}})
_st151.handle({"MessageType": "ShipStaticData", "MetaData": {"MMSI": 111}, "Message": {"ShipStaticData": {"Type": 70}}})
_st151.handle({"MessageType": "PositionReport", "MetaData": {"MMSI": 538010447, "latitude": 59.91, "longitude": 26.15},
               "Message": {"PositionReport": {"Sog": 11.2, "Cog": 271.0}}})
_st151.handle({"MessageType": "PositionReport", "MetaData": {"MMSI": 111, "Latitude": 50.0, "Longitude": 1.0},
               "Message": {"PositionReport": {"Sog": 9.0}}})
_snap151 = _st151.snapshot()["tankers"]
try:
    _st151.handle({"error": "Api Key Is Not Valid"}); _ref151 = False
except PermissionError:
    _ref151 = True
_now151 = 1_800_000_000_000
_bal151 = _ais.parse_baltic({"features": [
    {"mmsi": 1, "geometry": {"coordinates": [26.0, 60.0]}, "properties": {"timestampExternal": _now151 - 60_000, "sog": 10, "cog": 90}},
    {"mmsi": 2, "geometry": {"coordinates": [25.0, 59.5]}, "properties": {"timestampExternal": _now151 - 60_000}},
    {"mmsi": 1, "geometry": {"coordinates": [24.0, 59.0]}, "properties": {"timestampExternal": _now151 - 5 * 3600_000}}]},
    {"1": [80, "T", "RU PRI", 12.0, 1]}, now_ms=_now151)
_ok151 = (len(_snap151) == 1 and _snap151[0][4] == "ATLANTIC PRESTIGE" and _snap151[0][:2] == [59.91, 26.15]
          and _ref151 and len(_bal151) == 1 and _bal151[0][5] == "RU PRI" and _ais.is_tanker(84) and not _ais.is_tanker(70))
print(f"151 live tankers             {ok(_ok151)}  {len(_snap151)} of 2 ships placed as tankers ({_snap151[0][4] if _snap151 else '?'}), a refused key {'raises' if _ref151 else 'is ignored'}; "
      f"the Baltic feed keeps {len(_bal151)} of 3 reports (fresh, tanker)")

# 152 oil alerts test the market, not a holding, and say so when there is no data.
_v152 = {"brent": {"v": 102.25, "asof": "2026-10-02"}, "hormuz": {"v": 1.3, "asof": "2026-09-27"}}
_a152 = [_oil.check_rule("oil_brent_above", 100, _v152), _oil.check_rule("oil_brent_below", 100, _v152),
         _oil.check_rule("oil_hormuz_above", 20, _v152), _oil.check_rule("oil_nldiesel_above", 2.0, _v152)]
_ok152 = (_a152[0] and _a152[0]["level"] == "warn" and "above" in _a152[0]["text"] and _a152[1] is None
          and _a152[2] is None and _a152[3] and _a152[3]["level"] == "info" and "no data" in _a152[3]["text"]
          and all(k in service.RULE_KINDS for k in _oil.ALERT_KINDS))
print(f"152 oil alerts               {ok(_ok152)}  “{_a152[0]['text'] if _a152[0] else '?'}”; a rule with no data says so")

# 153 the map puts things where they are: the Equal Earth projection in
#     oilmap.js has the published constants, the country file is in degrees,
#     Norway's platforms convert from degrees-minutes-seconds, and OpenStreetMap
#     points on top of an official one are dropped.
_js153 = open(os.path.join(HERE, "backend", "static", "oilmap.js"), encoding="utf-8").read()
_A153 = dict(_re.findall(r"(A[1-4]) = (-?[\d.]+)", _js153))
_land153 = _json153.loads(open(os.path.join(HERE, "backend", "static", "land.json"), encoding="utf-8").read())
_xy153 = [v for c in _land153["c"] for r in c["p"] for v in r]
_lon153, _lat153 = _xy153[0::2], _xy153[1::2]
_sod153 = _oilmap.parse_sodir("fclName,fclPhase,fclSurface,fclKind,fclNsDeg,fclNsMin,fclNsSec,fclNsCode,fclEwDeg,fclEwMin,fclEwSec,fclEwCode,fclCurrentOperatorName\n"
                              "EKOFISK J,IN SERVICE,Y,JACKET 8 LEGS,56,32,57.00,N,3,12,30.00,E,ConocoPhillips\n"
                              "TEMPLATE,IN SERVICE,N,MULTI WELL TEMPLATE,56,0,0,N,3,0,0,E,X\n"
                              "OLD,REMOVED,Y,JACKET 4 LEGS,58,0,0,N,2,0,0,E,X\n")
_mg153 = _oilmap.merge_platforms([[56.55, 3.21, "osm", "", "near", "", ""], [27.0, 50.0, "osm", "oil", "far", "", ""]], _sod153)
_ok153 = (_A153 == {"A1": "1.340264", "A2": "-0.081106", "A3": "0.000893", "A4": "0.003796"}
          and len(_land153["c"]) >= 200 and min(_lon153) >= -180 and max(_lon153) <= 180 and min(_lat153) >= -90 and max(_lat153) <= 90
          and any(c["iso"] == "TW" for c in _land153["c"]) and any(c["iso"] == "BH" for c in _land153["c"])
          and len(_sod153) == 1 and abs(_sod153[0][0] - 56.549) < 0.001 and abs(_sod153[0][1] - 3.208) < 0.001
          and [p[4] for p in _mg153] == ["EKOFISK J", "far"])
print(f"153 the map's places         {ok(_ok153)}  Equal Earth constants as published; {len(_land153['c'])} countries in degrees; "
      f"Ekofisk at {_sod153[0][0] if _sod153 else '?'}N {_sod153[0][1] if _sod153 else '?'}E; the OSM copy beside it dropped")

# 154 the page is wired: every route it calls exists, the shell knows OIL by
#     its words, MKT and GEO link to it, the brief carries its line, and the
#     notes' working folder stays out of git.
_oj154 = open(os.path.join(HERE, "backend", "static", "oil.js"), encoding="utf-8").read()
_need154 = {"/api/pf/oil/{part}", "/api/pf/oil/history", "/api/pf/oil/country/{iso}", "/api/pf/oil/map",
            "/api/pf/oil/platforms", "/api/pf/oil/tankers", "/api/pf/oil/alerts", "/api/pf/oil/note", "/api/pf/oil/sources"}
_mk154 = open(os.path.join(HERE, "backend", "static", "market.html"), encoding="utf-8").read()
_geo154 = open(os.path.join(HERE, "backend", "static", "geo.html"), encoding="utf-8").read()
_ix154 = open(os.path.join(HERE, "backend", "static", "index.html"), encoding="utf-8").read()
_gi154 = open(os.path.join(HERE, ".gitignore"), encoding="utf-8").read()
import inspect as _insp154
from pf import morning as _mo154
_ok154 = (_need154 <= _routes and "/m/MKT/oil" in _routes and all(p in _oj154 for p in _oil.PARTS)
          and '"CRUDE", "CRUD", "BRENT", "WTI", "OPEC"' in _js and 'code: "OIL"' in _js
          and 'href="/m/MKT/oil"' in _mk154 and "/m/MKT/oil#s-flows" in _geo154
          and 'id="sAis"' in _ix154 and "oil_brent_above" in _ix154 and "\noil_work/" in _gi154
          and 'b.get("oil")' in _insp154.getsource(_mo154.summary) and "oilmod.brief_line" in _insp154.getsource(_mo154.build))
print(f"154 the page is wired        {ok(_ok154)}  {len(_need154)} oil routes registered; OIL, CRUDE and BRENT open it; "
      f"MKT and GEO link to it; the brief carries its line; oil_work/ is ignored")

# ==========================================================================
# WCH: the watchlist (build 25). Fixtures only: no check here asks Yahoo.
# ==========================================================================
from pf import watch as _wl
from datetime import datetime as _dtw, timedelta as _tdw

# 155 one company, one listing: a company quoted in several places is shown
#     once, by the listing that trades in the currency it reports in (your own
#     when you hold it); copies of foreign companies, preference shares and
#     prices in currencies the screen cannot convert are dropped; market caps
#     are converted to dollars before the $5bn floor, and London's pence are pence.
_usd155 = {"USD": 1.0, "EUR": 1.10, "GBP": 1.30, "SEK": 0.10}
def _q155(sym, ex, ccy, fin, mc, px=100.0, vol=1e6, name=None):
    return {"symbol": sym, "exchange": ex, "currency": ccy, "financialCurrency": fin, "marketCap": mc,
            "regularMarketPrice": px, "averageDailyVolume3Month": vol, "longName": name or sym,
            "fiftyTwoWeekHigh": px * 1.25, "quoteType": "EQUITY"}
_raw155 = [
    (_q155("ASML.AS", "AMS", "EUR", "EUR", 600e9, 1600, 6e5, "ASML Holding N.V."), "EU"),
    (_q155("ASML", "NMS", "USD", "EUR", 650e9, 1700, 1.2e6, "ASML Holding N.V."), "US"),
    (_q155("ASME.DE", "GER", "EUR", "EUR", 600e9, 1600, 1e3, "ASML Holding N.V."), "EU"),
    (_q155("NVDA", "NMS", "USD", "USD", 4.5e12, 180, 2e8, "NVIDIA Corporation"), "US"),
    (_q155("NVD.DE", "GER", "EUR", "USD", 4.0e12, 160, 5e5, "NVIDIA Corporation"), "EU"),
    (_q155("SSU.VI", "VIE", "EUR", "KRW", 300e9, 50, 1e3, "Samsung Electronics Co., Ltd."), "EU"),
    (_q155("BAC", "NYQ", "USD", "USD", 300e9, 40, 3e7, "Bank of America Corporation"), "US"),
    (_q155("BAC-PB", "NYQ", "USD", "USD", 300e9, 25, 1e5, "Bank of America Corporation"), "US"),
    (_q155("SHELL.AS", "AMS", "EUR", "USD", 220e9, 43, 4e6, "Shell plc"), "EU"),
    (_q155("SHEL", "NYQ", "USD", "USD", 240e9, 75, 5e6, "Shell plc"), "US"),
    (_q155("SHEL.L", "LSE", "GBp", "USD", 180e9, 3648, 1e6, "Shell plc"), "EU"),
    (_q155("VOLO-PREF.ST", "STO", "SEK", "SEK", 60e9, 300, 1e4, "Volvo Car AB (publ) pref"), "EU"),
    (_q155("SMALL.ST", "STO", "SEK", "SEK", 30e9, 100, 1e5, "Small AB"), "EU"),
]
_rows155 = [r for r in (_wl.qrow(x, "Technology", reg, _usd155) for x, reg in _raw155) if r]
_tyt155 = _wl.qrow(_q155("TYT.L", "LSE", "JPY", "JPY", 34e12, 2900), "Consumer Cyclical", "EU", _usd155)
_d155 = {r["sym"]: r for r in _wl.dedupe(_rows155, set())}
_dh155 = {r["sym"]: r for r in _wl.dedupe(_rows155, {"SHELL.AS"})}
_shl155 = next(r for r in _rows155 if r["sym"] == "SHEL.L")
_sm155 = next(r for r in _rows155 if r["sym"] == "SMALL.ST")
_ok155 = (set(_d155) == {"ASML.AS", "NVDA", "BAC", "SHEL", "SMALL.ST"} and "SHELL.AS" in _dh155 and "SHEL" not in _dh155
          and {"ASML", "ASME.DE"} <= set(_d155["ASML.AS"]["also"]) and _tyt155 is None
          and abs(_sm155["mcap_usd"] - 3e9) < 1 and _sm155["mcap_usd"] < _wl.MIN_CAP_USD
          and abs(_shl155["adv_usd"] - 1e6 * 36.48 * 1.30) < 1 and abs(_shl155["mcap_usd"] - 180e9 * 1.30) < 1
          and _wl.norm_name("ASML Holding N.V.") == _wl.norm_name("ASML HOLDING NV") == "asml")
print(f"155 one company, one listing {ok(_ok155)}  {len(_rows155)} listings → {len(_d155)}: ASML as {next((k for k in _d155 if k.startswith('ASM')), '?')}, "
      f"Nvidia as NVDA, Shell as SHEL or SHELL.AS when you hold it; Samsung in Vienna, a preference share and Toyota in yen dropped; "
      f"30bn kronor = ${_sm155['mcap_usd'] / 1e9:.0f}bn, under the floor")

# 156 the chart: the drop from the 52-week high, the low since, how unusual the
#     depth is for this stock, still falling or not; and the fall split into
#     market, sector and company parts that add up.
_days156, _t156 = [], date(2025, 6, 2)
while len(_days156) < 300:
    if _t156.weekday() < 5:
        _days156.append(_t156.isoformat())
    _t156 += _tdw(days=1)
_c156 = [50 + 50 * i / 150 for i in range(151)] + [100 - 30 * (i + 1) / 100 for i in range(100)] + [70.0] * 49
_p156 = _wl.price_metrics(_days156, _c156)
_pf156 = _wl.price_metrics(_days156, _c156[:-1] + [69.0])
_pr156 = _wl.price_metrics(_days156, _c156[:-10] + [72.0, 74.0, 76.0, 78.0] + [80.0] * 6)
_fp156 = _wl.fall_parts(-30.0, -10.0, -4.0)
_fn156 = _wl.fall_parts(-30.0, None, -4.0)
_wk156 = {f"2025-{i:02d}": 100 * (1.01 ** i) * (1 + 0.03 * ((i * 7) % 5 - 2)) for i in range(1, 61)}
_ok156 = (_p156 and _p156["dd52"] == -30.0 and _p156["hi_d"] == _days156[150] and _p156["lo_d"] == _days156[250]
          and _p156["since_low"] == 49 and _p156["trend"] == "basing" and 75 <= (_p156["pctile"] or 0) <= 85
          and _pf156["trend"] == "falling" and _pr156["trend"] == "recovering" and _pr156["dd52"] == -20.0
          and _fp156 == {"stock": -30.0, "market": -4.0, "sector": -6.25, "company": -22.22}
          and _fn156["sector"] is None and _fn156["company"] == -27.08
          and _wl.corr_weekly(_wk156, _wk156) == 1.0 and _wl.corr_weekly(dict(list(_wk156.items())[:20]), _wk156) is None)
print(f"156 the chart                {ok(_ok156)}  {(_p156 or {}).get('dd52')}% from a high of 100 on day 150, low 70, "
      f"{(_p156 or {}).get('since_low')} sessions ago: {(_p156 or {}).get('trend')}, deeper than {(_p156 or {}).get('pctile')}% of its days; "
      f"a new low makes it {(_pf156 or {}).get('trend')}, 80 again {(_pr156 or {}).get('trend')}; down 30% with the market −4% and its sector −10%: "
      f"the sector {_fp156['sector']}% against the market, the company {_fp156['company']}% against its sector")

# 157 Yahoo's answers are read the way Yahoo writes them: the first name a
#     statement line goes by wins, outflows become positive spend, gaps are
#     skipped; the dividend yield comes as a fraction; estimates by period.
_ts157 = {"timeseries": {"result": [
    {"meta": {"type": ["annualTotalRevenue"]}, "annualTotalRevenue": [
        {"asOfDate": "2024-12-31", "reportedValue": {"raw": 100.0}}, {"asOfDate": "2025-12-31", "reportedValue": {"raw": 110.0}}]},
    {"meta": {"type": ["annualOperatingRevenue"]}, "annualOperatingRevenue": [{"asOfDate": "2025-12-31", "reportedValue": {"raw": 999.0}}]},
    {"meta": {"type": ["annualOperatingCashFlow"]}, "annualOperatingCashFlow": [{"asOfDate": "2025-12-31", "reportedValue": {"raw": 50.0}}]},
    {"meta": {"type": ["annualCapitalExpenditure"]}, "annualCapitalExpenditure": [{"asOfDate": "2025-12-31", "reportedValue": {"raw": -20.0}}]},
    {"meta": {"type": ["quarterlyTotalRevenue"]}, "quarterlyTotalRevenue": [None, {"asOfDate": "2025-12-31", "reportedValue": {"raw": 30.0}}]},
]}}
_a157, _qq157 = _wl.parse_timeseries(_ts157)
from pf import research as _rs157
_ae157 = _rs157._enrich([dict(r) for r in _a157], "a")
_qs157 = {"quoteSummary": {"result": [{
    "financialData": {"operatingMargins": 0.3, "financialCurrency": "USD", "numberOfAnalystOpinions": 20, "targetMeanPrice": 120.0},
    "summaryDetail": {"dividendYield": 0.0123, "payoutRatio": 0.25},
    "assetProfile": {"sector": "Technology", "industry": "Software - Application", "country": "United States", "longBusinessSummary": "x" * 900},
    "earningsTrend": {"trend": [
        {"period": "+1y", "epsTrend": {"current": 5.0, "30daysAgo": 5.5, "90daysAgo": 6.25}, "epsRevisions": {"upLast30days": 1, "downLast30days": 9},
         "earningsEstimate": {"growth": 0.12, "numberOfAnalysts": 20}},
        {"period": "0q", "epsTrend": {"current": 1.0}}]},
    "price": {"currency": "USD", "longName": "Fixture Inc."}}]}}
_e157 = _wl.parse_summary(_qs157)
_m157 = _wl.metrics({"sector": "Technology", "px": 100.0, "mcap_usd": 1000.0, "hi52": 150.0, "fpe": 20.0, "fin_ccy": "USD"},
                    {"a": _ae157, "q": []}, _e157, {"USD": 1.0})
_ok157 = (len(_a157) == 2 and _a157[-1]["revenue"] == 110.0 and _a157[-1]["capex"] == 20.0 and len(_qq157) == 1
          and _ae157[-1]["fcf"] == 30.0 and _ae157[-1]["revenue_yoy"] == 10.0
          and _e157["info"]["dividendYield"] == 1.23 and len(_e157["info"]["longBusinessSummary"]) == 700
          and _e157["info"]["sector"] == "Technology" and _e157["trend+1y"]["current"] == 5.0 and "trend0q" not in _e157
          and _e157["rev+1y"]["downLast30days"] == 9.0 and _m157["est_ny_90"] == -20.0 and _m157["n_an"] == 20
          and _m157["eps_g_ny"] == 12.0 and _m157["fcf_yield"] == 3.0 and _m157["upside"] == 20.0)
print(f"157 Yahoo's answers          {ok(_ok157)}  revenue {_a157[-1]['revenue'] if _a157 else '?'} (not the 999 under another name), capex spent "
      f"{_a157[-1]['capex'] if _a157 else '?'}, free cash flow {_ae157[-1]['fcf'] if _ae157 else '?'}; yield {_e157['info'].get('dividendYield')}%; "
      f"next year's forecast {_m157.get('est_ny_90')}% in 90 days")

# 158 every check judges the number in front of it: by the sector's own bar,
#     by what a bank or a utility can carry, and not at all with too few analysts.
_m158 = {"kind": "std", "om": 25.0, "om_avg3": 27.0, "om_n": 3, "fcf_ttm": 100.0, "fcf_n": 4, "fcf_pos": 4,
         "fcf_margin": 10.0, "roic": 15.0, "net_debt": 300.0, "ebitda": 100.0, "int_cov": 10.0, "shares_chg": -2.0,
         "shares_yrs": 3, "rev_g_fy": 8.0, "rev_g_q": 6.0, "rev_cagr3": 9.0, "eps_g_ny": 12.0, "n_an": 10,
         "est_ny_90": 0.5, "eps_ny": 5.0, "eps_ny_90": 4.98, "est_cy_30": 0.0, "up30": 5, "dn30": 1, "fpe": 15.0,
         "fpe_ny": 15.0, "fpe_hi": 22.0, "peg": 1.25, "fcf_yield": 6.0, "dy": None, "payout": None,
         "div_cover": None, "fin_ccy": "USD"}
_row158, _ctx158 = {"sector": "Technology", "eps_fwd": 5.0}, {"pe_med": 20.0, "pe_label": "technology in the US"}
_ck158 = _wl.checks(_m158, _row158, _ctx158)
_s158 = {k: s for k, s, _ in _ck158}
_util158 = {k: s for k, s, _ in _wl.checks({**_m158, "kind": "util"}, {"sector": "Utilities", "eps_fwd": 5.0}, _ctx158)}
_fin158 = {k: s for k, s, _ in _wl.checks({**_m158, "kind": "fin", "roe": 14.0}, {"sector": "Financial Services", "eps_fwd": 5.0}, _ctx158)}
_few158 = {k: s for k, s, _ in _wl.checks({**_m158, "n_an": 2}, _row158, _ctx158)}
_var158 = [{k: s for k, s, _ in _wl.checks({**_m158, **v}, _row158, _ctx158)} for v in
           ({"fcf_pos": 3}, {"fcf_ttm": -5.0}, {"net_debt": -50.0}, {"fpe": 25.0}, {"om": 9.0})]
_ok158 = (_s158 == {"q_margin": "pass", "q_trend": "warn", "q_fcf": "pass", "q_roic": "pass", "q_debt": "warn",
                    "q_cover": "pass", "q_dilution": "pass", "g_rev": "pass", "g_rev_q": "pass", "g_rev3": "pass",
                    "g_eps_ny": "pass", "e_ny90": "pass", "e_cy30": "pass", "e_breadth": "pass", "v_pe": "pass",
                    "v_compress": "pass", "v_fcfy": "pass", "v_peg": "warn", "i_yield": "fail", "i_payout": "na", "i_cover": "na"}
          and _util158["q_debt"] == "pass" and _fin158.get("q_roe") == "pass"
          and not ({"q_fcf", "q_debt", "q_cover", "q_margin", "v_fcfy"} & set(_fin158))
          and _few158["e_ny90"] == _few158["e_breadth"] == "na"
          and [v.get(k) for v, k in zip(_var158, ("q_fcf", "q_fcf", "q_debt", "v_pe", "q_margin"))] == ["warn", "fail", "pass", "fail", "fail"])
print(f"158 the checks               {ok(_ok158)}  {sum(1 for s in _s158.values() if s == 'pass')} pass, "
      f"{sum(1 for s in _s158.values() if s == 'warn')} warn, {sum(1 for s in _s158.values() if s == 'fail')} fail; 3× EBITDA warns for a "
      f"tech company, passes for a utility; a bank is judged on its return on equity; two analysts are too few to judge")

# 159 the score and the presets: the same two companies rank one way under
#     Growth and the other under Value; a cut forecast makes a stock down for
#     a reason whatever it scores; the page's statuses follow the threshold.
_ckA159 = [["q_margin", "pass", ""], ["q_fcf", "pass", ""], ["q_debt", "pass", ""], ["g_rev", "pass", ""], ["g_rev_q", "pass", ""],
           ["g_eps_ny", "pass", ""], ["e_ny90", "pass", ""], ["v_pe", "fail", ""], ["v_peg", "fail", ""], ["i_yield", "fail", ""]]
_ckB159 = [["q_margin", "pass", ""], ["q_fcf", "pass", ""], ["q_debt", "pass", ""], ["g_rev", "fail", ""], ["g_rev_q", "warn", ""],
           ["g_eps_ny", "fail", ""], ["e_ny90", "pass", ""], ["v_pe", "pass", ""], ["v_peg", "pass", ""], ["i_yield", "pass", ""]]
_gA159, _gB159 = _wl.group_scores(_ckA159), _wl.group_scores(_ckB159)
_sc159 = {p: (_wl.preset_score(_gA159, p), _wl.preset_score(_gB159, p)) for p in _wl.PRESETS}
_jA159 = _wl.judge(_ckA159, _gA159, "value", None)
_jcut159 = _wl.judge([c if c[0] != "e_ny90" else ["e_ny90", "fail", ""] for c in _ckA159], _gA159, "growth", None)
# one counted check failing makes a high scorer close, not an opportunity; so do
# forecasts nobody could judge; a failed must-pass check makes it neither
_ckC159 = [["q_margin", "pass", ""], ["q_fcf", "pass", ""], ["q_debt", "pass", ""], ["g_rev", "pass", ""],
           ["g_rev3", "fail", ""], ["e_ny90", "pass", ""], ["v_pe", "pass", ""]]
_jC159 = _wl.judge(_ckC159, _wl.group_scores(_ckC159), "balanced", None)
_ckD159 = [c if c[0] not in ("g_rev3", "e_ny90") else [c[0], "na" if c[0] == "e_ny90" else "pass", ""] for c in _ckC159]
_jD159 = _wl.judge(_ckD159, _wl.group_scores(_ckD159), "balanced", None)
_ckE159 = [c if c[0] != "q_fcf" else ["q_fcf", "fail", ""] for c in _ckD159]
_ckE159 = [c if c[0] != "e_ny90" else ["e_ny90", "pass", ""] for c in _ckE159]
_jE159 = _wl.judge(_ckE159, _wl.group_scores(_ckE159), "balanced", None)
_ckF159 = [c if c[0] != "e_ny90" else ["e_ny90", "pass", ""] for c in _ckD159]
_jF159 = _wl.judge(_ckF159, _wl.group_scores(_ckF159), "balanced", None)
_br159 = [_wl.broken_reason(m) for m in ({"est_ny_90": -15.0, "n_an": 5}, {"est_ny_90": -10.0, "rev_g_q": 5.0, "n_an": 5},
                                          {"est_ny_90": -10.0, "rev_g_q": -2.0, "n_an": 5}, {"est_ny_90": -30.0, "n_an": 2},
                                          {"eps_ny": -1.0, "fcf_ttm": -5.0, "n_an": 0})]
def _row159(**kw):
    r = {"pr": {"dd52": -25.0}, "ck": [["q_fcf", "pass", ""]], "broken": None,
         "ps": {p: {"score": 80.0, "ok": True, "fails": []} for p in _wl.PRESETS}}
    r.update(kw)
    return r
_st159 = [_wl.status(_row159(), "balanced", 20), _wl.status(_row159(), "balanced", 30),
          _wl.status(_row159(broken="cut"), "balanced", 20),
          _wl.status(_row159(ps={p: {"score": 70.0, "ok": False, "close": True} for p in _wl.PRESETS}), "balanced", 20),
          _wl.status(_row159(ps={p: {"score": 80.0, "ok": False, "close": False} for p in _wl.PRESETS}), "balanced", 20),
          _wl.status(_row159(ck=None), "balanced", 20)]
_ok159 = (_sc159["growth"][0] > _sc159["growth"][1] and _sc159["value"][1] > _sc159["value"][0]
          and _gA159["value"] == 0 and _gB159["growth"] == 17 and not _jA159["ok"] and _jA159["must"] == []
          and set(_jA159["fails"]) == {"v_pe", "v_peg", "i_yield"}
          and _jcut159["must"] == ["e_ny90"] and not _jcut159["ok"] and not _jcut159["close"]
          and _jF159["ok"] and (_jC159["ok"], _jC159["close"]) == (False, True) and (_jD159["ok"], _jD159["close"]) == (False, True)
          and (_jE159["ok"], _jE159["close"]) == (False, False)
          and [b is not None for b in _br159] == [True, False, True, False, True]
          and _st159 == ["opp", "near", "reason", "close", "weak", "nodata"]
          and _wl.pullback_score(-15.0, 0) == 0 and _wl.pullback_score(-50.0, 100) == 100 and _wl.pullback_score(-32.5, 50) == 50)
print(f"159 the score and presets    {ok(_ok159)}  a grower against a cheap dividend payer: Growth {_sc159['growth'][0]} vs {_sc159['growth'][1]}, "
      f"Value {_sc159['value'][0]} vs {_sc159['value'][1]}; a 15% forecast cut is down for a reason, 10% only with shrinking revenue; "
      f"statuses {_st159}")

# 160 Yahoo, politely: one request at a time, spaced; a 429 is five minutes'
#     silence, twice at most, then the scan uses what it has; a fresh cache is
#     not asked again.
_stamp160 = []
for _ in range(3):
    _wl._paced(lambda: _stamp160.append(__import__("time").time()))
_gap160 = min(b - a for a, b in zip(_stamp160, _stamp160[1:]))
try:
    _wl._paced(lambda: (_ for _ in ()).throw(RuntimeError("Too Many Requests")))
    _rl160 = False
except _wl.RateLimited:
    _rl160 = True
_mem160, _calls160, _sleep160 = {}, {"n": 0}, []
def _ffin160(s):
    _calls160["n"] += 1
    if (s == "AAA" and _calls160["n"] == 1) or s == "CCC":
        raise _wl.RateLimited("429")
    return {"a": [{"end": "2025-12-31", "revenue": 1.0}], "q": []}
def _boom160(s):
    raise AssertionError("asked Yahoo for a fresh cache")
_keep160 = (_wl.store.cache_get, _wl.store.cache_put, _wl._load_fin, _wl._load_est)
try:
    _wl.store.cache_get = lambda k, max_age=None: ({**_mem160[k], "_age_s": 0} if k in _mem160 else None)
    _wl.store.cache_put = lambda k, v: _mem160.__setitem__(k, v)
    _wl._load_fin, _wl._load_est = _ffin160, (lambda s: {"info": {"sector": "Technology"}})
    _deep160, _err160, _cools160 = _wl.deep_all(["AAA", "BBB", "CCC", "DDD"], sleep=lambda s: _sleep160.append(s))
    _wl._load_fin = _wl._load_est = _boom160
    _again160, _aerr160, _ = _wl.deep_all(["AAA", "BBB"], sleep=lambda s: _sleep160.append(s))
finally:
    _wl.store.cache_get, _wl.store.cache_put, _wl._load_fin, _wl._load_est = _keep160
_ok160 = (_gap160 >= 0.36 and _rl160 and _sleep160 == [_wl.COOL_S, _wl.COOL_S] and _cools160 == 3
          and _deep160["AAA"][0].get("a") and set(_err160) == {"CCC", "DDD"} and "not read" in _err160["DDD"]
          and not _aerr160 and _again160["BBB"][1].get("info"))
print(f"160 Yahoo, politely          {ok(_ok160)}  requests {_gap160:.2f}s apart; a 429 raises; two five-minute pauses, then "
      f"{len(_err160)} stocks left unread rather than a third; a fresh cache is not asked again")

# 161-163 run on an empty database of their own.
from pf import store as _st161
_tmp161 = os.path.join(tempfile.gettempdir(), "pf_wlcheck.db")
for _x161 in (_tmp161, _tmp161 + "-wal", _tmp161 + "-shm"):
    if os.path.exists(_x161):
        os.remove(_x161)
_orig161 = _st161.DB_PATH
_st161.DB_PATH = _tmp161
try:
    _st161.init_db()

    # 161 the track record: a stock is written down the day it qualifies, at
    #     that close and its market's, closed the day it stops; its return is
    #     measured against the market over the same days.
    def _r161(sym, dd, okk, px):
        return {"sym": sym, "name": sym, "sector": "Technology", "region": "US", "px": px, "ccy": "USD",
                "pr": {"dd52": dd}, "ck": [["q_fcf", "pass", ""]], "broken": None,
                "ps": {p: {"score": 80.0 if okk else 40.0, "ok": okk, "fails": []} for p in _wl.PRESETS}}
    _s1_161 = {"scanned_at": _dtw(2026, 10, 5, 23, 0, tzinfo=_wl.AMS).timestamp(),
               "rows": [_r161("AAA", -30.0, True, 100.0), _r161("BBB", -25.0, False, 50.0)], "near": [],
               "bench": {"US": {"last": 6000.0}}}
    _s2_161 = {"scanned_at": _dtw(2026, 10, 6, 23, 0, tzinfo=_wl.AMS).timestamp(),
               "rows": [_r161("AAA", -10.0, True, 110.0), _r161("BBB", -26.0, True, 52.0)], "near": [],
               "bench": {"US": {"last": 6300.0}}}
    _wl.update_flags(_s1_161, 20.0)
    # CCC qualifies in the evening's scan and is gone by a rescan the same day: no row
    _wl.update_flags({**_s2_161, "rows": _s2_161["rows"] + [_r161("CCC", -40.0, True, 10.0)]}, 20.0)
    _wl.update_flags(_s2_161, 20.0)
    _new161 = _wl.new_since(_s1_161, _s2_161, "balanced", 20.0)
    _st161.cache_put("wl:scan", {**_s2_161, "new": _new161})
    _tr161 = _wl.track("balanced")
    _ta161 = next((x for x in _tr161["rows"] if x["sym"] == "AAA"), {})
    _tb161 = next((x for x in _tr161["rows"] if x["sym"] == "BBB"), {})
    _ok161 = (_ta161.get("active") == 0 and _ta161.get("first_d") == "2026-10-05" and _ta161.get("end_d") == "2026-10-06"
              and _ta161.get("ret") == 10.0 and _ta161.get("bench_ret") == 5.0 and _ta161.get("excess") == 5.0
              and _tb161.get("active") == 1 and _tb161.get("first_d") == "2026-10-06" and _tb161.get("first_px") == 52.0
              and _tr161["summary"]["n"] == 2 and _tr161["summary"]["active"] == 1 and _new161 == ["BBB"]
              and not any(x["sym"] == "CCC" for x in _tr161["rows"]))
    print(f"161 the track record         {ok(_ok161)}  AAA flagged 05/10 at 100, left 06/10: {_ta161.get('ret')}% against the market's "
          f"{_ta161.get('bench_ret')}%; BBB flagged 06/10 at {_tb161.get('first_px')}; new since the last scan: {_new161}")

    # 162 alerts and the brief read the last scan and nothing else: a stock new
    #     to the list, a pin at or below its buy price; the scan is due after
    #     22:45 on a weekday and not before.
    _wl.pin("BBB", note="fixture", buy=55.0)
    _wl.pin("AAA", buy=90.0)
    _st161.cache_put("wl:scan", {**_s2_161, "new": _new161, "scanned_at": __import__("time").time()})
    _al162 = _wl.alerts()
    _bl162 = _wl.brief_line() or ""
    _sat162 = _wl.last_slot(_dtw(2026, 10, 10, 12, 0, tzinfo=_wl.AMS))
    _satn162 = _wl.last_slot(_dtw(2026, 10, 10, 23, 30, tzinfo=_wl.AMS))
    _nxt162 = _wl.next_slot(_dtw(2026, 10, 9, 23, 0, tzinfo=_wl.AMS))
    _mon162 = _wl.last_slot(_dtw(2026, 10, 12, 8, 0, tzinfo=_wl.AMS))
    _tue162 = _wl.last_slot(_dtw(2026, 10, 13, 23, 0, tzinfo=_wl.AMS))
    _st161.cache_put("wl:scan", {"scanned_at": _dtw(2026, 10, 12, 23, 0, tzinfo=_wl.AMS).timestamp(), "rows": []})
    _due162 = (_wl.due(_dtw(2026, 10, 13, 8, 0, tzinfo=_wl.AMS)), _wl.due(_dtw(2026, 10, 13, 23, 0, tzinfo=_wl.AMS)))
    _ok162 = (any(a["code"] == "WCH" and "1 new" in a["text"] and "BBB" in a["text"] for a in _al162)
              and any(a["level"] == "warn" and a["text"].startswith("BBB at 52.00") for a in _al162)
              and not any(a["text"].startswith("AAA") for a in _al162)
              and "[BBB](/m/WCH#s=BBB)" in _bl162 and "buy price" in _bl162 and _bl162.startswith("Watchlist: 1 opportunity")
              and _sat162.isoformat()[:16] == "2026-10-09T22:45" and _satn162.isoformat()[:16] == "2026-10-09T22:45"
              and _mon162.isoformat()[:16] == "2026-10-09T22:45" and _nxt162.isoformat()[:16] == "2026-10-12T22:45"
              and _tue162.isoformat()[:16] == "2026-10-13T22:45" and _due162 == (False, True))
    print(f"162 alerts and the brief     {ok(_ok162)}  {len(_al162)} alerts from the last scan; “{_bl162[:90]}…”; "
          f"Saturday's slot is Friday 22:45; a Monday-night scan is due again Tuesday at 22:45, not that morning")

    # 163 Claude only on request: nothing in the scan or the scheduler writes a
    #     note, a stock the scan did not check is refused before any writer is
    #     asked, and the note may not tell anyone to buy or sell.
    from pf import watchnote as _wn163
    try:
        _wn163.start_note("ZZZZ")
        _ref163 = False
    except LookupError:
        _ref163 = True
    _src163 = _insp154.getsource(_wl)
    _ok163 = (_ref163 and "watchnote" not in _src163 and "start_note" not in _src163
              and "Do not tell the investor to buy or sell" in _wn163.SYSTEM and not _wn163.NOTE_JOBS)
    print(f"163 Claude only on request   {ok(_ok163)}  a stock outside the scan is refused before any writer is asked; the scan never "
          f"writes a note; the note may not say buy or sell")
finally:
    _st161.DB_PATH = _orig161
    for _x161 in (_tmp161, _tmp161 + "-wal", _tmp161 + "-shm"):
        try:
            os.remove(_x161)
        except OSError:
            pass

# 164 the page is wired: every route it calls exists, WCH and its words open
#     it, pins reach ERN's watchlist, the home screen and the brief carry its
#     alerts, the scheduler starts with the app, and wl_work/ stays out of git.
_wj164 = open(os.path.join(HERE, "backend", "static", "watch.js"), encoding="utf-8").read()
_need164 = {"/api/pf/wl", "/api/pf/wl/job", "/api/pf/wl/scan", "/api/pf/wl/prefs", "/api/pf/wl/pin", "/api/pf/wl/pin/{sym}",
            "/api/pf/wl/why/{sym}", "/api/pf/wl/track", "/api/pf/wl/notes", "/api/pf/wl/note/{sym}", "/api/pf/wl/note/{nid}"}
_app164 = open(os.path.join(HERE, "backend", "portfolio_app.py"), encoding="utf-8").read()
from pf import earnings as _ern164
_ok164 = (_need164 <= _routes and _app.BUILT_MODULES.get("WCH") == "watch.html"
          and all(u.replace("/{sym}", "/").replace("/{nid}", "/") in _wj164 for u in _need164)
          and 'code: "WCH",  title: "Watchlist",        href: "/m/WCH",       state: "live"' in _js
          and '"WATCH", "WATCHLIST", "EQS", "SCREEN"' in _js and 're: "WCH|WATCH"' in _js
          and "watch.pinned_syms" in _insp154.getsource(_ern164.watchlist)
          and "watch.alerts()" in _insp154.getsource(service.alerts)
          and 'b.get("wl")' in _insp154.getsource(_mo154.summary) and "wlmod.brief_line" in _insp154.getsource(_mo154.build)
          and "watch.ensure()" in _app164 and "\nwl_work/" in _gi154)
print(f"164 the watchlist is wired   {ok(_ok164)}  {len(_need164)} routes registered and called; WCH, WATCH and EQS open it; pins reach ERN; "
      f"the home screen and the brief carry its alerts; the scan starts with the app; wl_work/ is ignored")

# ---------------------------------------------------------------------------
# FCST — the forecast (build 26)
# ---------------------------------------------------------------------------
from pf import fcst as _fc
import math as _m165
import numpy as _np165

def _tab165(real, inf, bill, nxt=None):
    """A history of our own making: rows in order, one run unless `nxt` says otherwise."""
    n = len(real)
    return {"real": _np165.array(real, float), "inf": _np165.array(inf, float), "bill": _np165.array(bill, float),
            "nxt": _np165.array(nxt if nxt is not None else [i + 1 if i + 1 < n else -1 for i in range(n)]),
            "label": [("T", 2000 + i) for i in range(n)]}

def _one165(k):
    """Index and shocks for one path that walks rows 0, 1, ..., k-1."""
    return _np165.arange(k).reshape(k, 1), _np165.zeros((k, 1))

# 165 the history is built as described: each market's return after its own
#     inflation, the world weighted by the previous year's GDP in dollars,
#     the Dutch inflation and cash rate beside it, a hyperinflation year left
#     out, the first year of a series skipped rather than invented; and the
#     money test on the real data says what the page says.
_H165 = ["year", "country", "iso", "cpi", "xrusd", "eq_tr", "bill_rate", "money", "gdp"]
_r165 = [_H165]
for _y in range(1990, 2001):
    _i = _y - 1990
    _r165.append([_y, "A", "AAA", 100 * 1.02 ** _i, 1.0, 0.10, 0.03, 100 * 1.05 ** _i, 300.0])
    _r165.append([_y, "B", "NLD", 100 * 1.01 ** _i, 2.0, -0.05, 0.02, 100 * 1.03 ** _i, 200.0])
_r165[2 * 5 + 1][3] = 100 * 1.02 ** 4 * 1.9      # AAA in 1995: prices up 90% in a year, a hyperinflation
_p165 = _fc.seed_parts(_r165, min_n=1)
_a165, _w165 = _p165["countries"]["AAA"], _p165["world"]
# 1991: AAA real = 1.10/1.02 - 1, weight 300/1; NLD real = 0.95/1.01 - 1, weight 200/2
_exp165 = (300 * (1.10 / 1.02 - 1) + 100 * (0.95 / 1.01 - 1)) / 400
_seed165 = _fc.hist()
_ok165 = (_a165["years"][0] == 1991 and 1995 not in _a165["years"] and "AAA 1995" in _p165["summary"]["dropped"]
          and abs(_a165["real"][0] - (1.10 / 1.02 - 1)) < 1e-6
          and _w165["years"][0] == 1991 and abs(_w165["real"][0] - _exp165) < 1e-6
          and abs(_w165["inf"][0] - 0.01) < 1e-6 and abs(_w165["bill"][0] - (1.02 / 1.01 - 1)) < 1e-6
          and len(_seed165["countries"]) == 16 and _seed165["world"]["years"][0] <= 1872 and _seed165["world"]["years"][-1] == 2020
          and 0.05 < _seed165["summary"]["world"]["geo"] < 0.075
          and _seed165["thesis"]["corr"]["money_inf"] > 0.5 and _seed165["thesis"]["corr"]["money_real"] < 0
          and "NonCommercial-ShareAlike" in _seed165["_about"]["license"])
print(f"165 the history is built right {ok(_ok165)}  a made-up pair of markets gives the weighted real return "
      f"{_w165['real'][0]*100:.3f}% (expected {_exp165*100:.3f}%) and drops its 90%-inflation year; the seed: "
      f"{len(_seed165['countries'])} markets, {_seed165['world']['years'][0]}–{_seed165['world']['years'][-1]}, world "
      f"{_seed165['summary']['world']['geo']*100:.1f}% a year real; money vs inflation {_seed165['thesis']['corr']['money_inf']:+.2f}, "
      f"vs real stocks {_seed165['thesis']['corr']['money_real']:+.2f}")

# 166 the money adds up: a market that returns 5% after 2% inflation
#     compounds to exactly 1.05^k in today's euros and (1.05 x 1.02)^k in
#     euros of the day; with no return at all the value is exactly what was
#     put in, deposits growing 3% a year; costs take their share every year.
_t166 = _tab165([0.05] * 10, [0.02] * 10, [0.0] * 10)
_i166, _e166 = _one165(10)
_kw166 = dict(years=10, v0=1000.0, beta=1.0, s=0.0, box3="none", growth=0.0, dep_years=0)
_c166 = _fc.simulate(_t166, _i166, _e166, cost=0.0, monthly=0.0, **_kw166)
_flat166 = _tab165([0.0] * 10, [0.0] * 10, [0.0] * 10)
_z166 = _fc.simulate(_flat166, _i166, _e166, years=10, v0=1000.0, beta=1.0, s=0.0,
                     box3="none", cost=0.0, monthly=100.0, growth=0.03, dep_years=6)
_pin166 = _fc.put_in(1000.0, 100.0, 0.03, 6, 10)
_f166 = _fc.simulate(_flat166, _i166, _e166, cost=0.01, monthly=0.0, **_kw166)
_ok166 = (abs(_c166["real"][10][0] - 1000 * 1.05 ** 10) < 1e-6 and abs(_c166["nom"][10][0] - 1000 * (1.05 * 1.02) ** 10) < 1e-6
          and all(abs(_z166["real"][t][0] - _pin166[t]) < 1e-6 for t in range(11))
          and abs(_pin166[10] - (1000 + sum(1200 * 1.03 ** t for t in range(6)))) < 1e-6
          and abs(_f166["real"][10][0] - 1000 * 0.99 ** 10) < 1e-6)
print(f"166 the money adds up        {ok(_ok166)}  €1,000 at 5% real is €{_c166['real'][10][0]:,.2f} after ten years "
      f"(€{_c166['nom'][10][0]:,.2f} with 2% inflation); with no return it is exactly what went in, €{_pin166[10]:,.2f}; "
      f"1% costs leave €{_f166['real'][10][0]:,.2f}")

# 167 risk is scaled the way the page says: beta multiplies the market's
#     excess over cash, and the book's own swing adds risk but no return —
#     the average path is unchanged, the typical one ends lower (volatility
#     drag, about s²/2 a year).
_t167 = _tab165([0.05], [0.0], [0.01], nxt=[0])
_i167 = _np165.zeros((30, 20000), dtype=int)
_e167 = _np165.random.default_rng(7).standard_normal((30, 20000))
_kw167 = dict(v0=1.0, cost=0.0, monthly=0.0, growth=0.0, dep_years=0, box3="none")
_b167 = _fc.simulate(_t167, _i167[:1], _e167[:1], years=1, beta=2.0, s=0.0, **_kw167)
_s0 = _fc.simulate(_t167, _i167, _e167, years=30, beta=1.0, s=0.0, **_kw167)
_s2 = _fc.simulate(_t167, _i167, _e167, years=30, beta=1.0, s=0.2, **_kw167)
_mean167 = float(_s2["real"][1].mean())
_ratio167 = float(_np165.median(_s2["real"][30])) / float(_s0["real"][30][0])
_ok167 = (abs(_b167["real"][1][0] - 1.09) < 1e-9 and abs(_mean167 - 1.05) < 0.005
          and abs(_ratio167 - _m165.exp(-0.02 * 30)) < 0.03)
print(f"167 beta and the own swing  {ok(_ok167)}  beta 2 on a 5% market with 1% cash gives {(_b167['real'][1][0]-1)*100:.1f}%; "
      f"a 20% swing keeps the average year at {(_mean167-1)*100:.2f}% but the typical 30-year path ends "
      f"{(1-_ratio167)*100:.0f}% lower (s²/2 a year predicts {(1-_m165.exp(-0.6))*100:.0f}%)")

# 168 the paths walk history in runs: a path goes on to the next year of the
#     same market until its run ends or the dice say jump, never from one
#     country, or from 2020, into an unrelated year; the same seed gives the
#     same paths.
_tc168 = _fc.table(_seed165, "country")
_lab168 = _tc168["label"]
_bad168 = [i for i, j in enumerate(_tc168["nxt"])
           if j >= 0 and (_lab168[j][0] != _lab168[i][0] or _lab168[j][1] != _lab168[i][1] + 1)]
_ends168 = int((_tc168["nxt"] < 0).sum())
_d1, _ = _fc.draw(_tc168, 500, 50, block=1e12)
_d2, _ = _fc.draw(_tc168, 500, 50, block=1e12)
_nx168 = _tc168["nxt"][_d1[:-1]]
_jumps168 = int(((_d1[1:] != _nx168) & (_nx168 >= 0)).sum())
_ok168 = (not _bad168 and _ends168 >= 16 and _jumps168 == 0 and bool((_d1 >= 0).all()) and bool((_d1 == _d2).all())
          and _fc.table(_seed165, "world")["nxt"][-1] == -1)
print(f"168 paths walk in runs      {ok(_ok168)}  {len(_tc168['real'])} market-years in {_ends168} runs, none linked across a "
      f"country or a gap; without random jumps a path leaves a run only where it ends; the same seed draws the same paths")

# 169 the 2028 bill, as passed: 36% of the year's actual gain above €1,800
#     (€3,600 with a partner); a loss of €500 or more is carried forward and
#     set against later gains, a smaller one is not, nothing goes back.
_t169 = _tab165([0.10, -0.03, 0.05, -0.004, 0.02], [0.0] * 5, [0.0] * 5)
_i169, _e169 = _one165(5)
_kw169 = dict(years=5, v0=100000.0, beta=1.0, s=0.0, cost=0.0, monthly=0.0, growth=0.0, dep_years=0, first_year=2028)
_x169 = _fc.simulate(_t169, _i169, _e169, box3="bill", **_kw169)
_p169 = _fc.simulate(_t169, _i169, _e169, box3="bill", persons=2, **_kw169)
# by hand: 100,000 +10% = 110,000, tax .36 x (10,000 - 1,800) = 2,952, leaves 107,048;
#          -3%: a loss of 3,211.44, carried; +5%: the gain less 1,800 less the loss is taxed;
#          -0.4%: a loss of about 436, under 500, not carried, so the next +2% pays in full
_v1 = 110000 - 0.36 * 8200
_v2 = _v1 * 0.97
_l2 = _v1 - _v2
_v3 = _v2 * 1.05
_g3 = _v3 - _v2
_v3t = _v3 - 0.36 * max(0.0, _g3 - 1800 - _l2)
_v4 = _v3t * 0.996
_v5 = _v4 * 1.02 - 0.36 * max(0.0, _v4 * 0.02 - 1800)
_ok169 = (abs(_x169["nom"][1][0] - _v1) < 1e-6 and abs(_x169["nom"][3][0] - _v3t) < 1e-6
          and abs(_x169["nom"][4][0] - _v4) < 1e-6 and abs(_x169["nom"][5][0] - _v5) < 1e-6
          and abs(_p169["nom"][1][0] - (110000 - 0.36 * 6400)) < 1e-6)
print(f"169 the 2028 bill            {ok(_ok169)}  a €10,000 gain pays €{0.36*8200:,.0f} (€{0.36*6400:,.0f} with a partner); "
      f"a €{_l2:,.0f} loss is carried, leaving €{0.36*max(0.0, _g3-1800-_l2):,.2f} on the next €{_g3:,.0f} gain; "
      f"a €{_v3t*0.004:,.0f} loss is not carried")

# 170 today's system: 36% of a deemed 6% on what lies above €60,098, or of
#     the actual return when that was lower; nothing below the tax-free
#     amount. Under the 2028 setting the first year ahead, 2027, still pays
#     this way.
_t170 = _tab165([0.10], [0.0], [0.0])
_i170, _e170 = _one165(1)
_kw170 = dict(years=1, v0=100000.0, beta=1.0, s=0.0, cost=0.0, monthly=0.0, growth=0.0, dep_years=0, first_year=2027)
_n170 = _fc.simulate(_t170, _i170, _e170, box3="now", **_kw170)
_b170 = _fc.simulate(_t170, _i170, _e170, box3="bill", **_kw170)
_l170 = _fc.simulate(_tab165([-0.05], [0.0], [0.0]), _i170, _e170, box3="now", **_kw170)
_lo170 = _fc.simulate(_tab165([0.02], [0.0], [0.0]), _i170, _e170, box3="now", **_kw170)
_s170 = _fc.simulate(_t170, _i170, _e170, box3="now", **{**_kw170, "v0": 50000.0})
_frac170 = (100000 - 60098) / 100000
_tax170 = 0.36 * 0.06 * 100000 * _frac170
_ok170 = (abs(110000 - _n170["nom"][1][0] - _tax170) < 1e-6 and abs(_b170["nom"][1][0] - _n170["nom"][1][0]) < 1e-9
          and abs(_l170["nom"][1][0] - 95000) < 1e-9 and abs(102000 - _lo170["nom"][1][0] - 0.36 * 2000 * _frac170) < 1e-6
          and abs(_s170["nom"][1][0] - 55000) < 1e-9)
print(f"170 today's Box 3 system    {ok(_ok170)}  €100,000 pays €{_tax170:,.2f} on a deemed 6% above €60,098; a 2% year pays on "
      f"the 2%; a losing year and a book below the tax-free amount pay nothing; 2027 is taxed this way under the 2028 setting too")

# 171 today's high prices, when asked for: the market's return is lowered by
#     what the CAPE regression predicts against the average, for ten years
#     and not the eleventh; and at today's CAPE that is a cut, not a boost.
_t171 = _tab165([0.05] * 12, [0.0] * 12, [0.0] * 12)
_i171, _e171 = _one165(12)
_d171 = _fc.val_delta({"pred10": 0.01, "avg10": 0.065})
_v171 = _fc.simulate(_t171, _i171, _e171, years=12, v0=1.0, beta=1.0, s=0.0, cost=0.0, monthly=0.0, growth=0.0,
                     dep_years=0, box3="none", delta=_d171)
_y10 = _v171["real"][10][0] / _v171["real"][9][0]
_y11 = _v171["real"][11][0] / _v171["real"][10][0]
_ok171 = (abs(_y10 - 1.05 * 1.01 / 1.065) < 1e-9 and abs(_y11 - 1.05) < 1e-9
          and _fc.val_delta(_seed165["cape"]) < -0.03 and _fc.val_delta(None) == 0.0)
print(f"171 today's high prices      {ok(_ok171)}  a 1% prediction against a 6.5% average turns 5% years into {(_y10-1)*100:.2f}% "
      f"for ten years, then 5% again; at the seed's CAPE of {_seed165['cape']['value']:.0f} the cut is "
      f"{(1-_m165.exp(_fc.val_delta(_seed165['cape'])))*100:.1f}% a year")

# 172 the book's risk is measured, not assumed: a weekly series built as
#     1.3 times the market plus noise of its own comes back as beta 1.3 and
#     that noise.
_rng172 = _np165.random.default_rng(3)
_x172 = list(_rng172.normal(0, 0.02, 2000))
_u172 = list(_rng172.normal(0, 0.01, 2000))
_f172 = _fc._fit([1.3 * a + b for a, b in zip(_x172, _u172)], _x172)
_ok172 = (bool(_f172) and abs(_f172["beta"] - 1.3) < 0.05 and abs(_f172["res_sd"] - 0.01) < 0.001
          and _fc._fit([1.0] * 10, list(range(10))) is None)
print(f"172 the book's risk measured {ok(_ok172)}  1.3 x market + 1% noise comes back as beta {_f172['beta']:.3f}, own swing "
      f"{_f172['res_sd']*100:.2f}%; fewer than 40 weeks gives no answer")

# 173 the track record: a range is kept at most once a week, and a year later
#     the book's actual time-weighted return is placed in it.
import tempfile as _tf173
from datetime import date as _d173, timedelta as _td173
_tmp173 = os.path.join(_tf173.gettempdir(), "pf_fccheck.db")
for _x in (_tmp173, _tmp173 + "-wal", _tmp173 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_orig173 = store.DB_PATH
store.DB_PATH = _tmp173
_ok173 = False
try:
    store.init_db()
    _base173 = {"value": 1000.0, "cost_pct": 0.5, "risk": {"available": True, "beta": 1.2, "s": 0.08}}
    _old173 = _d173(2025, 9, 1)
    _s1 = _fc.snapshot(_base173, today=_old173)
    _s2b = _fc.snapshot(_base173, today=_old173 + _td173(days=3))
    _s3 = _fc.snapshot(_base173, today=_old173 + _td173(days=8))
    _q173 = _s1["q"]
    _curve173 = [{"d": "2025-09-01", "x": 1.0}, {"d": "2026-09-01", "x": 1.0 + (_q173["p25"] + _q173["p50"]) / 2},
                 {"d": "2026-09-09", "x": 5.0}]
    _tr173 = _fc.track(curve=_curve173, today=_d173(2026, 9, 5))
    _ok173 = (bool(_s1) and _s2b is None and bool(_s3) and _q173["p5"] < _q173["p50"] < _q173["p95"]
              and len(_tr173["rows"]) == 1 and _tr173["rows"][0]["band"] == "25–50%" and _tr173["n"] == 2
              and _tr173["next_due"] == "2026-09-09" and _fc.band_of(-9.0, _q173) == "below 5%"
              and _fc.band_of(9.0, _q173) == "above 95%")
finally:
    store.DB_PATH = _orig173
    for _x in (_tmp173, _tmp173 + "-wal", _tmp173 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
print(f"173 the track record        {ok(_ok173)}  one range a week (a second one three days later is skipped); a year on, a return "
      f"between the 25th and 50th percentile is reported as such; the next comparison falls a year after the next range")

# 174 the run the page asks for: every horizon you asked about, percentiles
#     in order, what was put in matching the deposits, the three Box 3
#     regimes side by side, and settings outside their range clipped.
_bp174 = {"value": 27500.0, "cost_pct": 1.0, "risk": {"available": True, "beta": 1.3, "s": 0.09}, "cape": _seed165["cape"]}
_r174 = _fc.run({"monthly": 150, "growth": 3, "hist": "nonsense", "box3": "bill"}, _bp174, n=2000)
_q174 = _fc.params({"monthly": -50, "dep_years": 99, "risk": "index"}, _bp174)
_rows174 = {r["y"]: r for r in _r174["rows"]}
_ord174 = all(r["real"]["p5"] <= r["real"]["p25"] <= r["real"]["p50"] <= r["real"]["p75"] <= r["real"]["p95"]
              for r in _r174["rows"])
_r30 = _rows174[30]
_ok174 = (sorted(_rows174) == [1, 2, 5, 10, 20, 30, 50] and _ord174 and len(_r174["fan"]) == 51
          and abs(_r30["put_in"] - _fc.put_in(27500, 150, 0.03, 50, 50)[30]) < 0.01      # rounded to the cent
          and _r174["params"]["hist"] == "world" and _r174["params"]["beta"] == 1.3 and _r174["params"]["cost"] == 1.0
          and _r30["box3_by"]["bill"] < _r30["box3_by"]["none"] and _r30["box3_by"]["now"] <= _r30["box3_by"]["none"]
          and _q174["monthly"] == 0 and _q174["dep_years"] == 50 and _q174["beta"] == 1.0 and _q174["cost"] == _fc.INDEX_COST)
print(f"174 the run the page shows   {ok(_ok174)}  7 horizons, percentiles in order; in 30 years typically €{_r30['real']['p50']:,.0f} "
      f"from €{_r30['put_in']:,.0f} put in; Box 3 bill €{_r30['box3_by']['bill']:,.0f}, today's system "
      f"€{_r30['box3_by']['now']:,.0f}, none €{_r30['box3_by']['none']:,.0f}; a negative deposit becomes 0")

# 175 the page is wired: both routes exist and are called, FCST and its words
#     open it, the track-record table exists, the history names its source.
#     Since build 32 it is MVO's second tab: /m/MVO/forecast, with /m/FCST
#     sent there, and FCST no longer a module of its own.
_fj175 = open(os.path.join(HERE, "backend", "static", "fcst.js"), encoding="utf-8").read()
_need175 = {"/api/pf/fcst", "/api/pf/fcst/run"}
with store.conn() as _c175:
    _tbl175 = {r[0] for r in _c175.execute("SELECT name FROM sqlite_master WHERE type='table'")}
_ok175 = (_need175 <= _routes and all(f'"{u}"' in _fj175 for u in _need175)
          and "FCST" not in _app.BUILT_MODULES and {"/m/MVO/forecast", "/m/FCST"} <= _routes
          and os.path.exists(os.path.join(HERE, "backend", "static", "fcst.html"))
          and 'code: "FCST"' not in _js and '"FCST", "FORECAST", "PROJECTION"' in _js
          and 'location.assign("/m/MVO/forecast")' in _js and "fc_snap" in _tbl175
          and os.path.exists(_fc.SEED_PATH) and "Jordà" in _seed165["_about"]["cite"])
print(f"175 the forecast is wired    {ok(_ok175)}  {len(_need175)} routes registered and called; FCST, FORECAST and PROJECTION "
      f"open MVO's forecast tab, and /m/FCST is sent there; the track-record table exists; the history file names its source and licence")

# ---------------------------------------------------------------------------
# WCH additions (build 27): the market's mood, who a fall belongs to, the
# higher bar, and the momentum leaders
# ---------------------------------------------------------------------------
from pf import watch as _w176
import math as _m176

# 176 the mood: calm while the median company is less than 10% below its
#     52-week high, a correction to 15%, a sell-off beyond; no reading from
#     fewer than thirty companies.
_calm176 = _w176.mood_of([-2.0] * 20 + [-6.0] * 21 + [-30.0] * 9)
_corr176 = _w176.mood_of([-12.0] * 40)
_sell176 = _w176.mood_of([-16.0] * 25 + [-40.0] * 15)
_ok176 = (_calm176["state"] == "calm" and _calm176["median_dd"] == -6.0 and _calm176["share20"] == 18
          and _corr176["state"] == "correction" and _sell176["state"] == "sell-off" and _sell176["share20"] == 38
          and _w176.mood_of([-50.0] * 29) is None and _w176.mood_of([-10.0] * 40)["state"] == "correction")
print(f"176 the market's mood        {ok(_ok176)}  median −6% is calm (18% of companies 20%+ down), −12% a correction, −16% a sell-off; "
      f"exactly −10% already counts as a correction; 29 companies give no reading")

# 177 who a fall belongs to: the market's share of the stock's fall in log
#     terms; a quarter or more is the market's, then the sector's (its index's
#     whole move), otherwise the company's own; a rise has no owner.
_tm177 = _w176.fall_tag({"stock": -40.0, "market": -20.0, "sector": 0.0, "company": -25.0})
_ts177 = _w176.fall_tag({"stock": -40.0, "market": -5.0, "sector": -15.0, "company": -25.7})
_to177 = _w176.fall_tag({"stock": -40.0, "market": 5.0, "sector": 0.0, "company": -42.9})
_tn177 = _w176.fall_tag({"stock": -40.0, "market": -8.0, "sector": None, "company": -34.8})
_ok177 = (_tm177["tag"] == "market" and _tm177["market_share"] == round(_m176.log(0.8) / _m176.log(0.6) * 100)
          and _ts177["tag"] == "sector" and _ts177["sector_share"] == round(_m176.log(0.95 * 0.85) / _m176.log(0.6) * 100)
          and _to177["tag"] == "own" and _to177["market_share"] == 0 and _tn177["tag"] == "own"
          and _w176.fall_tag({"stock": 5.0, "market": -3.0, "sector": None, "company": 8.0}) is None and _w176.fall_tag(None) is None)
print(f"177 who a fall belongs to    {ok(_ok177)}  −40% with the market −20%: market's ({_tm177['market_share']}%); with its sector index "
      f"−19%: sector's ({_ts177['sector_share']}%); with the market up, or down 8% and no sector: the company's own")

# 178 the higher bar: a fall of its own in a calm market needs 85, not 75 —
#     a score of 80 is then close, not an opportunity; any other mood, any
#     other owner, or a stock not deep enough to be checked keeps the 75.
_ck178 = [["e_ny90", "pass", ""], ["q_fcf", "pass", ""], ["q_debt", "pass", ""]]
_gs178 = {"quality": 80.0, "growth": 80.0, "estimates": 80.0, "value": 80.0}
_j0 = _w176.judge(_ck178, _gs178, "balanced", None)
_j1 = _w176.judge(_ck178, _gs178, "balanced", None, strict=True)
_own178 = {"tag": "own"}
_ok178 = (_j0["ok"] and _j0["bar"] == 75 and not _j1["ok"] and _j1["close"] and _j1["bar"] == 85 and _j1["score"] == 80.0
          and _w176.strict_for(-25.0, _own178, "calm") and not _w176.strict_for(-25.0, _own178, "correction")
          and not _w176.strict_for(-25.0, {"tag": "sector"}, "calm") and not _w176.strict_for(-8.0, _own178, "calm")
          and not _w176.strict_for(None, _own178, "calm"))
print(f"178 the higher bar           {ok(_ok178)}  a score of 80 is an opportunity at the usual 75 and close at {_j1['bar']}; the 85 applies only to a "
      f"checked dip of its own in a calm market")

# 179 momentum as the test measured it: the close 30 days ago against the
#     close a year ago, in calendar days; the plain twelve-month return beside
#     it; nothing without a full year of history.
from datetime import date as _d179, timedelta as _td179
_days179 = [(_d179(2025, 1, 1) + _td179(days=i)).isoformat() for i in range(500)]
_c179 = [100.0 + i for i in range(500)]
_mom179, _r12_179 = _w176.momentum(_days179, _c179)
_last = _d179.fromisoformat(_days179[-1])
_p12 = _c179[_days179.index((_last - _td179(days=365)).isoformat())]
_p1 = _c179[_days179.index((_last - _td179(days=30)).isoformat())]
_ok179 = (abs(_mom179 - round((_p1 / _p12 - 1) * 100, 2)) < 1e-9 and abs(_r12_179 - round((_c179[-1] / _p12 - 1) * 100, 2)) < 1e-9
          and _w176.momentum(_days179[-300:], _c179[-300:]) == (None, None)
          and _w176.price_metrics(_days179, _c179)["mom"] == _mom179)
print(f"179 momentum, 12-1           {ok(_ok179)}  a steady climber: {_mom179:+.2f}% from a year ago to a month ago, {_r12_179:+.2f}% over the year; "
      f"300 days of history give none")

# 180 the leaders: each region ranked on its own, strongest first, cut at
#     thirty, nothing without a momentum figure; a leader's business is
#     weakening when it is down for a reason, an estimates check fails, or
#     its debt does.
_rows180 = [{"sym": f"U{i}", "region": "US", "mcap_usd": 1000 - i} for i in range(40)] + \
           [{"sym": f"E{i}", "region": "EU", "mcap_usd": 50} for i in range(5)] + [{"sym": "UX", "region": "US", "mcap_usd": 5000}]
_pm180 = {**{f"U{i}": {"mom": float(i)} for i in range(40)}, **{f"E{i}": {"mom": float(-i)} for i in range(5)}, "UX": {"mom": None}}
_L180 = _w176.rank_leaders(_rows180, _pm180)
_L180b = _w176.rank_leaders(_rows180, _pm180, pool=6)          # the six largest US: UX (no momentum), U0..U4
_wk_ok = _w176.lead_weak({"ck": [["e_ny90", "pass", "+2%"], ["q_debt", "pass", "0.5x"]]})
_wk_cut = _w176.lead_weak({"ck": [["e_cy30", "fail", "-9% in 30 days"], ["q_debt", "fail", "5.1x"]]})
_wk_br = _w176.lead_weak({"broken": "analysts cut next year's earnings forecast by 20% in 90 days", "ck": [["e_ny90", "fail", "-20%"]]})
_ok180 = (len(_L180["US"]) == 30 and _L180["US"][0] == "U39" and _L180["US"][-1] == "U10" and "UX" not in _L180["US"]
          and _L180["EU"] == ["E0", "E1", "E2", "E3", "E4"] and _L180b["US"] == ["U4", "U3", "U2", "U1", "U0"] and _wk_ok == []
          and len(_wk_cut) == 2 and _wk_cut[0].startswith("this year's forecast, 30 days") and _wk_cut[1].startswith("debt")
          and _wk_br == ["analysts cut next year's earnings forecast by 20% in 90 days"])
print(f"180 the momentum leaders     {ok(_ok180)}  US ranked {_L180['US'][0]}…{_L180['US'][-1]} (30 of 40), Europe on its own, only among the largest (of the six largest: {', '.join(_L180b['US'])}); a forecast cut "
      f"and heavy debt mark a leader weakening, a reason to be down replaces the estimates lines")

# 181–182 need a database of their own
import tempfile as _tf181
_tmp181 = os.path.join(_tf181.gettempdir(), "pf_wl27check.db")
for _x in (_tmp181, _tmp181 + "-wal", _tmp181 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_orig181 = store.DB_PATH
store.DB_PATH = _tmp181
_ok181 = _ok182 = False
try:
    store.init_db()
    # 181 who entered and left the top ten reaches the home screen and the
    #     brief, with the market's mood; the first scan has nothing to compare.
    _new181, _out181 = _w176.lead_changes({"US": ["A", "B", "C"], "EU": ["X"]}, {"US": ["A", "D", "C"], "EU": ["X"]})
    _first181 = _w176.lead_changes(None, {"US": ["A"]})
    import time as _t181
    store.cache_put("wl:scan", {"scanned_at": _t181.time(), "rows": [{"sym": "ZZ", "pr": {"dd52": -2.0}}], "near": [], "new": [],
                                "lead_new": _new181, "lead_out": _out181,
                                "mood": {"US": {"state": "calm", "median_dd": -6.0, "n": 900, "share20": 15}, "EU": None}})
    _al181 = _w176.alerts()
    _br181 = _w176.brief_line() or ""
    _ok181 = (_new181 == {"US": ["D"]} and _out181 == {"US": ["B"]} and _first181 == ({}, {})
              and any(a["text"].startswith("Momentum top 10, new: D (US)") for a in _al181)
              and "momentum top 10, US: new [D](/m/WCH#s=D), dropped B" in _br181 and "market mood: US calm" in _br181)

    # 182 the momentum track record holds a quarter at a time: the top ten
    #     written down at the quarter's first scan, carried along within it,
    #     closed at the next quarter's first scan and replaced.
    def _res182(t, leaders, px):
        return {"scanned_at": t, "rows": [{"sym": s, "px": p, "name": s, "sector": "Technology", "ccy": "USD"} for s, p in px.items()],
                "near": [], "bench": {"US": {"last": 100.0}, "EU": {"last": 50.0}}, "leaders": leaders}
    _L = {"US": [f"S{i}" for i in range(12)], "EU": ["E1"]}
    _px = {s: 10.0 for s in _L["US"] + _L["EU"]}
    _w176.update_leaders(_res182(0, _L, _px), d="2026-10-12")
    _w176.update_leaders(_res182(0, _L, {**_px, "S0": 12.0}), d="2026-11-02")
    with store.conn() as _c182:
        _a1 = [dict(r) for r in _c182.execute("SELECT * FROM wl_flag WHERE preset='momentum'")]
    _L2 = {"US": ["S0", "N1"] + [f"S{i}" for i in range(2, 12)], "EU": ["E1"]}
    _w176.update_leaders(_res182(0, _L2, {**_px, "S0": 13.0, "N1": 20.0}), d="2027-01-04")
    with store.conn() as _c182:
        _a2 = [dict(r) for r in _c182.execute("SELECT * FROM wl_flag WHERE preset='momentum' ORDER BY first_d, sym")]
    _old = [f for f in _a2 if f["first_d"] == "2026-10-12"]
    _cur = [f for f in _a2 if f["first_d"] == "2027-01-04"]
    _s0 = next(f for f in _old if f["sym"] == "S0")
    _tr182 = _w176.track(_w176.MOM_PRESET)
    _ok182 = (len(_a1) == 11 and all(f["active"] for f in _a1) and next(f for f in _a1 if f["sym"] == "S0")["last_px"] == 12.0
              and all(not f["active"] and f["end_d"] == "2027-01-04" for f in _old) and _s0["end_px"] == 13.0
              and len(_cur) == 11 and {"N1", "S0"} <= {f["sym"] for f in _cur} and "S10" not in {f["sym"] for f in _cur}
              and next(f for f in _cur if f["sym"] == "N1")["score"] == 2 and _tr182["summary"]["n"] == 22)
finally:
    store.DB_PATH = _orig181
    for _x in (_tmp181, _tmp181 + "-wal", _tmp181 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
print(f"181 leaders in the alerts    {ok(_ok181)}  D entered and B left the US top ten: on the home screen and in the brief, with the US mood; "
      f"the first scan compares with nothing")
print(f"182 the momentum record      {ok(_ok182)}  ten US and one European written down on 12 October, carried along in November, closed on "
      f"4 January at that day's price and replaced by the new ten; held as a quarter each")

# 183 the page is wired: the mood, the tags, the momentum column and the
#     leaders panel are drawn; the track record asks for the momentum rows;
#     an older scan is redone on the next visit; the build moved.
_wj183 = open(os.path.join(HERE, "backend", "static", "watch.js"), encoding="utf-8").read()
_wh183 = open(os.path.join(HERE, "backend", "static", "watch.html"), encoding="utf-8").read()
import re as _re183
_head183 = _wj183[_wj183.index("const head = `<thead>"):_wj183.index("</tr></thead>`;", _wj183.index("const head = `<thead>"))]
_trow183 = _wj183[_wj183.index("function trow(r)"):_wj183.index("function wireRows")]
from pf import api as _api183
_src183 = _insp154.getsource(_api183.wl_view)
try:
    _api183.wl_track(_w176.MOM_PRESET)
    _trk183 = True
except Exception:
    _trk183 = False
_ok183 = (len(_re183.findall(r"<th[ >]", _head183)) == len(_re183.findall(r"<td[ >]", _trow183)) == 13
          and "renderMoods(S)" in _wj183 and "renderMomentum()" in _wj183 and 'id="s-mom"' in _wj183 and "ftag(r)" in _trow183
          and "V.rules.mom_preset" in _wj183 and ".ftag{" in _wh183 and ".mchip{" in _wh183
          and "watch.SCAN_VER" in _src183 and _trk183 and _BUILD >= 27)
print(f"183 the additions are wired   {ok(_ok183)}  13 columns in the header and the row; mood, tags, momentum panel and its track record "
      f"drawn; the momentum track route answers; a scan from before build 27 is redone on the next visit")

# ---------------------------------------------------------------------------
# MVO, Markowitz (build 28): the optimiser, the frontier, Black-Litterman,
# reverse optimisation, costs from the ledger, the deposit plan, and the
# home screen's constellation
# ---------------------------------------------------------------------------
from pf import mvo as _mv
import numpy as _np184

# 184 the optimiser: the projection lands on the capped simplex exactly; two
#     uncorrelated assets with returns 10% and 5%, variances 4% and 1%, at a
#     risk aversion of 2 split 70/30 (worked by hand); capped at 60% the first
#     takes its cap; the corner of the frontier fills the best returns first.
_S184 = _np184.diag([0.04, 0.01])
_mu184 = _np184.array([0.10, 0.05])
_w184 = _mv.solve(_mu184, _S184, 2.0, _np184.array([1.0, 1.0]))
_c184 = _mv.solve(_mu184, _S184, 2.0, _np184.array([0.6, 1.0]))
_p184 = _mv.project(_np184.array([0.9, 0.5, -0.2, 0.4]), _np184.array([0.5, 0.5, 0.5, 0.5]))
_m184 = _mv.max_return(_np184.array([0.03, 0.09, 0.07, 0.01]), _np184.array([0.4, 0.4, 0.4, 0.4]))
_ok184 = (abs(_w184[0] - 0.7) < 1e-6 and abs(_w184[1] - 0.3) < 1e-6 and abs(_c184[0] - 0.6) < 1e-6
          and abs(_p184.sum() - 1) < 1e-12 and float(_p184.max()) <= 0.5 + 1e-12 and float(_p184.min()) >= 0
          and abs(_p184[0] - 0.5) < 1e-9 and abs(_p184[2]) < 1e-12
          and list(_np184.round(_m184, 6)) == [0.2, 0.4, 0.4, 0.0])
print(f"184 the optimiser            {ok(_ok184)}  10% and 5% returns, 4% and 1% variance, aversion 2: {_w184[0]:.3f}/{_w184[1]:.3f} "
      f"(by hand 0.7/0.3); capped at 60%: {_c184[0]:.3f}; the corner fills 9%, 7%, then 3% to the 40% caps")

# 185 the frontier: with the market's own returns (cash plus beta times the
#     premium) and the market in the universe, the best mix per unit of risk
#     is the market, 100%; the line rises; the mix at a given risk has that
#     risk, and the least-risk mix for a return earns it.
_f185 = _np184.array([1.0, 1.4, 0.6, 0.0])                    # betas to the market
_sm185 = 0.15
_S185 = _np184.outer(_f185, _f185) * _sm185 ** 2 + _np184.diag([0.0, 0.25 ** 2, 0.2 ** 2, 0.05 ** 2])
_pi185, _b185 = _mv.prior(_S185, 0, 0.02, 0.06)
_u185 = _np184.array([1.0, 0.5, 0.5, 1.0])
_fr185 = _mv.frontier(_pi185, _S185, _u185)
_t185 = _mv.tangency(_pi185, _S185, _u185, 0.02, _fr185)
_v185 = _mv.at_vol(_pi185, _S185, _u185, 0.20, _fr185)
_r185 = _mv.at_ret(_pi185, _S185, _u185, 0.07, _fr185)
_rets185 = [p["ret"] for p in _fr185]
_ok185 = (abs(_t185["w"][0] - 1.0) < 2e-3 and all(b > a for a, b in zip(_rets185, _rets185[1:]))
          and abs(_v185["vol"] - 0.20) < 5e-4 and abs(_r185["ret"] - 0.07) < 5e-4
          and abs(_pi185[1] - (0.02 + 1.4 * 0.06)) < 1e-9 and abs(_pi185[3] - 0.02) < 1e-9)
print(f"185 the frontier             {ok(_ok185)}  with the market's returns the best mix per unit of risk is the market "
      f"({_t185['w'][0] * 100:.1f}%); the line rises; asked for 20% risk it gives {_v185['vol'] * 100:.2f}%, for a 7% return {_r185['ret'] * 100:.2f}%")

# 186 Black-Litterman: one view moves its own holding its confidence's share
#     of the way to the view, an unrelated holding not at all, and a related
#     one by its covariance with the viewed one; no view leaves the market's.
_S186 = _np184.array([[0.04, 0.012, 0.0], [0.012, 0.09, 0.0], [0.0, 0.0, 0.02]])
_pi186 = _np184.array([0.08, 0.10, 0.04])
_po186 = _mv.posterior(_pi186, _S186, [(0, 0.16, 0.5)])
_ok186 = (abs(_po186[0] - 0.12) < 1e-9 and abs(_po186[2] - 0.04) < 1e-12
          and abs(_po186[1] - (0.10 + 0.5 * 0.08 * 0.012 / 0.04)) < 1e-9
          and _np184.allclose(_mv.posterior(_pi186, _S186, []), _pi186)
          and abs(_mv.posterior(_pi186, _S186, [(0, 0.16, _mv.CONF["high"])])[0] - 0.14) < 1e-9)
print(f"186 Black-Litterman views    {ok(_ok186)}  8% held, a view of 16% at middling confidence: {_po186[0] * 100:.1f}% (half way); "
      f"its correlated neighbour {_pi186[1] * 100:.0f}% → {_po186[1] * 100:.1f}%; an unrelated one stays {_po186[2] * 100:.0f}%; high confidence 14%")

# 187 reverse optimisation: the returns a book's weights imply, fed back to
#     the optimiser at the same risk aversion, give back exactly that book.
_wb187 = _np184.array([0.5, 0.3, 0.2])
_im187 = _mv.implied(_S186, _wb187, 0.02, 3.0)
_back187 = _mv.solve(_im187, _S186, 3.0, _np184.ones(3))
_ok187 = _np184.allclose(_back187, _wb187, atol=1e-6) and abs(_im187[0] - (0.02 + 3.0 * float(_S186[0] @ _wb187))) < 1e-12
print(f"187 what your weights imply  {ok(_ok187)}  a 50/30/20 book's implied returns put back into the optimiser return "
      f"{'/'.join(f'{x * 100:.0f}' for x in _back187)}")

# 188 costs from the ledger: the median fee of the last year's buys per
#     exchange, the currency charge as a share of a dollar order; under three
#     buys the default stands; a buy older than a year is ignored.
from types import SimpleNamespace as _NS188
from datetime import datetime as _dt188, date as _date188
_tx188 = ([_NS188(kind="BUY", trade_dt=_dt188(2026, 9, d), exchange="NDQ", currency="USD", fee_eur=-2.0,
                  autofx_cost_eur=-0.25 * v / 100, value_eur=-v) for d, v in ((1, 200.0), (2, 400.0), (3, 300.0))]
          + [_NS188(kind="BUY", trade_dt=_dt188(2024, 1, 5), exchange="NSY", currency="USD", fee_eur=-9.0,
                    autofx_cost_eur=-5.0, value_eur=-500.0),
             _NS188(kind="BUY", trade_dt=_dt188(2026, 8, 1), exchange="EAM", currency="EUR", fee_eur=-7.0,
                    autofx_cost_eur=0.0, value_eur=-300.0),
             _NS188(kind="SELL", trade_dt=_dt188(2026, 8, 2), exchange="XET", currency="EUR", fee_eur=-9.0,
                    autofx_cost_eur=0.0, value_eur=300.0)])
_fe188 = _mv.fees(_tx188, _date188(2026, 10, 13))
_ok188 = (_fe188["US"]["fee"] == 2.0 and abs(_fe188["US"]["fx"] - 0.0025) < 1e-9 and _fe188["US"]["n"] == 3
          and _fe188["US"]["from"] == "ledger" and _fe188["EAM"]["fee"] == 3.0 and _fe188["EAM"]["from"] == "default"
          and _fe188["XET"]["n"] == 0 and _mv.venue_class("NSY", "USD") == "US" and _mv.venue_class(None, "USD") == "US"
          and _mv.venue_class("XET", "EUR") == "XET" and _mv.venue_class("SCG", "EUR") == "OTHER"
          and abs(_mv.order_cost(_fe188["US"], 400.0) - 3.0) < 1e-9)
print(f"188 costs from the ledger    {ok(_ok188)}  three New York buys this year at €2 and 0.25%: €{_fe188['US']['fee']:.2f} + "
      f"{_fe188['US']['fx'] * 100:.2f}% (the 2024 one at €9 ignored); one Amsterdam buy keeps DEGIRO's usual €3; a sale is not a buy")

# 189 the deposit plan: whole shares, buy-only, never above the cost limit
#     or the cash in hand; the gap to the target never widens; the overweight
#     holding is never bought; with €100 a month and a €1 fee the first month
#     waits, as no order of €100 or more fits.
_S189 = _np184.diag([0.04, 0.04])
_c189 = {"fee": 1.0, "fx": 0.0}
_pl189 = _mv.plan([900.0, 100.0], [100.0, 50.0], [_c189, _c189], _np184.array([0.5, 0.5]), _S189,
                  monthly=100.0, months=6, max_cost=0.01)
_ords189 = [o for m in _pl189 for o in m["orders"]]
_tes189 = [_pl189[0]["te_before"]] + [m["te"] for m in _pl189]
_ok189 = (not _pl189[0]["orders"] and len(_ords189) >= 2 and all(o["i"] == 1 for o in _ords189)
          and all(abs(o["value"] - o["n"] * 50.0) < 1e-9 and o["cost"] / o["value"] <= 0.01 + 1e-12 for o in _ords189)
          and all(m["left"] >= -1e-9 for m in _pl189) and all(b <= a + 1e-12 for a, b in zip(_tes189, _tes189[1:]))
          and abs(sum(o["value"] + o["cost"] for o in _ords189) + _pl189[-1]["left"] - 600.0) < 1e-6)
print(f"189 the deposit plan         {ok(_ok189)}  €900/€100 toward 50/50 at €100 a month: month 1 waits, then {len(_ords189)} orders, all of "
      f"the underweight one, whole shares, none over 1%; the gap {_tes189[0] * 100:.1f}% → {_tes189[-1] * 100:.1f}%; every euro accounted for")

# 190 the constellation: holdings that move together sit together. Two
#     groups of three, 0.9 within and 0.1 across, are three times further
#     apart across than within in full; in three dimensions still more than
#     2.5 times. Most stars inside the unit sphere, none far beyond it.
_C190 = _np184.full((6, 6), 0.1)
_C190[:3, :3] = 0.9
_C190[3:, 3:] = 0.9
_np184.fill_diagonal(_C190, 1.0)
_X190, _sh190 = _mv.embed3(_C190)
_d190 = lambda a, b: float(_np184.linalg.norm(_X190[a] - _X190[b]))
_in190 = max(_d190(0, 1), _d190(0, 2), _d190(3, 4), _d190(4, 5))
_out190 = min(_d190(a, b) for a in range(3) for b in range(3, 6))
_rad190 = _np184.sqrt((_X190 ** 2).sum(axis=1))
# an uneven sky, four and two, still centred on the middle of the picture
_C190b = _np184.full((6, 6), 0.2)
_C190b[:4, :4] = 0.85
_C190b[4:, 4:] = 0.7
_C190b[0, 1] = _C190b[1, 0] = 0.95
_np184.fill_diagonal(_C190b, 1.0)
_Xb190, _ = _mv.embed3(_C190b)
_ctr190 = float(_np184.abs(_Xb190.mean(axis=0)).max())
_ok190 = (_in190 * 2.5 < _out190 and float(_rad190.max()) <= 1.35 + 1e-9 and 0 < float(_sh190.sum()) <= 1.0 + 1e-9
          and _X190.shape == (6, 3) and _ctr190 < 1e-9)
print(f"190 the constellation        {ok(_ok190)}  two groups of three: {_in190:.2f} apart within a group, {_out190:.2f} across; "
      f"none beyond {_rad190.max():.2f} of the sphere; an uneven four-and-two sky centres on the middle")

# 191 MVO is wired: the routes answer through the router, views are kept and
#     checked, the page and the home screen's stars are linked, the panel
#     stops when hidden and keeps still for reduced motion, the build moved.
from pf import api as _api191
_paths191 = {r.path for r in _api191.router.routes}
_mj191 = open(os.path.join(HERE, "backend", "static", "mvo.js"), encoding="utf-8").read()
_sj191 = open(os.path.join(HERE, "backend", "static", "stars.js"), encoding="utf-8").read()
_ix191 = open(os.path.join(HERE, "backend", "static", "index.html"), encoding="utf-8").read()
_tj191 = open(os.path.join(HERE, "backend", "static", "term.js"), encoding="utf-8").read()
_ap191 = open(os.path.join(HERE, "backend", "portfolio_app.py"), encoding="utf-8").read()
_tmp191 = os.path.join(_tf181.gettempdir(), "pf_mvo28check.db")
for _x in (_tmp191, _tmp191 + "-wal", _tmp191 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_orig191 = store.DB_PATH
store.DB_PATH = _tmp191
_vw191 = _bad191 = None
try:
    store.init_db()
    _mv.set_view("msft", 12.0, "mid", "cloud")
    _mv.set_view("MSFT", 14.0, "high")
    _vw191 = _mv.views()
    _bad191 = 0
    for _args in (("NVDA", 12.0, "sure"), ("NVDA", 400.0, "low"), ("", 5.0, "low")):
        try:
            _mv.set_view(*_args)
        except ValueError:
            _bad191 += 1
    _mv.drop_view("MSFT")
    _left191 = _mv.views()
finally:
    store.DB_PATH = _orig191
    for _x in (_tmp191, _tmp191 + "-wal", _tmp191 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
_ok191 = ({"/api/pf/mvo", "/api/pf/mvo/view", "/api/pf/mvo/view/{t}", "/api/pf/stars"} <= _paths191
          and _vw191 == [{"t": "MSFT", "ret": 14.0, "conf": "high", "note": "", "created": _vw191[0]["created"]}]
          and _bad191 == 3 and _left191 == []
          and '"MVO": "mvo.html"' in _ap191 and 'code: "MVO",  title: "Markowitz & forecast"' in _tj191 and '"MARKOWITZ", "FRONTIER"' in _tj191
          and "/api/pf/mvo?" in _mj191 and "/api/pf/mvo/view" in _mj191
          and "Stars.mount(" in _ix191 and "Stars.tick(" in _ix191 and '<script src="/stars.js">' in _ix191
          and "/api/pf/stars" in _sj191 and "prefers-reduced-motion" in _sj191 and "IntersectionObserver" in _sj191
          and "document.hidden" in _sj191 and _BUILD >= 28)
print(f"191 Markowitz is wired       {ok(_ok191)}  four routes; a view kept once per holding and replaced, three bad ones refused, "
      f"removed; MVO and MARKOWITZ open the page; the home screen mounts the stars and feeds them live ticks; they stop when hidden")

# ---------------------------------------------------------------------------
# EUR, the euro (build 29): changes, the rate-gap fit, what a move does to the
# book's dollars, hedging, positioning, alerts and the brief, the options range
# ---------------------------------------------------------------------------
from pf import euro as _eu
import math as _m192
from datetime import date as _d192, timedelta as _td192

# 192 changes over a period read the last value on or before each end: a
#     week across a weekend, a year to date from the last day of last year.
_s192 = {"2025-12-31": 1.00, "2026-01-02": 1.02, "2026-10-02": 1.10, "2026-10-05": 1.11, "2026-10-09": 1.12, "2026-10-12": 1.21}
_ok192 = (abs(_eu.change(_s192, 7) - (1.21 / 1.11 - 1) * 100) < 1e-9
          and abs(_eu.ytd(_s192) - 21.0) < 1e-9
          and _eu.on_or_before(_s192, "2026-10-11") == ("2026-10-09", 1.12)
          and _eu.on_or_before(_s192, "2025-01-01") is None
          and _eu.change(_s192, 400) is None)
print(f"192 the euro's changes       {ok(_ok192)}  a week to Monday 12/10 reads Monday 05/10 (+{_eu.change(_s192, 7):.2f}%); the year to date "
      f"from 31/12: +{_eu.ytd(_s192):.1f}%; nothing before the first day")

# 193 the rate-gap fit: EUR/USD built as exactly 1.30 − 0.10 × the gap gives
#     back that line, an R² of one and a fair value equal to the rate; one
#     cent off the line today reads as that far above it.
_days193, _us193, _de193, _fx193 = [], {}, {}, {}
_t193 = _d192(2023, 1, 6)
for _i in range(160):
    _d = (_t193 + _td192(days=7 * _i)).isoformat()
    _g = 1.0 + 1.5 * _m192.sin(_i / 9.0)
    _us193[_d], _de193[_d], _fx193[_d] = 2.0 + _g, 2.0, 1.30 - 0.10 * _g
_last193 = max(_fx193)
_f193 = _eu.gap_fit(_fx193, _us193, _de193)
_fx193b = dict(_fx193)
_fx193b[_last193] += 0.01
_f193b = _eu.gap_fit(_fx193b, _us193, _de193)
_ok193 = (_f193 and abs(_f193["b"] + 0.10) < 1e-4 and abs(_f193["a"] - 1.30) < 1e-4 and _f193["r2"] == 1.0
          and abs(_f193["fair"] - _fx193[_last193]) < 1e-4 and abs(_f193["gap_pct"]) < 0.05 and _f193["n"] == 156
          and _f193b["gap_pct"] > 0.5 and len(_f193["pts"]) == 156)
print(f"193 the rate-gap fit         {ok(_ok193)}  a pair built as 1.30 − 0.10 × gap: slope {_f193['b']:+.4f}, R² {_f193['r2']:.2f}, "
      f"fair value = the rate; a cent above the line today reads {_f193b['gap_pct']:+.1f}%")

# 194 what a move does to the dollars: €10,000 in dollars is worth €909 less
#     when EUR/USD rises 10% and €1,111 more when it falls 10%.
_sv194 = {r["move"]: r["eur"] for r in _eu.sensitivity(10000.0)}
_ok194 = abs(_sv194[10] + 909.09) < 0.01 and abs(_sv194[-10] - 1111.11) < 0.01 and abs(_sv194[1] + 99.01) < 0.01
print(f"194 a move against the dollars {ok(_ok194)}  €10,000 in dollars: EUR/USD +10% → €{_sv194[10]:,.2f}, −10% → +€{_sv194[-10]:,.2f}, +1% → €{_sv194[1]:,.2f}")

# 195 hedging: a share index rising 1% a month with the euro flat earns 1% a
#     month unhedged and 1% plus the rate gap hedged (2% euro, 4% dollar:
#     −2/12 points a month); a month in which the euro rises 10% turns the
#     unhedged month into 1.01/1.10 − 1, and leaves the hedged one alone.
_sp195, _fx195, _e3, _u3 = {}, {}, {}, {}
for _i in range(80):
    _y, _mm = 2000 + _i // 12, _i % 12 + 1
    _k = f"{_y}-{_mm:02d}"
    _sp195[_k + "-01"] = 100 * 1.01 ** _i
    _e3[_k], _u3[_k] = 2.0, 4.0
    _end = (_d192(_y + (_mm == 12), _mm % 12 + 1, 1) - _td192(days=1)).isoformat()
    _fx195[_end] = 1.0 if _i < 70 else 1.1
_h195 = _eu.hedge_history(_sp195, _fx195, _e3, _u3, since="2000-01")
_r195 = {r[0][:7]: r for r in _h195["series"]}
_y195 = {y["y"]: y for y in _h195["years"]}
_un69 = _r195["2005-11"][1] / _r195["2005-10"][1] - 1          # month 70, Nov 2005: the euro jumps
_hd69 = _r195["2005-11"][2] / _r195["2005-10"][2] - 1
# the series is rounded to four decimals, so a month read back from it is good to about 1e-4
_ok195 = (_h195["months"] == 79 and abs(_r195["2000-03"][1] / _r195["2000-02"][1] - 1.01) < 3e-4
          and abs(_r195["2000-03"][2] / _r195["2000-02"][2] - (1 + 0.01 - 2 / 1200)) < 3e-4
          and abs(_un69 - (1.01 / 1.10 - 1)) < 3e-4 and abs(_hd69 - (0.01 - 2 / 1200)) < 3e-4
          and abs(_h195["hedged"]["cagr"] - ((1.01 - 2 / 1200) ** 12 - 1) * 100) < 0.01
          and _h195["hedged"]["worst"] == 0.0 and _h195["unhedged"]["worst"] < -8)
print(f"195 hedging, month by month   {ok(_ok195)}  1% a month, euro flat: 1.0000% unhedged, {(0.01 - 2 / 1200) * 100:.4f}% hedged (the 2-point gap); "
      f"the month the euro rises 10%: {_un69 * 100:+.2f}% unhedged, {_hd69 * 100:+.2f}% hedged")

# 196 positioning: each group's net is longs less shorts, as a share of open
#     interest; weeks come back in date order whatever the feed's order.
_cot196 = _eu.parse_cot([
    {"report_date_as_yyyy_mm_dd": "2026-10-06T00:00:00.000", "open_interest_all": "800000", "asset_mgr_positions_long": "500000",
     "asset_mgr_positions_short": "300000", "lev_money_positions_long": "100000", "lev_money_positions_short": "140000",
     "dealer_positions_long_all": "40000", "dealer_positions_short_all": "200000"},
    {"report_date_as_yyyy_mm_dd": "2026-09-29T00:00:00.000", "open_interest_all": "0"},
    {"report_date_as_yyyy_mm_dd": "2026-09-22T00:00:00.000", "open_interest_all": "780000", "asset_mgr_positions_long": "400000",
     "asset_mgr_positions_short": "300000"}])
_ok196 = (len(_cot196) == 2 and _cot196[0]["d"] == "2026-09-22" and _cot196[1]["am"] == 200000 and _cot196[1]["am_pct"] == 25.0
          and _cot196[1]["lf"] == -40000 and _cot196[1]["lf_pct"] == -5.0 and _cot196[1]["dealer"] == -160000
          and _cot196[0]["lf"] == 0)
print(f"196 who is betting           {ok(_ok196)}  asset managers 500k long, 300k short of 800k open: +200k, +25%; leveraged funds −40k, −5%; "
      f"a week without open interest is dropped; oldest first")

# 197 alerts and the brief: a 1.2% day for EUR/USD on the dashboard is an EUR
#     alert carrying the book's currency effect; a 30bp month for the rate gap
#     is one too; a rule above 1.15 fires at 1.16 and not at 1.14; with
#     nothing cached the brief stays silent and the rule says so.
_tmp197 = os.path.join(_tf181.gettempdir(), "pf_eur29check.db")
for _x in (_tmp197, _tmp197 + "-wal", _tmp197 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_orig197 = store.DB_PATH
store.DB_PATH = _tmp197
_ok197 = False
try:
    store.init_db()
    _empty197 = (_eu.brief_line() is None and _eu.check_rule("eur_usd_above", 1.15)["level"] == "info")
    _dash197 = {"positions": [{"currency": "EUR", "fx": 1.0}, {"currency": "USD", "fx": 1.1132, "fx_prev": 1.1000}],
                "totals": {"day_fx_eur": -180.0}}
    _mv197 = _eu.live_move(_dash197)
    _al0 = _eu.alerts(_dash197)
    store.cache_put("eur:alert", {"fx": 1.16, "day": 0.4, "m1": -1.2, "gap": 1.69, "gap_m1_bp": -30, "usd_pct": 70.0,
                                  "fed_bp": 82, "ecb_bp": 69, "ecb_next": "2026-10-29"})
    _al1 = _eu.alerts({"positions": [], "totals": {}})
    _br197 = _eu.brief_line()
    _ok197 = (_empty197 and abs(_mv197["pct"] - 1.2) < 1e-9 and len(_al0) == 1 and _al0[0]["code"] == "EUR"
              and "+1.20%" in _al0[0]["text"] and "-180" in _al0[0]["detail"]
              and len(_al1) == 1 and "narrowed 30bp" in _al1[0]["text"]
              and _eu.check_rule("eur_usd_above", 1.15)["level"] == "warn" and _eu.check_rule("eur_usd_below", 1.15) is None
              and _eu.check_rule("eur_usd_above", 1.15, fx_live=1.14) is None
              and _br197.startswith("Euro: EUR/USD 1.1600") and "70% of your book in dollars" in _br197
              and "next ECB 29/10" in _br197 and _br197.endswith("[EUR](/m/EUR)"))
finally:
    store.DB_PATH = _orig197
    for _x in (_tmp197, _tmp197 + "-wal", _tmp197 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
print(f"197 euro alerts and the brief {ok(_ok197)}  EUR/USD +1.20% on the dashboard: an alert with the −€180 it cost; the gap −30bp in a month: "
      f"another; a rule above 1.15 fires at 1.16, not at today's 1.14; nothing cached, nothing said")

# 198 the currency table drops a currency the ECB stopped fixing (the lev,
#     after 31/12/2025); FXE's range is scaled to EUR/USD by today's ratio;
#     the fallback range from past swings is a lognormal around today's rate.
_ref198 = {"USD": {f"2026-10-{d:02d}": 1.10 + d / 1000 for d in range(1, 31)} | {f"2026-09-{d:02d}": 1.10 for d in range(1, 31)},
           "BGN": {f"2025-12-{d:02d}": 1.95583 for d in range(1, 32)} | {f"2025-11-{d:02d}": 1.95583 for d in range(1, 31)}}
_cr198 = _eu.part_crosses(_ref198)
_ch198 = _eu.from_chain({"spot": 100.0, "expiries": [{"exp": "2026-11-20", "days": 38, "atm": 0.06, "q": [90, 95, 100, 105, 110]},
                                                    {"exp": "2026-10-16", "days": 3, "atm": 0.09, "q": [99, 99.5, 100, 100.5, 101]}],
                         "cm": {"30": 0.061}, "skew30": {"put": 0.065, "call": 0.058, "gap": 0.7}}, 1.12)
_fx198 = {(_d192(2026, 6, 1) + _td192(days=i)).isoformat(): 1.10 * (1.003 if i % 2 else 0.997) for i in range(100)}
_rr198 = _eu.realised_range(_fx198, 1.10)
_q198 = _rr198["rows"][0]["q"]
_ok198 = ([r["c"] for r in _cr198["rows"]] == ["USD"] and _cr198["gone"][0]["c"] == "BGN" and _cr198["gone"][0]["joined"] == "2026-01-01"
          and _ch198["factor"] == 0.0112 and _ch198["rows"][0]["q"] == [1.008, 1.064, 1.12, 1.176, 1.232] and len(_ch198["rows"]) == 1
          and _ch198["iv"]["30"] == 6.1 and _rr198["source"] == "realised"
          and abs(_m192.log(_q198[3] / _q198[2]) - _m192.log(_q198[2] / _q198[1])) < 1e-3 and _q198[2] < 1.10)
print(f"198 the table and the range   {ok(_ok198)}  the lev, last fixed 31/12/2025, leaves the table; FXE at $100 with EUR/USD 1.12 scales "
      f"by 0.0112; a three-day expiry is skipped; past swings give a range even in log terms around today's rate")

# 199 EUR is wired: the routes, the module and its words, the rules and the
#     alerts on the home screen, the brief's line, the warm-up at start.
from pf import api as _api199, service as _sv199, morning as _mo199
_paths199 = {r.path for r in _api199.router.routes}
_ej199 = open(os.path.join(HERE, "backend", "static", "euro.js"), encoding="utf-8").read()
_tj199 = open(os.path.join(HERE, "backend", "static", "term.js"), encoding="utf-8").read()
_ap199 = open(os.path.join(HERE, "backend", "portfolio_app.py"), encoding="utf-8").read()
_ix199 = open(os.path.join(HERE, "backend", "static", "index.html"), encoding="utf-8").read()
_ok199 = ({"/api/pf/euro", "/api/pf/euro/opt"} <= _paths199 and {"eur_usd_above", "eur_usd_below"} <= set(_sv199.RULE_KINDS)
          and "euro.alerts(d)" in _insp154.getsource(_sv199.alerts) and "euro.check_rule" in _insp154.getsource(_sv199.rule_alerts)
          and 'b["eur"]' in _insp154.getsource(_mo199.build) and 'b.get("eur")' in _insp154.getsource(_mo199.summary)
          and '"EUR": "euro.html"' in _ap199 and "euro.ensure()" in _ap199
          and 'code: "EUR",  title: "The euro"' in _tj199 and '"EURO", "EURUSD"' in _tj199
          and "/api/pf/euro" in _ej199 and "/api/pf/euro/opt" in _ej199 and "eur_usd_above:" in _ix199 and _BUILD >= 29)
print(f"199 the euro is wired        {ok(_ok199)}  two routes; EUR, EURO and EURUSD open the page; its rules and alerts reach the home "
      f"screen; the brief carries its line; it warms itself at start")

# ---------------------------------------------------------------------------
# OPT's strategies (build 30): placing legs, the whole position's arithmetic,
# the odds and the volatility premium, the screener, Cboe's histories, the
# VIX against what came
# ---------------------------------------------------------------------------
from pf import options as _op, strategy as _st, strathist as _sh
import math as _m200, statistics as _stat200, time as _t200
from datetime import date as _d200, timedelta as _td200

# A chain priced by Black's formula at 30% volatility, 40 days out, strikes 60
# to 140: every leg's own price gives 30% back, so the answers are known.
_rate200 = _op._rate
_op._rate = lambda T: 0.04
try:
    _S200, _v200 = 100.0, 0.30
    _now200 = _t200.time()
    _exp200 = (_d200.today() + _td200(days=40)).isoformat()
    _T200 = (_op._expiry_ts(_exp200) - _now200) / (365 * 86400)
    _F200 = _S200 * _m200.exp(0.04 * _T200)
    _df200 = _m200.exp(-0.04 * _T200)
    _px200 = lambda K, call: _df200 * _op.black(_F200, K, _T200, _v200, call)
    _rows200 = lambda call: [[float(K), round(_px200(K, call) - 0.005, 4), round(_px200(K, call) + 0.005, 4), round(_px200(K, call), 4), 0, 10, 100]
                             for K in range(60, 145, 5)]
    _ch200 = {"sym": "TEST", "fetched": _now200, "spot": _S200, "currency": "USD", "div_yield": 0.0,
              "exps": [{"exp": _exp200, "calls": _rows200(True), "puts": _rows200(False)}]}
    _er200 = {"exp": _exp200, "t": _T200, "fwd": _F200, "atm": _v200, "days": _T200 * 365, "q": None, "cdf": None}
    _a200 = {"expiries": [_er200], "points": {}, "mode": "quotes", "session": _d200.today().isoformat()}
    _L = lambda typ, side, K=None: {"type": typ, "side": side, "n": 1, "exp": _exp200, **({"strike": float(K)} if K else {})}

    # 200 placing the legs: the expiry nearest a month (two weeks at least)
    #     and one 25+ days beyond it; a 5-delta call off the smile's grid has
    #     Black's delta of 0.05 exactly; a condor's strikes sit put wing <
    #     short put < forward < short call < call wing, all listed.
    _ne200, _fa200 = _st.pick_expiries([{"exp": "a", "days": 5}, {"exp": "b", "days": 20}, {"exp": "c", "days": 40}, {"exp": "d", "days": 70}])
    _k5 = _st.target_strike({"d": 0.05}, "C", _er200, _a200, _S200)
    _kp = _st.target_strike({"d": 0.30}, "P", _er200, _a200, _S200)
    _ic200 = _st.default_legs("iron_condor", _ch200, _a200)
    _ks200 = [l["strike"] for l in _ic200]
    _ok200 = (_ne200["exp"] == "c" and _fa200["exp"] == "d"
              and abs(_op.call_delta(_m200.log(_k5 / _F200), _v200, _T200) - 0.05) < 1e-6 and _kp < _F200
              and abs(_op.call_delta(_m200.log(_kp / _F200), _v200, _T200) - 0.70) < 1e-6
              and _ks200[0] < _ks200[1] < _F200 < _ks200[2] < _ks200[3] and len(set(_ks200)) == 4
              and [l["side"] for l in _ic200] == [1, -1, -1, 1]
              and _st.target_strike({"atm": True}, "C", _er200, _a200, _S200) == _F200
              and abs(_st.target_strike({"pct": 0.05}, "C", _er200, _a200, _S200) - 105.0) < 1e-9
              and _st.snap([[90.0, 0, 0, 0], [95.0, 1.0, 1.1, 1.0]], 91.0)[0] == 95.0)
    print(f"200 placing the legs         {ok(_ok200)}  a month out: the 40-day expiry, then the 70-day; a 5-delta call at {_k5:.2f}; the condor at "
          f"{'/'.join(f'{k:g}' for k in _ks200)}; an unpriced strike is passed over")

    # 201 a bull call spread 100/110 costs the two prices' difference, makes
    #     ten dollars a share less that and loses at most that, breaks even at
    #     100 plus it; a covered call needs a hundred shares and cannot gain
    #     past its strike; a straddle has two break-evens and no ceiling.
    _bc = _st.evaluate(_ch200, _a200, [_L("C", 1, 100), _L("C", -1, 110)])
    _db = (_px200(100, True) - _px200(110, True)) * 100
    _cc = _st.evaluate(_ch200, _a200, [_L("S", 1), _L("C", -1, 110)])
    _sd201 = _st.evaluate(_ch200, _a200, [_L("C", 1, 100), _L("P", 1, 100)])
    _sdb = (_px200(100, True) + _px200(100, False))
    _g201 = (_op.greeks(_S200, 100, _T200, _v200, 0.04, 0.0, True)["delta"] - _op.greeks(_S200, 110, _T200, _v200, 0.04, 0.0, True)["delta"]) * 100
    _ok201 = (abs(_bc["cost"] - _db) < 0.02 and abs(_bc["max_gain"] - (1000 - _db)) < 0.05 and abs(_bc["max_loss"] + _db) < 0.05
              and len(_bc["breakevens"]) == 1 and abs(_bc["breakevens"][0] - (100 + _db / 100)) < 0.02
              and not _bc["unlimited_gain"] and not _bc["unlimited_loss"] and abs(_bc["capital"] - _db) < 0.02
              and abs(_bc["greeks"]["delta"] - _g201) < 0.02
              and _cc["shares_needed"] == 100 and not _cc["unlimited_gain"] and abs(_cc["max_gain"] - (11000 - _cc["cost"])) < 0.05
              and abs(_cc["cost"] - (100 * _S200 - 100 * _px200(110, True))) < 0.02
              and abs(_cc["capital"] - _cc["cost"]) < 0.01
              and _sd201["unlimited_gain"] and len(_sd201["breakevens"]) == 2
              and abs(_sd201["breakevens"][0] - (100 - _sdb)) < 0.05 and abs(_sd201["breakevens"][1] - (100 + _sdb)) < 0.05)
    print(f"201 a position's payoff      {ok(_ok201)}  bull call 100/110: costs ${_bc['cost']:.2f}, makes at most ${_bc['max_gain']:.2f}, loses ${-_bc['max_loss']:.2f}, "
          f"breaks even at {_bc['breakevens'][0]:.2f}; covered call capped at its strike; straddle open above, two break-evens")

    # 202 the odds: the chance of a profit is the lognormal's beyond the
    #     break-even; at the market's own odds a position priced at the same
    #     volatility expects nothing once interest is counted; selling a
    #     straddle expects a gain if the share swings 20% and a loss at 40%.
    _be = _bc["breakevens"][0]
    _s202 = _v200 * _m200.sqrt(_T200)
    _pexp = _op.ncdf((_m200.log(_F200 / _be) - 0.5 * _s202 * _s202) / _s202)
    _ss20 = _st.evaluate(_ch200, _a200, [_L("C", -1, 100), _L("P", -1, 100)], rv=0.20)
    _ss40 = _st.evaluate(_ch200, _a200, [_L("C", -1, 100), _L("P", -1, 100)], rv=0.40)
    _ok202 = (abs(_bc["p_profit"] - _pexp) < 0.015 and abs(_bc["ev_market"]) < 1.5 and abs(_ss20["ev_market"]) < 3.0
              and _ss20["ev_rv"] > 100 and _ss40["ev_rv"] < -100 and _bc["odds_from"] == "model")
    print(f"202 the odds and the premium {ok(_ok202)}  bull call: {_bc['p_profit'] * 100:.1f}% a profit (lognormal {_pexp * 100:.1f}%), expected ${_bc['ev_market']:+.2f}; "
          f"a sold straddle expects ${_ss20['ev_rv']:+,.0f} if the share swings 20%, ${_ss40['ev_rv']:+,.0f} at 40%")

    # 203 the screener's legs: the 30-delta call above the price and the
    #     30-delta put below it, each paying its price over the share (call)
    #     or the strike (put), scaled to a year by the days left.
    _c203 = _st._row_for(_a200, _ch200, _er200, "C", 0.30)
    _p203 = _st._row_for(_a200, _ch200, _er200, "P", 0.30)
    _pc = next(r for r in _ch200["exps"][0]["calls"] if r[0] == _c203["strike"])
    _pp = next(r for r in _ch200["exps"][0]["puts"] if r[0] == _p203["strike"])
    _ok203 = (_c203["strike"] > _S200 and _p203["strike"] < _S200 and _c203["otm"] > 0 and _p203["otm"] < 0
              and abs(_c203["yield"] - round(0.5 * (_pc[1] + _pc[2]) / _S200 * 100, 2)) < 0.011
              and abs(_p203["yield_y"] - round(0.5 * (_pp[1] + _pp[2]) / _p203["strike"] * 365 / _er200["days"] * 100, 1)) < 0.11
              and _c203["p"] is None)
    print(f"203 the premium screener     {ok(_ok203)}  30-delta call at {_c203['strike']:g} pays {_c203['yield']:.2f}% ({_c203['yield_y']:.0f}% a year); "
          f"30-delta put at {_p203['strike']:g} pays {_p203['yield']:.2f}% of its strike")
finally:
    _op._rate = _rate200

# 204 Cboe's histories: a scattered old value does not join the record; a
#     strategy alternating +2% and −1% a month against an index alternating
#     +1% and 0% compounds exactly, falls 1% at worst, catches 200% of the up
#     months, and in a calm VIX every month counts as calm.
_ms204 = [f"{2019 + i // 12}-{i % 12 + 1:02d}" for i in range(37)]
_lv, _bv, _a, _b = {"2010-05-28": 50.0}, {}, 100.0, 100.0
for _i, _m in enumerate(_ms204):
    if _i:
        _a *= 1.02 if _i % 2 else 0.99
        _b *= 1.01 if _i % 2 else 1.00
    _lv[_m + "-28"], _bv[_m + "-28"] = _a, _b
_data204 = {"BXM": _lv, "SPXTR": _bv, "VIX": {_m + "-28": 12.0 for _m in _ms204}}
from pf import bonds as _bo204
_us204 = _bo204.us_series
_bo204.us_series = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
try:
    _h204 = _sh.history("2010-01", "USD", _data204)
finally:
    _bo204.us_series = _us204
_r204 = _h204["rows"][0]
_cagr204 = ((1.02 * 0.99) ** 18) ** (12 / 36) - 1
_ok204 = (_sh.unbroken(["2010-05", "2019-01", "2019-02", "2019-03"]) == ["2019-01", "2019-02", "2019-03"]
          and len(_h204["rows"]) == 1 and _r204["from"] == "2019-02" and _r204["months"] == 36
          and abs(_r204["cagr"] - round(_cagr204 * 100, 2)) < 1e-9 and _r204["worst"] == -1.0 and _r204["up"] == 200
          and _r204["down"] is None and _r204["pos_months"] == 50
          and list(_r204["regimes"]) == ["calm"] and _r204["regimes"]["calm"]["n"] == 36
          and _sh.regime_of(15.0) == "normal" and _sh.regime_of(25.0) == "stressed" and _sh.regime_of(14.99) == "calm")
print(f"204 Cboe's histories         {ok(_ok204)}  a lone value from 2010 is left out; +2%/−1% a month compounds to {_r204['cagr']:.2f}% a year, "
      f"worst −1%, 200% of the up months, every month calm at a VIX of 12")

# 205 why sellers earn: the next 21 sessions' realised volatility of a
#     market alternating ±1% a day, against a VIX of 20 every month; the VIX
#     above its three-month sibling on a known share of days.
_days205, _px205, _x = [], {}, 100.0
_d = _d200(2000, 1, 3)
while len(_days205) < 252 * 6:
    if _d.weekday() < 5:
        _days205.append(_d.isoformat())
        _px205[_d.isoformat()] = _x
        _x *= 1.01 if len(_days205) % 2 else 0.99
    _d += _td200(days=1)
_v3 = {d: (18.0 if i % 4 == 0 else 22.0) for i, d in enumerate(_days205)}
_sel205 = _sh.sellers({"SPX": _px205, "VIX": {d: 20.0 for d in _days205}, "VIX3M": _v3, "VIX9D": {}, "SKEW": {d: 130.0 for d in _days205}})
_rs205 = [_m200.log(_px205[_days205[j + 1]] / _px205[_days205[j]]) for j in range(0, 21)]
_rv205 = _stat200.pstdev(_rs205) * _m200.sqrt(252) * 100
_ok205 = (abs(_sh.realised_forward(_px205, _days205, 0) - _rv205) < 1e-9 and _sel205["share_over"] == 100
          and abs(_sel205["gap_avg"] - round(20 - _sel205["rv_avg"], 1)) <= 0.11 and abs(_sel205["inverted_share"] - 25.0) < 0.2
          and _sel205["n"] >= 65 and _sel205["now"]["vix"] == 20.0 and _sel205["skew_pct"] == 100)
print(f"205 why sellers earn         {ok(_ok205)}  ±1% a day is {_rv205:.1f}% a year realised against a VIX of 20: priced over delivered in every month; "
      f"the VIX above its three-month version on {_sel205['inverted_share']:.0f}% of days")

# 206 reading Cboe's files: one value column or OPEN/HIGH/LOW/CLOSE (the
#     close is kept); a bad date or a blank value is skipped.
_p206a = _sh.parse_cboe("DATE,BXM\n03/22/2002,584.30\nbad,1\n03/25/2002,\n03/26/2002,580.1\n")
_p206b = _sh.parse_cboe("DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,17.24,17.30,17.10,17.29\n")
_ok206 = _p206a == {"2002-03-22": 584.3, "2002-03-26": 580.1} and _p206b == {"1990-01-02": 17.29}
print(f"206 reading Cboe's files     {ok(_ok206)}  a one-value file and an OHLC file both read, the close kept; a bad row and a blank value skipped")

# 207 OPT's strategies are wired: four routes; fifteen templates, each with
#     what it is, when it works and what can go wrong; the page loads the
#     builder on a share and the three panels on the book; the build moved.
from pf import api as _api207
_paths207 = {r.path for r in _api207.router.routes}
_oh207 = open(os.path.join(HERE, "backend", "static", "options.html"), encoding="utf-8").read()
_oj207 = open(os.path.join(HERE, "backend", "static", "options.js"), encoding="utf-8").read()
_os207 = open(os.path.join(HERE, "backend", "static", "optstrat.js"), encoding="utf-8").read()
_ok207 = ({"/api/pf/opt/strategies", "/api/pf/opt/strategy/{sym}", "/api/pf/opt/screen", "/api/pf/opt/history"} <= _paths207
          and len(_st.TEMPLATES) == 15 and all(t.get("what") and t.get("when") and t.get("risk") and t["view"] in _st.VIEWS for t in _st.TEMPLATES.values())
          and _st.FEE_USD == 0.75 and len(_sh.STRATS) == 11
          and _oh207.index('src="/optstrat.js"') < _oh207.index('src="/options.js"')
          and 'OptStrat.builder($("#s-strat"), SYM)' in _oj207 and "OptStrat.overview(STRAT_OV)" in _oj207
          and "/api/pf/opt/screen" in _os207 and "/api/pf/opt/history" in _os207 and _BUILD >= 30)
print(f"207 strategies are wired     {ok(_ok207)}  four routes; fifteen templates, each saying what, when and the risk; eleven Cboe indexes; "
      f"the builder on a share's page, the screener and the histories on the book's")

# ---------------------------------------------------------------------------
# MAC, the macro calendar (build 31): numbers as the feed writes them, the
# period a release reports, the actual from its series, the reaction from
# five-minute bars, the record, the wiring
# ---------------------------------------------------------------------------
from pf import macro as _mc
from datetime import date as _d208, datetime as _dt208, timedelta as _td208
import time as _t208

# 208 the feed's numbers: a percent, thousands, millions, a sign, a "<",
#     a plain index; nothing from an empty cell or a word.
_ok208 = (_mc.parse_num("0.3%") == (0.3, "%") and _mc.parse_num("195K") == (195.0, "K") and _mc.parse_num("-0.1%") == (-0.1, "%")
          and _mc.parse_num("<0.1%") == (0.1, "%") and _mc.parse_num("7.67M") == (7.67, "M") and _mc.parse_num("47.5") == (47.5, "")
          and _mc.parse_num("") is None and _mc.parse_num(None) is None and _mc.parse_num("tentative") is None
          and _mc.fmt(0.25, "%", 1) == "0.2%" and _mc.fmt(29.0, "K", 0) == "29K")
print(f"208 the feed's numbers       {ok(_ok208)}  0.3% · 195K · −0.1% · <0.1% · 7.67M · 47.5 read; an empty cell and a word are not numbers")

# 209 the period a release reports: CPI on 14 October is September's,
#     JOLTS August's; claims on Thursday the week to Saturday; GDP in late
#     October the third quarter, in late January the fourth; a flash at the
#     month's end is that month's, at the start of the next still last
#     month's; the Fed's target the next day, the ECB's a week on.
_ok209 = (_mc.reference("m", _d208(2026, 10, 14)) == "2026-09-01" and _mc.reference("m2", _d208(2026, 10, 6)) == "2026-08-01"
          # JOLTS at the end of a month (build 33): 29 September is August's, 4 September July's
          and _mc.reference("m2", _d208(2026, 9, 29)) == "2026-08-01" and _mc.reference("m2", _d208(2026, 9, 4)) == "2026-07-01"
          and _mc.reference("m", _d208(2026, 1, 13)) == "2025-12-01"
          and _mc.reference("w", _d208(2026, 10, 15)) == "2026-10-10" and _mc.reference("w", _d208(2026, 10, 17)) == "2026-10-10"
          and _mc.reference("q", _d208(2026, 10, 29)) == "2026-07-01" and _mc.reference("q", _d208(2027, 1, 28)) == "2026-10-01"
          and _mc.reference("q", _d208(2026, 12, 22)) == "2026-07-01"
          and _mc.reference("mf", _d208(2026, 10, 30)) == "2026-10-01" and _mc.reference("mf", _d208(2026, 11, 3)) == "2026-10-01"
          and _mc.reference("d", _d208(2026, 10, 28)) == "2026-10-29" and _mc.reference("d7", _d208(2026, 10, 29)) == "2026-11-05")
print(f"209 the period it reports    {ok(_ok209)}  CPI on 14/10 is September's, JOLTS on 06/10 and on 29/09 August's, claims on Thursday the week to Saturday 10/10, "
      f"GDP on 29/10 the third quarter; a flash on 30/10 or 03/11 is October's")

# 210 the actual from its series: an index up from 300.0 to 300.9 is +0.3%
#     on the month; a year on 3.0% up; payrolls the change in thousands;
#     claims in thousands from persons; the Fed's target on the first day at
#     or after; a period not published yet is no actual at all.
_ix210 = {f"2025-{m:02d}-01": 291.0 for m in range(1, 13)} | {"2026-08-01": 300.0, "2026-09-01": 300.9}
_ix210["2025-09-01"] = 292.1359             # a year before September 2026: +3.0%; the months between are missing on purpose
_cpi = _mc.SOURCES[("USD", "CPI m/m")]
_yoy = {**_mc.SOURCES[("USD", "CPI y/y")]}
_pay = {"2026-07-01": 159000.0, "2026-08-01": 159162.0, "2026-09-01": 159191.0}
_icsa = {"2026-10-03": 197000.0, "2026-10-10": 199500.0}
_dff = {"2026-10-28": 4.0, "2026-10-29": 4.25, "2026-10-30": 4.25}
_v210 = _mc.value_at(_ix210, _cpi, "2026-09-01")
_y210 = _mc.value_at(_ix210, _yoy, "2026-09-01")
_p210 = _mc.value_at(_pay, _mc.SOURCES[("USD", "Non-Farm Employment Change")], "2026-09-01")
_c210 = _mc.value_at(_icsa, _mc.SOURCES[("USD", "Unemployment Claims")], "2026-10-10")
_f210 = _mc.value_at(_dff, _mc.SOURCES[("USD", "Federal Funds Rate")], "2026-10-29")
_ok210 = (_v210 == (0.3, 3.1) and _y210 and _y210[0] == 3.0 and _p210 == (29.0, 162.0) and _c210 == (200.0, 197.0)
          and _f210 and _f210[0] == 4.25 and _mc.value_at(_ix210, _cpi, "2026-10-01") is None
          and _mc.value_at(_dff, _mc.SOURCES[("USD", "Federal Funds Rate")], "2026-11-01") is None)
print(f"210 the actual from a series {ok(_ok210)}  300.0 → 300.9 reads 0.3%; a year on, 3.0%; payrolls +29K after +162K; claims 199,500 → 200K; "
      f"the target the day after; an unpublished month is nothing")

# 211 the reaction: from the close of the bar finished by the release to
#     the close half an hour later; no bar within a quarter of an hour of
#     either end, no reaction.
_t0 = 1_800_000_000
_bars211 = [(_t0 - 3600 + 300 * i, 100.0 if _t0 - 3600 + 300 * i < _t0 else 101.0) for i in range(30)]
_gap211 = [b for b in _bars211 if not (_t0 - 1500 <= b[0] <= _t0 + 300)]
_ok211 = (_mc.move(_bars211, _t0) == 1.0 and _mc.move(_bars211, _t0 - 1800) == 0.0 and _mc.move(_gap211, _t0) is None
          and _mc.move([], _t0) is None)
print(f"211 the half hour after      {ok(_ok211)}  a step from 100 to 101 at the release reads +1.00%; flat before it 0%; a hole in the bars, or none, reads nothing")

# 212 the record: a release is stored once and updated without losing its
#     actual; the actual is filled only half an hour after, from the series,
#     with its surprise and the check against "previous"; the reaction from
#     the bars; a slope needs four surprises.
_tmp212 = os.path.join(_tf181.gettempdir(), "pf_mac31check.db")
for _x in (_tmp212, _tmp212 + "-wal", _tmp212 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_orig212 = store.DB_PATH
store.DB_PATH = _tmp212
_ok212 = False
try:
    store.init_db()
    _today = _d208.today()
    _rel = _dt208(_today.year, _today.month, _today.day, 14, 30).timestamp()
    _ev = {"date": _today.isoformat(), "time": "14:30", "ts": _rel, "currency": "USD", "title": "CPI m/m", "impact": "high", "rank": 3,
           "forecast": "0.2%", "previous": "0.4%"}
    _ref = _mc.reference("m", _today)
    _pm = (_d208.fromisoformat(_ref) - _td208(days=1)).replace(day=1).isoformat()
    _ppm = (_d208.fromisoformat(_pm) - _td208(days=1)).replace(day=1).isoformat()
    _n1 = _mc.upsert([_ev])
    _ix212 = {_ppm: 298.8, _pm: 300.0, _ref: 300.9}
    _early = _mc.fill_actuals(now=_rel + 600, series={"CPIAUCSL": _ix212})
    _got = _mc.fill_actuals(now=_rel + 3600, series={"CPIAUCSL": _ix212})
    _n2 = _mc.upsert([{**_ev, "forecast": "0.3%"}])
    _rx = _mc.fill_reactions(now=_rel + 3600, bar_sets={_mc.FX_SYM: [(int(_rel) - 3600 + 300 * i, 1.10 if i < 12 else 1.0989) for i in range(30)],
                                                       _mc.SPX_SYM: []})
    _row = _mc.rows(_today.isoformat(), _today.isoformat())[0]
    _sens = _mc.sensitivity([{"surprise": s, "rx_fx": -0.3 * s} for s in (0.1, -0.1, 0.2, 0.1)], "rx_fx")
    _ok212 = (_n1 == 1 and _n2 == 0 and _early == 0 and _got == 1 and _row["actual"] == "0.3%" and _row["surprise"] == 0.1
              and "(ok)" in (_row["actual_src"] or "") and _row["forecast"] == "0.3%"
              and _row["rx_fx"] == -0.1 and _row["rx_spx"] is None and _row["rx_at"]
              and _sens == {"per_unit": -0.3, "n": 4} and _mc.sensitivity([{"surprise": 0.1, "rx_fx": 0.1}] * 3, "rx_fx") is None)
finally:
    store.DB_PATH = _orig212
    for _x in (_tmp212, _tmp212 + "-wal", _tmp212 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
print(f"212 the record               {ok(_ok212)}  CPI stored once, its forecast updated; nothing ten minutes in, 0.3% an hour later (+0.1 against 0.2%, "
      f"previous 0.4% checks out); EUR/USD −0.10% after; a slope from four surprises, not three")

# 213 MAC is wired: two routes, the module live with ECO and MACRO, the page
#     and its loop at start, the build moved.
from pf import api as _api213
_paths213 = {r.path for r in _api213.router.routes}
_tj213 = open(os.path.join(HERE, "backend", "static", "term.js"), encoding="utf-8").read()
_ap213 = open(os.path.join(HERE, "backend", "portfolio_app.py"), encoding="utf-8").read()
_mj213 = open(os.path.join(HERE, "backend", "static", "mac.js"), encoding="utf-8").read()
_ok213 = ({"/api/pf/mac", "/api/pf/mac/refresh"} <= _paths213
          and 'code: "MAC",  title: "Macro calendar",   href: "/m/MAC",       state: "live"' in _tj213 and '"ECO", "ECOW", "MACRO"' in _tj213
          and '"MAC": "mac.html"' in _ap213 and "macro.ensure()" in _ap213 and "/api/pf/mac" in _mj213
          and all(s["src"] in ("fred", "ecb") and s["freq"] in ("m", "m2", "mf", "w", "q", "d", "d7") for s in _mc.SOURCES.values())
          and _BUILD >= 31)
print(f"213 MAC is wired             {ok(_ok213)}  two routes; MAC, ECO and MACRO open it; it reads itself every quarter hour from start; "
      f"{len(_mc.SOURCES)} releases have a source for their actual")

# ---------------------------------------------------------------------------
# Build 32: the market-hours fix and the hours in the bar, blurring euros,
# the one fund, the constellation's world star and time-lapse, the rotation
# graph's motion and record, junk bonds, the forecast under MVO
# ---------------------------------------------------------------------------
import re as _re214
from datetime import datetime as _dt214
from zoneinfo import ZoneInfo as _zi214
from pf import stream as _st214, market as _mk214, mvo as _mv214, rotation as _ro214, bonds as _bo214

# 214 the market is open when a fresh tick says so: yfinance sends the
#     session as a number (1 = regular), and a symbol's last word ages out
#     after a quarter of an hour; no ticks at all is "don't know".
_now214 = 1_800_000_000.0
_q214 = lambda h, age: {"market_hours": h, "seen": _now214 - age}
_ok214 = (_st214.market_open({"MSFT": _q214(1, 10), "INGA.AS": _q214(1, 5000)}, now=_now214) is True
          and _st214.market_open({"MSFT": _q214(2, 10)}, now=_now214) is False
          and _st214.market_open({"MSFT": _q214(1, 5000)}, now=_now214) is False
          and _st214.market_open({"MSFT": {"price": 1.0}}, now=_now214) is None and _st214.market_open({}, now=_now214) is None
          and _st214.is_regular(1) and _st214.is_regular("REGULAR_MARKET") and _st214.is_regular("1")
          and not _st214.is_regular(0) and not _st214.is_regular(3) and not _st214.is_regular(True))
print(f"214 open is open             {ok(_ok214)}  a fresh 1 reads open; 2 (after hours) and a tick an hour and a half old read shut; "
      f"no session field reads don't-know; True is not the number 1")

# 215 the hours in the bar: New York open on a Wednesday afternoon and
#     closing at four; shut for Thanksgiving with Amsterdam open; the day
#     after closes at one and is after-hours at two; pre-market at eight on
#     a Monday; a Saturday waits for Monday; Amsterdam's Christmas Eve ends
#     at 14:05.
_NY, _AM = _zi214("America/New_York"), _zi214("Europe/Amsterdam")
def _h215(dt):
    return {x["k"]: x for x in _mk214.hours(dt)["exchanges"]}
_a215 = _h215(_dt214(2026, 11, 25, 15, 0, tzinfo=_NY))
_b215 = _h215(_dt214(2026, 11, 26, 10, 0, tzinfo=_NY))
_c215 = _h215(_dt214(2026, 11, 27, 12, 0, tzinfo=_NY))
_d215 = _h215(_dt214(2026, 11, 27, 14, 0, tzinfo=_NY))
_e215 = _h215(_dt214(2026, 11, 30, 8, 0, tzinfo=_NY))
_f215 = _h215(_dt214(2026, 11, 28, 12, 0, tzinfo=_NY))
_g215 = _h215(_dt214(2026, 12, 24, 13, 0, tzinfo=_AM))
_ts = lambda *a, tz=_NY: _dt214(*a, tzinfo=tz).timestamp()
_ok215 = (_a215["NYC"]["state"] == "open" and _a215["NYC"]["next"] == "closes" and _a215["NYC"]["at"] == _ts(2026, 11, 25, 16, 0)
          and _a215["AMS"]["state"] == "shut" and _a215["AMS"]["at"] == _ts(2026, 11, 26, 9, 0, tz=_AM)
          and _b215["NYC"]["state"] == "shut" and _b215["NYC"]["holiday"] == "Thanksgiving" and _b215["NYC"]["at"] == _ts(2026, 11, 27, 9, 30)
          and _b215["AMS"]["state"] == "open"
          and _c215["NYC"]["state"] == "open" and _c215["NYC"]["early"] == "13:00" and _c215["NYC"]["at"] == _ts(2026, 11, 27, 13, 0)
          and _d215["NYC"]["state"] == "post" and _d215["NYC"]["at"] == _ts(2026, 11, 30, 9, 30)
          and _e215["NYC"]["state"] == "pre" and _e215["NYC"]["at"] == _ts(2026, 11, 30, 9, 30)
          and _f215["NYC"]["state"] == "shut" and _f215["AMS"]["at"] == _ts(2026, 11, 30, 9, 0, tz=_AM) and "FRA" not in _f215
          and _g215["AMS"]["state"] == "open" and _g215["AMS"]["early"] == "14:05")
print(f"215 the hours in the bar     {ok(_ok215)}  NY open to 16:00 on 25/11, shut for Thanksgiving while Amsterdam trades, open to 13:00 on 27/11 "
      f"and after-hours at 14:00; pre-market at 08:00 on a Monday; Amsterdam's Christmas Eve ends 14:05")

# 216 blurring euros: the page's own pattern takes every way a euro amount
#     is written and leaves percentages, dollars and the exchange rate alone;
#     the switch is in every page's bar, remembered, and on Shift+B.
_tj216 = open(os.path.join(HERE, "backend", "static", "term.js"), encoding="utf-8").read()
_css216 = open(os.path.join(HERE, "backend", "static", "term.css"), encoding="utf-8").read()
# the portfolio curve writes its euros as "25.3k", with no euro sign: its labels carry the class themselves
_pf216 = open(os.path.join(HERE, "backend", "static", "portfolio.html"), encoding="utf-8").read()
_cv216 = _pf216[_pf216.index("function curveSVG"):_pf216.index("function curvePeriod")]
_k216 = [l for l in _cv216.splitlines() if "<text" in l and ("kfmt(t)" in l or "/1000).toFixed(1)}k" in l)]
_m216 = _re214.search(r"const RE = /(.+)/g;", _tj216)
_rx216 = _re214.compile(_m216.group(1)) if _m216 else None
_hit216 = {t: (_rx216.findall(t) if _rx216 else None) for t in
           ("€1.234,56", "+€12", "−€250", "€ 3.4k", "€1.2bn", "1.234 EUR", "EUR 500", "a gain of €3,210 (+4.1%)",
            "12.5%", "$412.30", "1.16 EUR/USD", "EUR/USD 1.16")}
_ok216 = (_rx216 is not None
          and all(_hit216[t] == [t] for t in ("€1.234,56", "+€12", "−€250", "€ 3.4k", "€1.2bn", "1.234 EUR", "EUR 500"))
          and _hit216["a gain of €3,210 (+4.1%)"] == ["€3,210"]
          and all(_hit216[t] == [] for t in ("12.5%", "$412.30", "1.16 EUR/USD", "EUR/USD 1.16"))
          and 'id="blurBtn"' in _tj216 and '"pf_blur"' in _tj216 and 'e.key === "B" && e.shiftKey' in _tj216
          and "[data-noblur]" in _tj216 and ".blur-eur .eur-amt" in _css216 and ".blur-eur input[data-eur]" in _css216
          and len(_k216) == 3 and all('class="eur-amt"' in l for l in _k216)
          # the cash balance per currency and DEGIRO's other credits carry no sign either
          and '<span class="sub eur-amt">${v.toFixed(2)}</span>' in _pf216
          and '<span class="sub eur-amt" style="white-space:nowrap">${r.eur.toFixed(2)}</span>' in _pf216)
print(f"216 blurring euros           {ok(_ok216)}  €1.234,56 · +€12 · −€250 · € 3.4k · €1.2bn · 1.234 EUR · EUR 500 blurred; "
      f"12.5%, $412.30 and EUR/USD 1.16 not; the portfolio curve's {len(_k216)} k-labels, cash by currency and other credits too; the € switch in the bar, kept, Shift+B")

# 217 the one fund: half the one fund's risk is half in it and half in
#     cash, earning cash plus half the premium; more risk than the fund
#     takes means borrowing; the plan buys only what is below its target,
#     never a target under half a percent, and keeps no cash it is not asked to.
_np214 = _mv214._np()
_t217 = {"vol": 0.131, "ret": 0.0825}
_h217 = _mv214.one_fund(_t217, 0.0229, 0.0655)
_l217 = _mv214.one_fund(_t217, 0.0229, 0.193)
_S217 = _np214.diag([0.04, 0.02])
_c217 = [{"fee": 1.0, "fx": 0.0}, {"fee": 1.0, "fx": 0.0}]
_o1 = _mv214.best_order([900.0, 100.0], [10.0, 10.0], _c217, [0.5, 0.5], _S217, 200.0, 0.05)
# a 0.4% target, a €1 share and no fee: one share would bring it nearer, and is still not bought
_o2 = _mv214.best_order([1000.0, 0.0], [10.0, 1.0], [{"fee": 1.0, "fx": 0.0}, {"fee": 0.0, "fx": 0.0}], [0.996, 0.004], _S217, 200.0, 0.05)
_S3, _v3, _p3, _c3, _ = _mv214.with_cash(_S217, [600.0, 400.0], [10.0, 10.0], _c217, None)
_o3 = _mv214.best_order(_v3, _p3, _c3, [0.5, 0.5, 0.0], _S3, 200.0, 0.05)
# the one holding below target costs more than the budget: buying an overweight
# low-risk one would dilute the big overweight, and must still not happen
_o4 = _mv214.best_order([500.0, 310.0, 190.0], [10.0, 10.0, 5000.0], [{"fee": 0.0, "fx": 0.0}] * 3, [0.3, 0.3, 0.4],
                        _np214.diag([0.09, 0.0001, 0.0001]), 200.0, 0.05)
_ok217 = (_h217["reach"] and _h217["share_at"] == 50.0 and _h217["cash_at"] == 50.0 and abs(_h217["ret_at"] - 5.27) < 0.01
          and not _l217["reach"] and _l217["share_at"] == 100.0 and _l217["cash_at"] == 0.0 and _l217["lever"] == 1.47
          and abs(_l217["ret_levered"] - 11.07) < 0.02 and [x["share"] for x in _h217["steps"]] == [25, 50, 75, 100]
          and _o1 is not None and _o1["i"] == 1 and _o2 is None
          and _S3.shape == (3, 3) and float(_S3[2].sum()) == 0.0 and _p3[2] == 1.0 and _c3[2] == {"fee": 0.0, "fx": 0.0}
          and (_o3 is None or _o3["i"] != 2) and _o4 is None)
print(f"217 the one fund             {ok(_ok217)}  at 6.55% risk: 50% fund, 50% cash, {_h217['ret_at']:.2f}% a year; at 19.3%: "
      f"{_l217['lever']:.2f}× the fund, borrowed; the plan buys the holding below target, not a 0.4% target, not cash at 0%")

# 218 the constellation: SMACOF lays out a square's distances exactly; the
#     world star's distances are kept better than the rest; a rotated sky
#     is rotated back by Procrustes; the build's frames are quarters apart.
_sq = _np214.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float)
_D218 = _np214.sqrt(((_sq[:, None, :] - _sq[None, :, :]) ** 2).sum(axis=2))
_W218 = _np214.ones_like(_D218)
_np214.fill_diagonal(_W218, 0.0)
_X218 = _mv214.smacof(_D218, _W218, _sq * 0.5 + _np214.array([[.1, 0, .05], [0, .1, 0], [.05, 0, 0], [0, 0, .1]]))
_fid218 = _mv214.fidelity(_D218, _X218)
_rng218 = _np214.random.default_rng(7)
_B218 = _rng218.normal(size=(7, 3))
_th = 0.7
_R218 = _np214.array([[_np214.cos(_th), -_np214.sin(_th), 0], [_np214.sin(_th), _np214.cos(_th), 0], [0, 0, 1]])
_A218 = _B218 @ _R218
_Q218 = _mv214.procrustes(_A218, _B218)
# five holdings in two groups and a world star that every one of them follows
_C218 = _np214.full((6, 6), 0.2)
for _g in ((0, 1, 2), (3, 4)):
    for _i in _g:
        for _j in _g:
            _C218[_i, _j] = 0.8
_C218[5, :] = _C218[:, 5] = 0.65
_np214.fill_diagonal(_C218, 1.0)
_P218, _f218 = _mv214.place3(_C218, world=5)
_Dt = _np214.sqrt(2 * (1 - _C218))
_dP = _np214.sqrt(((_P218[:, None, :] - _P218[None, :, :]) ** 2).sum(axis=2))
_err_w = float(_np214.std([_dP[5, i] / _Dt[5, i] for i in range(5)]))
_err_o = float(_np214.std([_dP[i, j] / _Dt[i, j] for i in range(5) for j in range(i + 1, 5)]))
_ok218 = (_fid218 > 0.999 and _np214.allclose(_A218 @ _Q218, _B218, atol=1e-9) and _P218.shape == (6, 3) and _f218 > 0.9
          and _err_w <= _err_o + 1e-9 and _mv214.WORLD_WEIGHT > 1 and _mv214.QSTEP == 63 and _mv214.FRAMES >= 5)
print(f"218 the constellation        {ok(_ok218)}  a square's distances kept (r {_fid218:.4f}); a sky turned 0.7 radians turned back; "
      f"the world star's distances held {_err_w:.3f} against {_err_o:.3f} for the rest; picture r {_f218:.2f}")

# 219 the rotation graph's motion: a name circling clockwise reads 100%
#     clockwise at the right speed and distance; its reading names the next
#     stop; near the centre it says the quadrant means little; against the
#     turn it says so; what followed a crossing into leading is counted.
_pts219 = [(100 + 2 * _np214.cos(_np214.radians(90 - 30 * k)), 100 + 2 * _np214.sin(_np214.radians(90 - 30 * k))) for k in range(14)]
_m219 = _ro214.motion(_pts219)
_r1 = _ro214.reading({"q": "leading", "motion": {"heading": -90, "distance": 2.0}, "since": 3, "x": 102, "y": 100}, "week")
_r2 = _ro214.reading({"q": "leading", "motion": {"heading": -90, "distance": 0.5}, "since": 1, "x": 100.4, "y": 100.2}, "week")
_r3 = _ro214.reading({"q": "leading", "motion": {"heading": 90, "distance": 2.0}, "since": 2, "x": 102, "y": 100}, "week")
_hs219 = _ro214.history([(99, 101), (99, 101), (101, 101), (101, 99), (101, 99), (101, 99)])
_fw219 = _ro214.followed([([99, 101, 101, 101, 101, 101, 101], [101, 101, 101, 101, 101, 101, 101])],
                         [[100, 100, 102, 104, 106, 110, 110]], [[100] * 7], (4,))
_ok219 = (_m219 and _m219["clockwise"] == 100 and abs(_m219["speed"] - 1.04) < 0.011 and abs(_m219["distance"] - 2.0) < 1e-6
          and _r1.startswith("Leading for 3 weeks") and "next stop is weakening" in _r1
          and "close to the centre" in _r2 and "against the usual clockwise path" in _r3
          and _hs219["path"] == [["improving", 2], ["leading", 1], ["weakening", 3]] and _hs219["share"]["weakening"] == 50
          and _fw219["leading"]["4"] == {"n": 1, "avg": 10.0, "med": 10.0, "beat": 100} and _fw219["lagging"]["4"] is None)
print(f"219 how a name is moving     {ok(_ok219)}  a clockwise circle: {_m219['clockwise'] if _m219 else '?'}% clockwise, speed "
      f"{_m219['speed'] if _m219 else '?'}, 2.0 from the centre; '{_r1[:60]}…'; a crossing into leading then +10% is counted")

# 220 junk bonds: a spread pays for its own default rate divided by what a
#     default loses; against an average year it leaves the rest; a 21%
#     rise over two years is 10% a year; junk against stocks in a crash.
from datetime import date as _da220, timedelta as _td220
_days220 = [(_da220(2019, 1, 1) + _td220(days=i)).isoformat() for i in range(912)]
_eq220, _hy220, _ve, _vh = {}, {}, 100.0, 100.0
for _d in _days220:
    _r = -0.01 if "2020-02-20" <= _d <= "2020-03-23" else 0.0003
    _ve *= 1 + _r
    _vh *= 1 + _r / 2
    _eq220[_d], _hy220[_d] = _ve, _vh
_js220 = _bo214.junk_vs_stocks(_hy220, _eq220)
_tr220 = {(_da220(2024, 1, 1) + _td220(days=i)).isoformat(): 100 * 1.21 ** (i / 731) for i in range(732)}
_re220 = _bo214.realised(_tr220, {"2024-01-01": 7.5}, {"2024-01-01": 4.0, "2025-12-31": 3.0})
_cv220 = _bo214.default_cover(1.91, 0.56)
_fy220 = _bo214.fund_yield(_bo214.CAND["EUNW.DE"], {"BAMLHE00EHYIEY": {"2026-10-13": 6.53}}, {}, None)
_ok220 = (_bo214.implied_default(3.0) == 5.0 and _bo214.implied_default(3.0, 0.2) == 3.75
          and _cv220 == {"loss": 0.34, "left": 1.57, "times": 5.7} and _bo214.default_cover(1.0, None) is None
          and _js220 and [w["k"] for w in _js220["windows"]] == ["2020"]
          and _js220["windows"][0]["eq"] < _js220["windows"][0]["hy"] < 0 and abs(_js220["windows"][0]["eq"] - (0.99 ** 33 - 1) * 100) < 0.1
          and 40 <= _js220["capture"] <= 60 and _js220["dd"][-1][0] == _days220[-1]
          and _re220 and abs(_re220["ret"] - 10.0) < 0.05 and _re220["yield0"] == 7.5 and _re220["oas1"] == 3.0
          and _fy220 == 6.03)
print(f"220 junk arithmetic          {ok(_ok220)}  300bp pays for 5% a year defaulting at 40% recovery; BB's 191bp leaves "
      f"{_cv220['left']}pp after an average year ({_cv220['times']}×); in a 1%-a-day crash junk at half the pace fell "
      f"{_js220['windows'][0]['hy'] if _js220 else '?'}% against {_js220['windows'][0]['eq'] if _js220 else '?'}%")

# 221 junk is wired: the rating series come from FRED and may be charted,
#     the part is on the page with its section and summary, two UCITS funds
#     join the what-if, and the long view reads Yahoo one symbol at a time.
_bj221 = open(os.path.join(HERE, "backend", "static", "bonds.js"), encoding="utf-8").read()
_bs221 = open(os.path.join(HERE, "backend", "pf", "bonds.py"), encoding="utf-8").read()
_ok221 = ("junk" in _bo214.PARTS and all(x in _bo214.US_DAILY for x in ("BAMLH0A1HYBB", "BAMLH0A2HYB", "BAMLH0A3HYC",
                                                                         "BAMLH0A1HYBBEY", "BAMLH0A2HYBEY", "BAMLH0A3HYCEY",
                                                                         "BAMLHYH0A0HYM2TRIV", "BAMLHE00EHYITRIV"))
          and all(_bo214.HIST_OK.match(x) for x in ("BAMLH0A1HYBB", "BAMLH0A3HYC"))
          and '"junk"' in _bj221 and '["s-junk", "Junk"]' in _bj221 and "function renderJunk" in _bj221 and "junk: renderJunk" in _bj221
          and {c["sym"] for c in _bo214.CANDIDATES if c.get("junk")} == {"EUNW.DE", "IHYU.L"}
          and all(c["group"] == "High yield" for c in _bo214.CANDIDATES if c.get("junk"))
          and _bo214.LONG_RUN_DEFAULT == {"BB": 0.56, "B": 2.93, "CCC": 26.12}
          and "threads=False" in _bs221[_bs221.index("def _load_junk_px"):_bs221.index("def _window_dd")])
print(f"221 junk is wired            {ok(_ok221)}  eight more FRED series, charted; the Junk section and its summary lines; "
      f"EUNW.DE and IHYU.L in the what-if; S&P's 1981–2024 default rates; Yahoo asked one symbol at a time")

# 222 the rest of the build is wired: the hours route and the bar; DIV gone;
#     the rotation page zooms, goes full width and full screen and shows
#     what followed; the stars focus and play; the build moved.
from pf import api as _api222
_paths222 = {r.path for r in _api222.router.routes}
_rj222 = open(os.path.join(HERE, "backend", "static", "rrg.js"), encoding="utf-8").read()
_sj222 = open(os.path.join(HERE, "backend", "static", "stars.js"), encoding="utf-8").read()
_mh222 = open(os.path.join(HERE, "backend", "static", "module.html"), encoding="utf-8").read()
_ok222 = ("/api/pf/hours" in _paths222 and 'id="termHours"' in _tj216 and "/api/pf/hours" in _tj216
          and 'code: "DIV"' not in _tj216 and '"DIV"' not in _mh222
          and all(x in _rj222 for x in ("function wireZoom", "requestFullscreen", "rrg_wide", "function followedPanel",
                                         "function detail", "clipPath"))
          and all(x in _sj222 for x in ("setFocus", "stScrub", "world")) and _BUILD >= 32)
print(f"222 build 32 is wired        {ok(_ok222)}  the hours route and the strip in the bar; DIV gone; RRG zooms, widens, fills the screen "
      f"and shows what followed; the stars focus and play; build {_BUILD}")

# ---------------------------------------------------------------------------
# Build 33: the day starts again at each market's close (pf/sessions.py),
# pre-market and after-hours tagged, the last session kept; Frankfurt and
# "since" out of the bar; MAC's rows open what moved inside them
# ---------------------------------------------------------------------------
from datetime import datetime as _dt223
from zoneinfo import ZoneInfo as _zi223
from pf import sessions as _se223, pricing as _pr223, stream as _st223, brief as _bf223, macparts as _mp223, macro as _mc223
_NY223, _AM223 = _zi223("America/New_York"), _zi223("Europe/Amsterdam")
_ny = lambda *a: _dt223(*a, tzinfo=_NY223)
_am = lambda *a: _dt223(*a, tzinfo=_AM223)

# 223 which close the day is measured from: during New York's session the
#     day before; after its close that close; in the next pre-market still
#     that close, even when Yahoo has no bar for today yet (the bug: the two
#     newest bars measured from two sessions back); Amsterdam before its
#     open yesterday; an exchange this does not know keeps the last two bars.
_b223 = [("2026-10-13", 100.0), ("2026-10-14", 98.0), ("2026-10-15", 99.0)]
_p_open = _se223.pick(_b223, "US", _ny(2026, 10, 15, 12, 0))
_p_after = _se223.pick(_b223, "US", _ny(2026, 10, 15, 16, 30))
_p_pre = _se223.pick(_b223[:2], "US", _ny(2026, 10, 15, 6, 0))
_p_eu = _se223.pick([("2026-10-14", 30.08), ("2026-10-15", 29.5)], "EURONEXT", _am(2026, 10, 15, 8, 30))
_p_unk = _se223.pick(_b223, None, _ny(2026, 10, 15, 16, 30))
_ok223 = (_p_open["ref_d"] == "2026-10-14" and _p_open["ref"] == 98.0 and _p_open["prev"] == 100.0 and _p_open["last"] == 99.0
          and _p_after["ref_d"] == "2026-10-15" and _p_after["prev_d"] == "2026-10-14"
          and _p_pre["ref_d"] == "2026-10-14" and _p_pre["last"] == 98.0 and _p_pre["prev_d"] == "2026-10-13"
          and _p_eu["ref_d"] == "2026-10-14" and _p_unk["ref_d"] == "2026-10-14"
          and [_se223.exchange_of(t) for t in ("MSFT", "BRK-B", "INGA.AS", "EQQQ.DE", "VOD.L", "EURUSD=X", "^GSPC", "X.ZZ")]
          == ["US", "US", "EURONEXT", "XETRA", "LSE", None, None, None])
print(f"223 the close it starts from {ok(_ok223)}  NY at noon: from 14/10; at 16:30 from 15/10's close; in 15/10's pre-market with no bar yet, "
      f"from 14/10, not 13/10; Amsterdam at 08:30 from 14/10; FX and indices keep the last two bars")

# 224 the session and its tag: New York pre at 06:00, open at noon, after
#     hours at 17:00, shut at 21:00 and on Thanksgiving, after hours at 14:00
#     the day after (it closes at one); Amsterdam shut at 08:30, open at ten;
#     a moved price outside the session is "pre" or "after", inside it nothing;
#     the base is due again at the next open or close.
_s224 = [_se223.state("US", _ny(2026, 10, 15, h, m)) for h, m in ((6, 0), (12, 0), (17, 0), (21, 0))]
_ok224 = (_s224 == ["pre", "open", "post", "closed"] and _se223.state("US", _ny(2026, 11, 26, 12, 0)) == "closed"
          and _se223.state("US", _ny(2026, 11, 27, 14, 0)) == "post"
          and _se223.state("EURONEXT", _am(2026, 10, 15, 8, 30)) == "closed" and _se223.state("EURONEXT", _am(2026, 10, 15, 10, 0)) == "open"
          and _se223.tag("pre", 101.0, 100.0) == "pre" and _se223.tag("post", 101.0, 100.0) == "after"
          and _se223.tag("closed", 101.0, 100.0) == "after" and _se223.tag("open", 101.0, 100.0) is None
          and _se223.tag("pre", 100.0, 100.0) is None
          and _se223.next_change(["US"], _ny(2026, 10, 15, 12, 0)) == _ny(2026, 10, 15, 16, 0).timestamp()
          and _se223.next_change(["US", "EURONEXT"], _ny(2026, 10, 15, 21, 0)) == _am(2026, 10, 16, 9, 0).timestamp())
print(f"224 the session and its tag  {ok(_ok224)}  NY {', '.join(_s224)} at 06:00, noon, 17:00, 21:00; shut for Thanksgiving, after hours at 14:00 the day after; "
      f"a move before the open is 'pre', after the close 'after'; next rebuild at the close, or at Amsterdam's 09:00 open")

# 225 a quote says which close it is measured from, and the last session in
#     full; the live fold tags each row by its exchange's session and passes
#     the last session through; the dashboard takes the currency's rate on the
#     reference date; the base is rebuilt after an open or a close.
_q225 = _pr223._quote("TST", [("2026-10-13", 100.0), ("2026-10-14", 98.0)], "test", _ny(2026, 10, 15, 6, 0))
_q225b = _pr223._quote("TST", [("2026-10-13", 100.0), ("2026-10-14", 98.0), ("2026-10-15", 99.0)], "test", _ny(2026, 10, 15, 12, 0))
_base225 = {"positions": [{"isin": "A", "ticker": "TST", "qty": 10, "currency": "EUR", "price": 98.0, "prev_close": 98.0,
                           "exchange": "US", "session": "pre", "last_eur": -20.0, "last_pct": -2.0},
                          {"isin": "B", "ticker": "INGA.AS", "qty": 10, "currency": "EUR", "price": 30.0, "prev_close": 30.0,
                           "exchange": "EURONEXT", "session": "closed", "last_eur": 1.0, "last_pct": 0.33}],
            "fx_symbol": {}, "cash_eur": 0.0, "last_eur": -19.0, "last_pct": -1.4, "last_dates": ["2026-10-14"]}
_f225 = _st223.fold(_base225, {"TST": {"price": 99.0}}, {"US": "pre", "EURONEXT": "closed"})
_f225b = _st223.fold(_base225, {"TST": {"price": 99.0}}, {"US": "open", "EURONEXT": "open"})
_sv225 = open(os.path.join(HERE, "backend", "pf", "service.py"), encoding="utf-8").read()
_sm225 = open(os.path.join(HERE, "backend", "pf", "stream.py"), encoding="utf-8").read()
_ok225 = (_q225["prev_close"] == 98.0 and _q225["price"] == 98.0 and _q225["ref_date"] == "2026-10-14"
          and _q225["last_session"] == {"d": "2026-10-14", "close": 98.0, "prev_d": "2026-10-13", "prev": 100.0, "pct": -2.0}
          and _q225["session"] == "pre" and _q225b["prev_close"] == 98.0 and _q225b["price"] == 99.0
          and _f225["positions"][0]["day_tag"] == "pre" and _f225["positions"][0]["day_eur"] == 10.0
          and _f225["positions"][1]["day_tag"] is None and _f225["totals"]["day_tags"] == ["pre"]
          and _f225["totals"]["last_eur"] == -19.0 and _f225["positions"][0]["last_pct"] == -2.0
          and _f225b["totals"]["day_tags"] == [] and _f225b["positions"][0]["day_tag"] is None
          and 'fx_on(p.currency, q.get("ref_date"))' in _sv225 and '"valid_until": nxt + 150' in _sv225
          and 'b.get("valid_until") and time.time() > b["valid_until"]' in _sm225)
print(f"225 quotes, the fold, the base {ok(_ok225)}  before New York opens a quote is measured from 14/10's 98 with 14/10's −2.0% as the last session; "
      f"a pre-market 99 reads +€10 tagged pre, inside the session untagged; FX from the reference date; the base rebuilt at the bell")

# 226 the movers: a holding whose market has not traded since its close
#     shows the session that just finished, labelled last; one that is
#     trading shows now, and the ranking follows what is shown.
_pos226 = [{"isin": "A", "ticker": "AAA", "value_eur": 100.0, "weight": 50.0, "day_pct": 0.0, "day_eur": 0.0, "session": "closed",
            "last_pct": -3.0, "last_eur": -3.0},
           {"isin": "B", "ticker": "BBB", "value_eur": 90.0, "weight": 45.0, "day_pct": 1.0, "day_eur": 0.9, "session": "open",
            "last_pct": 2.0, "last_eur": 1.8},
           {"isin": "C", "ticker": "CCC", "value_eur": 5.0, "weight": 2.5, "day_pct": 0.0, "day_eur": 0.0, "session": "pre",
            "last_pct": 4.0, "last_eur": 0.2},
           {"isin": "D", "ticker": "DDD", "value_eur": 5.0, "weight": 2.5, "day_pct": -0.5, "day_eur": 0.0, "session": "pre",
            "last_pct": 5.0, "last_eur": 0.2}]
_w226 = _bf223.what_changed(_pos226, n_weight=2, n_side=1)
_tj226 = open(os.path.join(HERE, "backend", "static", "term.js"), encoding="utf-8").read()
_ix226 = open(os.path.join(HERE, "backend", "static", "index.html"), encoding="utf-8").read()
_ok226 = ([r["isin"] for r in _w226["movers"]] == ["C", "D"] and _w226["weights"][0]["last_pct"] == -3.0
          and _w226["weights"][0]["session"] == "closed"
          and "const shownMove" in _tj226 and "shownMove(r)" in _ix226 and 'id="chg"' in _ix226 and "#chg .row[data-isin=" in _ix226)
print(f"226 the movers               {ok(_ok226)}  a shut market with nothing since its close shows its last session (+4% up, ranked); "
      f"a pre-market −0.5% shows as it is; the rows move with the feed")

# 227 the bar: Amsterdam and New York only; the portfolio page's bar says
#     as of and how many are open, not since when; the day's tile carries the
#     tag and the last session on both pages.
from pf import market as _mk227
_ph227 = open(os.path.join(HERE, "backend", "static", "portfolio.html"), encoding="utf-8").read()
_ok227 = ([h[0] for h in _mk227.HOURS] == ["AMS", "NYC"] and [x["k"] for x in _mk227.hours()["exchanges"]] == ["AMS", "NYC"]
          and "open · since ${dmy(T.since)}" not in _ph227 and 'id="dayLast"' in _ph227 and 'id="dayTag"' in _ph227
          and 'id="fDayL"' in _ix226 and 'id="fDayT"' in _ix226 and "const lastLine" in _tj226 and "const dayTag" in _tj226
          and '{k:"last_pct", t:"Last session"' in _ph227)
print(f"227 the bar and the tiles    {ok(_ok227)}  AMS and NY in the bar; 'since' gone from the portfolio's bar; both day tiles carry the tag "
      f"and the last session; a Last session column")

# 228 what moved inside CPI: each part's share is its weight times its
#     change, the parts that are listed leave a residual, the sentence names
#     the largest either way; payrolls add up with the rest; GDP's
#     contributions pass through; the weights add up.
def _ser228(g, months=15, start=(2025, 7)):
    out, v = {}, 100.0
    y, m = start
    for k in range(months):
        out[f"{y:04d}-{m:02d}-01"] = v
        v *= 1 + g / 100
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out
_g228 = {"CPIAUCSL": 0.3, "CPIUFDSL": 0.2, "CPIENGSL": -1.0, "CUSR0000SACL1E": 0.1, "CUSR0000SAH1": 0.4, "CUSR0000SAM2": 0.3, "CUSR0000SAS4": 0.5}
_cpi228 = {r[1]: _ser228(_g228.get(r[1], 0.2)) for r in _mp223.FAMILIES["us_cpi"]["rows"]}
_P228 = _mp223.build("us_cpi", "2026-09-01", _cpi228, "CPI m/m")
_by228 = {r["id"]: r for r in _P228["rows"]}
_rest228 = 0.3 - sum(_mp223.CPI_W[i] / 100 * _g228[i] for i in _mp223.FAMILIES["us_cpi"]["parts"])
_jobs228 = {"PAYEMS": {"2026-08-01": 150000.0, "2026-09-01": 150100.0}, "USCONS": {"2026-08-01": 8000.0, "2026-09-01": 8020.0},
            "MANEMP": {"2026-08-01": 12000.0, "2026-09-01": 11990.0}}
for _i in _mp223.FAMILIES["us_jobs"]["parts"]:
    _jobs228.setdefault(_i, {"2026-08-01": 1000.0, "2026-09-01": 1000.0})
_J228 = _mp223.build("us_jobs", "2026-09-01", _jobs228, "Non-Farm Employment Change")
_G228 = _mp223.build("us_gdp", "2026-04-01", {"A191RL1Q225SBEA": {"2026-04-01": 2.2}, "DPCERY2Q224SBEA": {"2026-04-01": 2.5},
                                             "A006RY2Q224SBEA": {"2026-04-01": 0.8}, "A019RY2Q224SBEA": {"2026-04-01": -1.1},
                                             "A822RY2Q224SBEA": {"2026-04-01": 0.0}}, "Advance GDP q/q")
_rj228 = next((r for r in _J228["rows"] if r["id"] == "_rest"), {})
_ok228 = (_P228["available"] and _P228["exact"] and abs(_by228["CPIAUCSL"]["mom"] - 0.3) < 1e-6
          and abs(_by228["CUSR0000SAH1"]["c_mom"] - 0.142) < 0.0011 and abs(_by228["CPIENGSL"]["c_mom"] + 0.064) < 0.0011
          and abs(_by228["CPIAUCSL"]["yoy"] - ((1.003 ** 12 - 1) * 100)) < 0.001
          and abs(_by228["_rest"]["c_mom"] - _rest228) < 0.002
          and _P228["say"].startswith("Shelter added the most (+0.14pp); energy took away the most (-0.06pp)")
          and _rj228.get("diff") == 90.0 and _J228["say"].startswith("Construction added the most (+20K); manufacturing took away the most (-10K)")
          and _G228["say"] == "Consumption added the most (+2.50pp); net exports took away the most (-1.10pp), of +2.2% annualised."
          and abs(sum(_mp223.HICP_W.values()) - 1000) < 0.01
          and abs(_mp223.CPI_W["CPIUFDSL"] + _mp223.CPI_W["CPIENGSL"] + _mp223.CPI_W["CPILFESL"] - 100) < 0.01
          and abs(_mp223.CPI_W["CUSR0000SACL1E"] + _mp223.CPI_W["CUSR0000SASLE"] - _mp223.CPI_W["CPILFESL"]) < 0.01)
print(f"228 what moved inside        {ok(_ok228)}  CPI +0.30%: shelter {_by228['CUSR0000SAH1']['c_mom']:+.3f}pp, energy {_by228['CPIENGSL']['c_mom']:+.3f}pp, "
      f"the rest {_by228['_rest']['c_mom']:+.3f}pp; payrolls +100K with 90K from the rest; GDP's parts as BEA gives them; the weights add up")

# 229 which release opens which parts, and for which period: ADP, GDP's
#     price index and Germany's CPI open nothing borrowed; a period FRED does
#     not have yet shows the latest, marked; a weekly series a week behind is
#     read from its last week; JOLTS actuals stored under the old rule are
#     read again, once.
_f229 = [_mp223.family_of(c, t) for c, t in (("USD", "CPI m/m"), ("USD", "Core CPI y/y"), ("USD", "Non-Farm Employment Change"),
                                             ("USD", "ADP Non-Farm Employment Change"), ("USD", "Final GDP q/q"),
                                             ("USD", "Final GDP Price Index q/q"), ("EUR", "CPI Flash Estimate y/y"),
                                             ("EUR", "German Prelim CPI m/m"), ("USD", "Core PPI m/m"), ("USD", "JOLTS Job Openings"))]
_L229 = _mp223.build("us_cpi", "2026-10-01", _cpi228, "CPI m/m")
_tmp229 = os.path.join(_tf181.gettempdir(), "pf_mac33check.db")
for _x in (_tmp229, _tmp229 + "-wal", _tmp229 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_orig229 = store.DB_PATH
store.DB_PATH = _tmp229
_fx229 = None
try:
    store.init_db()
    _mc223.upsert([{"date": "2026-09-29", "time": "16:00", "ts": 1_790_690_400, "currency": "USD", "title": "JOLTS Job Openings",
                    "impact": "high", "rank": 3, "forecast": "7.2M", "previous": "7.27M"}])
    with store.conn() as _c229:
        _c229.execute("UPDATE mac_event SET actual='7.33M' WHERE title='JOLTS Job Openings'")
    _fx1 = _mc223.fix_jolts()
    with store.conn() as _c229:
        _cleared229 = _c229.execute("SELECT actual FROM mac_event WHERE title='JOLTS Job Openings'").fetchone()[0]
        # read again under the new rule: August's 7.08M
        _c229.execute("UPDATE mac_event SET actual='7.08M' WHERE title='JOLTS Job Openings'")
    _fx229 = (_fx1, _mc223.fix_jolts())
    with store.conn() as _c229:
        _left229 = _c229.execute("SELECT actual FROM mac_event WHERE title='JOLTS Job Openings'").fetchone()[0]
finally:
    store.DB_PATH = _orig229
    for _x in (_tmp229, _tmp229 + "-wal", _tmp229 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
_ok229 = (_f229 == ["us_cpi", "us_cpi", "us_jobs", None, "us_gdp", None, "ea_hicp", None, "us_ppi", "us_jolts"]
          and _L229["ref"] == "2026-09-01" and _L229["exact"] is False and _L229["asked"] == "2026-10-01"
          and _mp223.measure({"2026-09-26": 1716000.0}, "2026-10-03", "level_k", "w") == 1716.0
          and _mp223.default_measure("us_cpi", "Core CPI y/y") == "yoy" and _mp223.default_measure("us_cpi", "CPI m/m") == "mom"
          and _fx229 == (1, 0) and _cleared229 is None and _left229 == "7.08M")
print(f"229 which parts, which month {ok(_ok229)}  ADP, GDP's deflator and Germany's CPI borrow nothing; October not on FRED shows September, marked; "
      f"continuing claims a week behind read; the old JOLTS actual read again once")

# 230 MAC's panel is wired: the route, rows that open, the page fetches it,
#     the build moved.
from pf import api as _api230
_paths230 = {r.path for r in _api230.router.routes}
_mj230 = open(os.path.join(HERE, "backend", "static", "mac.js"), encoding="utf-8").read()
_mpy230 = open(os.path.join(HERE, "backend", "pf", "macro.py"), encoding="utf-8").read()
_ok230 = ("/api/pf/mac/detail" in _paths230 and "/api/pf/mac/detail?" in _mj230 and "function wireDetails" in _mj230
          and 'wireDetails("#weekT", V.week)' in _mj230 and 'wireDetails("#pastT", V.past || [])' in _mj230
          and 'r["family"] = macparts.family_of(r["ccy"], r["title"])' in _mpy230 and "fix_jolts()" in _mpy230
          and len(_mp223.FAMILIES) >= 10 and _BUILD >= 33)
print(f"230 MAC's panel is wired     {ok(_ok230)}  the detail route; rows this week and the last four weeks open it; "
      f"{len(_mp223.FAMILIES)} families of parts; build {_BUILD}")

# 231 a stored price history is asked again by its exchange's clock: when
#     the newest finished session is missing, when it was fetched before that
#     session's close (so its last bar may be a price from during it), every
#     quarter of an hour while the exchange trades; never twice in ten
#     minutes; the weekend after Friday's close stored asks nothing. The
#     benchmark on PORT stood at a mid-session price for three days before.
from pf import pricing as _pr231
_S231 = "2025-12-30"
_t231 = lambda *a: _dt223(*a, tzinfo=_AM223)
_save231 = dict(_pr231._HIST_AT)
try:
    _pr231._HIST_AT.clear()
    _a231 = _pr231.history_due("SXR8.DE", {"2025-12-30": 629.0, "2026-10-13": 753.25}, _S231, _t231(2026, 10, 15, 12, 0))
    _pr231._HIST_AT["SXR8.DE"] = _t231(2026, 10, 15, 11, 55).timestamp()
    _b231 = _pr231.history_due("SXR8.DE", {"2025-12-30": 629.0, "2026-10-13": 753.25}, _S231, _t231(2026, 10, 15, 12, 0))
    _pr231._HIST_AT["SXR8.DE"] = _t231(2026, 10, 15, 15, 0).timestamp()
    _c231 = _pr231.history_due("SXR8.DE", {"2025-12-30": 629.0, "2026-10-15": 741.0}, _S231, _t231(2026, 10, 15, 18, 0))
    _pr231._HIST_AT["SXR8.DE"] = _t231(2026, 10, 15, 17, 45).timestamp()
    _d231 = _pr231.history_due("SXR8.DE", {"2025-12-30": 629.0, "2026-10-15": 741.0}, _S231, _t231(2026, 10, 15, 18, 0))
    _pr231._HIST_AT["SXR8.DE"] = _t231(2026, 10, 15, 11, 0).timestamp()
    _e231 = (_pr231.history_due("SXR8.DE", {"2025-12-30": 629.0, "2026-10-14": 745.0}, _S231, _t231(2026, 10, 15, 11, 20)),
             _pr231.history_due("SXR8.DE", {"2025-12-30": 629.0, "2026-10-14": 745.0}, _S231, _t231(2026, 10, 15, 11, 12)))
    _pr231._HIST_AT["SXR8.DE"] = _t231(2026, 10, 16, 18, 0).timestamp()
    _f231 = _pr231.history_due("SXR8.DE", {"2025-12-30": 1.0, "2026-10-16": 2.0}, _S231, _t231(2026, 10, 17, 12, 0))
    _g231 = _pr231.history_due("SXR8.DE", {"2026-01-05": 1.0, "2026-10-16": 2.0}, _S231, _t231(2026, 10, 17, 12, 0))
finally:
    _pr231._HIST_AT.clear()
    _pr231._HIST_AT.update(_save231)
_src231 = open(os.path.join(HERE, "backend", "pf", "pricing.py"), encoding="utf-8").read()
_gh231 = _src231[_src231.index("def get_history("):_src231.index("def get_fx(")]
_ghm231 = _src231[_src231.index("def get_history_many("):_src231.index("def get_history(")]
_ok231 = (_a231 and not _b231 and _c231 and not _d231 and _e231 == (True, False) and not _f231 and _g231
          and "history_due(ticker, cached, start)" in _gh231 and "need_from = max(cached)" in _gh231
          and "history_due(t, cached, start)" in _ghm231 and "stale_cut" not in _ghm231 and "timedelta(days=3)" not in _gh231)
print(f"231 histories by the clock   {ok(_ok231)}  13/10 stored at noon on 15/10: asked again; five minutes after a fetch: not; fetched at 15:00, "
      f"at 18:00 after the 17:30 close: again, fetched at 17:45: not; every quarter hour in the session; Saturday after Friday's close: not; "
      f"a history that starts too late: asked")

# 232 a turbo in euros on a dollar share: (S - FN) / R / fx, the financing
#     level and the share both in dollars; the name's LV gives the share the
#     day it was written; the financing level between and beyond its dated
#     points; the share at the moment of a trade; the conversion decided by
#     the share's currency, not by whichever fits.
from pf import leveraged as _lv232
_sn232 = _lv232.parse("SG SNDK CLASSIC TURBO LONG SL 360.36 FN 325.683 R 100")
_ab232 = _lv232.parse("SG Abbott Labs BEST Turbo Long SL 73.9439 FN 73.9439 R 5 LV 3.33")
_px232 = _lv232.turbo_price(_sn232, 1357.33, fx=1.1208)
_P232 = _lv232.FinPath([("2026-09-08", 73.6477, 73.6477, "export"), ("2026-10-15", 73.9439, 73.9439, "typed")])
_r232 = (73.9439 / 73.6477) ** (365 / 37) - 1
_mid232 = _P232.at("2026-09-26")
_fwd232 = _P232.at("2026-11-15")
_back232 = _P232.at("2026-01-28")
_one232 = _lv232.FinPath([("2026-09-08", 73.6477, 73.6477, "export")])
_near232 = _lv232.FinPath([("2026-10-08", 73.9, None, "typed"), ("2026-10-15", 73.95, None, "typed")])
_cl232 = {"2026-01-27": 108.27, "2026-01-28": 106.05}
_fx232 = {"2026-01-27": 1.2010, "2026-01-28": 1.2018}
_ok_fx = _lv232.calibrate_turbo(_ab232, [("2026-01-28T12:30:00", (108.27 - 72.25) / 5 / 1.2018)], _cl232, _fx232,
                                fin_at=lambda d: 72.25, ex="US")
# priced as if no conversion were needed: with the dollar share it is 17% off, and must not pass
_no_fx = _lv232.calibrate_turbo(_ab232, [("2026-01-28T12:30:00", (108.27 - 72.25) / 5)], _cl232, _fx232,
                                fin_at=lambda d: 72.25, ex="US")
_ok232 = (abs(_px232 - (1357.33 - 325.683) / 100 / 1.1208) < 1e-9
          and abs(_lv232.implied_underlying(_sn232, _px232, fx=1.1208) - 1357.33) < 1e-6
          and abs(_lv232.implied_financing(_ab232, 6.387, 108.27, 1.2018) - (108.27 - 6.387 * 5 * 1.2018)) < 1e-9
          and _ab232.name_leverage == 3.33 and abs(_ab232.share_when_named - 73.9439 * 3.33 / 2.33) < 1e-9
          and abs(_P232.rate - _r232) < 1e-9 and 73.6477 < _mid232[0] < 73.9439 and _mid232[1] == _mid232[0]
          and abs(_fwd232[0] - 73.9439 * (1 + _r232) ** (31 / 365)) < 1e-9
          and abs(_back232[0] - 73.6477 / (1 + _r232) ** (223 / 365)) < 1e-9
          and _one232.rate == 0.0 and _one232.at("2027-01-01")[0] == 73.6477 and _near232.rate == 0.0
          and _lv232.underlying_at(_cl232, "2026-01-28T12:30:00", "US") == 108.27
          and _lv232.underlying_at(_cl232, "2026-01-28T16:00:00", "US") == 106.05
          and _lv232.underlying_at(_cl232, "2026-01-28", "US") == 106.05
          and _ok_fx.ok and _ok_fx.fx_applied and _ok_fx.error_pct == 0.0 and not _no_fx.ok
          and _lv232.underlying_ccy("ABT") == "USD" and _lv232.underlying_ccy("INGA.AS") == "EUR"
          and _lv232.underlying_ccy("VOD.L") == "GBP")
print(f"232 the turbo model          {ok(_ok232)}  SNDK at $1,357.33 and 1.1208: €{_px232:.3f}, not the dollar figure; LV 3.33 says ABT stood at "
      f"${_ab232.share_when_named:.2f}; FN 73.6477 on 8/9 to 73.9439 on 15/10 is {_P232.rate * 100:.2f}% a year; a trade at 12:30 "
      f"takes the close before New York opened; a price fitted only without the conversion fails")

# 233 recording financing levels: an import records the level in each
#     turbo's name, dated that day, never over one typed in; the route takes
#     DEGIRO's current name or a number, refuses another product's ratio, a
#     level far from the name's and text it cannot read; the stop keeps its
#     distance; the dashboard prices from the path; the page offers it.
import types as _ty233
from pf import api as _api233
from fastapi import HTTPException as _HE233
_tmp233 = os.path.join(_tf181.gettempdir(), "pf_lev33check.db")
for _x in (_tmp233, _tmp233 + "-wal", _tmp233 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_orig233 = store.DB_PATH
store.DB_PATH = _tmp233
_res233 = {}
try:
    store.init_db()
    _inst = _ty233.SimpleNamespace(isin="DE000SU2VLR6", name="SG ABBOTT LABS BEST TURBO LONG SL 73.6477 FN 73.6477 R 5",
                                   kind="DERIVATIVE", currency="EUR", exchange="SCG", underlying="ABBOTT LABS", underlying_ticker="ABT")
    _cls = _ty233.SimpleNamespace(isin="DE000SW25KC3", name="SG APPLE CLASSIC TURBO LONG SL 152.36 FN 146.193 R 10",
                                  kind="DERIVATIVE", currency="EUR", exchange="SCG", underlying="APPLE", underlying_ticker="AAPL")
    store.commit_import([], {"DE000SU2VLR6": _inst, "DE000SW25KC3": _cls})
    _res233["export"] = store.fin_points("DE000SU2VLR6")
    _r1 = _api233.set_financing("DE000SU2VLR6", _api233.FinIn(text="SG Abbott Labs BEST Turbo Long SL 73.9439 FN 73.9439 R 5 LV 3.33", d="2026-10-15"))
    _r2 = _api233.set_financing("DE000SW25KC3", _api233.FinIn(text="147,5", d="2026-10-15"))
    _bad = []
    for _isin, _t in (("DE000SU2VLR6", "SG ABBOTT LABS BEST TURBO LONG SL 73.9 FN 73.9 R 10"), ("DE000SU2VLR6", "7.39"),
                      ("DE000SU2VLR6", "hello"), ("DE000SU2VLR6", "SG ABBOTT LABS BEST TURBO SHORT SL 73.9 FN 73.9 R 5")):
        try:
            _api233.set_financing(_isin, _api233.FinIn(text=_t))
        except _HE233 as _e:
            _bad.append(_e.status_code)
    store.commit_import([], {"DE000SU2VLR6": _inst})          # a later import on a day already typed in leaves it
    _res233["after"] = store.fin_points("DE000SU2VLR6")
finally:
    store.DB_PATH = _orig233
    for _x in (_tmp233, _tmp233 + "-wal", _tmp233 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
_sv233 = open(os.path.join(HERE, "backend", "pf", "service.py"), encoding="utf-8").read()
_ph233 = open(os.path.join(HERE, "backend", "static", "portfolio.html"), encoding="utf-8").read()
_paths233 = {r.path for r in _api233.router.routes}
_today233 = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()   # imports stamp UTC
_ok233 = (len(_res233.get("export") or []) == 1 and _res233["export"][0]["fn"] == 73.6477 and _res233["export"][0]["src"] == "export"
          and _res233["export"][0]["d"] == _today233
          and _r1["fn"] == 73.9439 and _r1["sl"] == 73.9439
          and [p["lv"] for p in _r1["points"] if p["d"] == "2026-10-15"] == [3.33]
          and abs(_r2["sl"] - 147.5 * 152.36 / 146.193) < 1e-9 and _bad == [400, 400, 400, 400]
          and [p["fn"] for p in _res233["after"] if p["d"] == "2026-10-15"] == [73.9439]
          and {"/api/pf/instrument/{isin}/financing", "/api/pf/instrument/{isin}/financing/{d}"} <= _paths233
          and "path = fin_path(isin, spec)" in _sv233 and "leveraged.underlying_ccy(ut)" in _sv233
          and "turbo_price(spec, s_now, fx=fx_now, financing=fn_now)" in _sv233
          and 'id="fFin"' in _ph233 and "/financing`" in _ph233 and 'data-fin="' in _ph233
          # a server started before the route existed says so (build 34)
          and "server is older than this page" in _ph233 and _BUILD >= 34)
print(f"233 recording financing      {ok(_ok233)}  an import records the name's 73.6477; DEGIRO's current name gives 73.9439 with LV 3.33; a classic's stop keeps "
      f"its distance; another ratio, 7.39, 'hello' and a short refused; the dashboard prices from the path; the page offers update")

# ---------------------------------------------------------------------------
# Build 35: the day's low and high, the equity curve zooms and reads out in
# its corner, exchange rates by the clock
# ---------------------------------------------------------------------------
from pf import dayrange as _dr234

# 234 the day's low and high: today's move is the day change, plus the last
#     session of any holding whose market closed today; a fall to -400 and a
#     finish at +100 is kept as low -400 and high +100 with their times, read
#     back from the table; the next day starts afresh; the feed records it.
_rows234 = [{"day_eur": -30.0, "last_eur": 120.0, "last_date": "2026-10-15"},     # closed today: its session counts
            {"day_eur": 50.0, "last_eur": -999.0, "last_date": "2026-10-14"}]     # still trading: yesterday does not
_tmp234 = os.path.join(_tf181.gettempdir(), "pf_day35check.db")
for _x in (_tmp234, _tmp234 + "-wal", _tmp234 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_orig234 = store.DB_PATH
store.DB_PATH = _tmp234
_res234 = {}
try:
    store.init_db()
    _dr234.reset_memory()
    _t0 = _dt223(2026, 10, 15, 10, 0, tzinfo=_AM223).timestamp()
    for _k, _v in enumerate((-100.0, -400.0, -250.0, 100.0)):
        _dr234.note(_v, _t0 + 3600 * (_k + 1))
    _res234["mem"] = _dr234.get("2026-10-15")
    _dr234.reset_memory()
    _res234["disk"] = _dr234.get("2026-10-15")
    _dr234.note(20.0, _dt223(2026, 10, 16, 9, 30, tzinfo=_AM223).timestamp())
    _res234["next"] = _dr234.get("2026-10-16")
    _res234["hist"] = [r["d"] for r in _dr234.history(5)]
except Exception as _e234:
    _res234["error"] = f"{_e234.__class__.__name__}: {_e234}"
finally:
    _dr234.reset_memory()
    store.DB_PATH = _orig234
    for _x in (_tmp234, _tmp234 + "-wal", _tmp234 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
_api234 = open(os.path.join(HERE, "backend", "pf", "api.py"), encoding="utf-8").read()
_sm234 = open(os.path.join(HERE, "backend", "pf", "stream.py"), encoding="utf-8").read()
_tj234 = open(os.path.join(HERE, "backend", "static", "term.js"), encoding="utf-8").read()
_ix234 = open(os.path.join(HERE, "backend", "static", "index.html"), encoding="utf-8").read()
_ph234 = open(os.path.join(HERE, "backend", "static", "portfolio.html"), encoding="utf-8").read()
_m234 = _res234.get("disk") or {}
_mm234, _nx234 = _res234.get("mem") or {}, _res234.get("next") or {}
_ok234 = ("error" not in _res234 and _dr234.today_move(_rows234, "2026-10-15") == 140.0 and _dr234.today_move([], "2026-10-15") is None
          and _mm234.get("lo") == -400.0 and _mm234.get("hi") == 100.0 and _mm234.get("last") == 100.0
          and _m234.get("lo") == -400.0 and _m234.get("hi") == 100.0
          and abs(_m234.get("lo_ts", 0) - (_t0 + 7200)) < 1 and abs(_m234.get("hi_ts", 0) - (_t0 + 14400)) < 1
          and _nx234.get("lo") == 20.0 and _nx234.get("hi") == 20.0 and _res234.get("hist") == ["2026-10-16", "2026-10-15"]
          and "dayrange.note(mv, now)" in _api234 and '"last_date": p.get("last_date")' in _sm234
          and "const rangeLine" in _tj234 and "hhmm" not in _tj234
          and 'id="fDayR"' in _ix234 and "rangeLine(p.day_range)" in _ix234
          and 'id="dayRange"' in _ph234 and "rangeLine(p.day_range)" in _ph234)
print(f"234 the day's low and high   {ok(_ok234)}  a holding shut today counts its session (+120 −30), one still trading only today (+50): +140; "
      f"−100, −400, −250, +100 keeps low −400 at 12:00 and high +100 at 14:00, read back from the table; 16/10 starts afresh")

# 235 the equity curve zooms and reads out in its corner: presets and a
#     dragged stretch set the window, double-click shows all of it, the
#     choice is remembered; the values under the pointer go to the readout
#     above the chart, not into a box over the lines; the stretch's return
#     is the book's time-weighted one and the tracker's from the same deposits.
_ok235 = (all(x in _ph234 for x in ("function presetWindow", "function curveWindow", "function curvePeriod", "function curveReadout",
                                     'id="cread"', 'id="czBar"', '"dblclick"', '"pf_curve_range"', 'id="csel"', 'class="curvebar"',
                                     "tb *= (q.b - (q.f || 0)) / r.b", "(p1.x / p0.x - 1) * 100"))
          and 'id="tip"' not in _ph234 and "tip.innerHTML" not in _ph234
          and all(f'["{l}",' in _ph234 for l in ("1M", "3M", "6M", "YTD", "1Y", "All")))
print(f"235 the curve zooms          {ok(_ok235)}  1M to All and a dragged stretch; double-click for all of it; the readout above the corner; "
      f"the stretch's return time-weighted for both lines; no box over the chart")

# 236 exchange rates by the clock: the rate is asked again every quarter of
#     an hour and after each close of New York or Amsterdam, never twice in
#     ten minutes, and from the newest stored day; a rate three days old no
#     longer passes for today's.
_save236 = dict(_pr231._HIST_AT)
try:
    _pr231._HIST_AT.clear()
    _c236 = {"2025-01-02": 1.03, "2026-10-15": 1.165}
    _pr231._HIST_AT["EURUSD=X"] = _t231(2026, 10, 15, 21, 50).timestamp()
    _a236 = _pr231.history_due("EURUSD=X", _c236, "2025-06-01", _t231(2026, 10, 15, 22, 5))
    _pr231._HIST_AT["EURUSD=X"] = _t231(2026, 10, 15, 22, 3).timestamp()
    _b236 = (_pr231.history_due("EURUSD=X", _c236, "2025-06-01", _t231(2026, 10, 15, 22, 10)),
             _pr231.history_due("EURUSD=X", _c236, "2025-06-01", _t231(2026, 10, 15, 22, 20)))
    _l236 = _pr231._last_close(("US", "EURONEXT"), _t231(2026, 10, 15, 23, 0))
finally:
    _pr231._HIST_AT.clear()
    _pr231._HIST_AT.update(_save236)
_src236 = open(os.path.join(HERE, "backend", "pf", "pricing.py"), encoding="utf-8").read()
_gf236 = _src236[_src236.index("def get_fx("):_src236.index("def fx_on(")]
_ok236 = (_a236 and _b236 == (False, True) and _l236 == _t231(2026, 10, 15, 22, 0).timestamp()
          and 'history_due(f"{pair}=X", cached, start)' in _gf236 and "since = max(cached)" in _gf236
          and "timedelta(days=3)" not in _gf236 and _BUILD >= 35)
print(f"236 rates by the clock       {ok(_ok236)}  fetched 21:50, New York shut at 22:00: asked again at 22:05; 7 minutes after a fetch not, "
      f"17 minutes after yes; from the newest stored day; build {_BUILD}")

# 237 a turbo is priced from the share it tracks, never as that share: a
#     symbol in its quote field becomes the share it tracks (the WDC turbo was
#     priced as WDC itself, at several times its worth); the name list knows Western
#     Digital and the Taiwanese ADR; an unknown name is looked up and stored;
#     the editor's field for a turbo is "Tracks"; the model beats a stale
#     manual price for a turbo; a missing share says so.
from pf import degiro as _dg237, service as _sv237, pricing as _pr237
import requests as _rq237
_tmp237 = os.path.join(_tf181.gettempdir(), "pf_trk36check.db")
for _x in (_tmp237, _tmp237 + "-wal", _tmp237 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_orig237 = store.DB_PATH
store.DB_PATH = _tmp237
_r237 = {}
_get237 = _rq237.get
try:
    store.init_db()
    with store.conn() as _c:
        _c.execute("INSERT INTO instrument (isin,name,kind,underlying,underlying_ticker,ticker) VALUES "
                   "('DE000FD2RUC3','SG WESTERN DIGITAL CLASSIC TURBO LONG SL 93.39 FN 86.5914 R 5','DERIVATIVE','WESTERN DIGITAL',NULL,'WDC'),"
                   "('DE000X','SG APPLE BEST TURBO LONG SL 1 FN 1 R 5','DERIVATIVE','APPLE','AAPL','AAPL.DE'),"
                   "('US5949181045','MICROSOFT CORPORATION','EQUITY',NULL,NULL,'MSFT'),"
                   "('DE000Y','SG NEWCO BEST TURBO LONG SL 9 FN 9 R 5','DERIVATIVE','NEWCO HOLDINGS',NULL,NULL)")
    store.init_db()                                   # the repair runs at start
    _ins = store.load_instruments()
    _r237["wdc"] = (_ins["DE000FD2RUC3"]["underlying_ticker"], _ins["DE000FD2RUC3"]["ticker"])
    _r237["aapl"] = (_ins["DE000X"]["underlying_ticker"], _ins["DE000X"]["ticker"])
    _r237["msft"] = _ins["US5949181045"]["ticker"]
    # an unknown name: looked up by name, a US listing first, stored once found
    _rq237.get = lambda *a, **k: type("R", (), {"json": staticmethod(lambda: {"quotes": [
        {"symbol": "NEWC.L", "quoteType": "EQUITY"}, {"symbol": "NEWC", "quoteType": "EQUITY"}, {"symbol": "NEWC-ETF", "quoteType": "ETF"}]})})()
    _pr237._SHARE_SEARCHED.clear()
    _r237["found"] = _sv237._find_underlying("DE000Y", _ins["DE000Y"])
    _r237["stored"] = store.load_instruments()["DE000Y"]["underlying_ticker"]
    # the editor: a symbol sent for a turbo is the share it tracks
    _api237 = __import__("pf.api", fromlist=["patch_instrument"])
    _api237.patch_instrument("DE000Y", _api237.InstrumentPatch(ticker="nwc"))
    _i2 = store.load_instruments()["DE000Y"]
    _r237["patched"] = (_i2["underlying_ticker"], _i2["ticker"])
finally:
    _rq237.get = _get237
    _pr237._SHARE_SEARCHED.clear()
    store.DB_PATH = _orig237
    for _x in (_tmp237, _tmp237 + "-wal", _tmp237 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
_svs237 = open(os.path.join(HERE, "backend", "pf", "service.py"), encoding="utf-8").read()
_ph237 = open(os.path.join(HERE, "backend", "static", "portfolio.html"), encoding="utf-8").read()
_ok237 = (_r237.get("wdc") == ("WDC", None) and _r237.get("aapl") == ("AAPL", None) and _r237.get("msft") == "MSFT"
          and _r237.get("found") == "NEWC" and _r237.get("stored") == "NEWC" and _r237.get("patched") == ("NWC", None)
          and _dg237.underlying_ticker_of("WESTERN DIGITAL") == "WDC" and _dg237.underlying_ticker_of("TAIWAN SEMICONDUCTOR") == "TSM"
          and _dg237.underlying_ticker_of("INTELLIA THERAPEUTICS") is None
          and "continue        # never quoted by a symbol of its own" in _svs237
          and 0 <= _svs237.find('elif (kind == "DERIVATIVE" and lev and lev.get("price") is not None and LEVMODE == "model"):')
                 < _svs237.find('elif meta.get("manual_price"):')
          and "which share it tracks is not known" in _svs237
          and '${isDeriv ? "Tracks" : "Quote symbol"}' in _ph237 and _BUILD >= 36)
print(f"237 turbos track their share {ok(_ok237)}  'WDC' in the WDC turbo's own symbol moved to what it tracks; a share's symbol left alone; "
      f"Western Digital and TSMC known; an unknown name found by search, a US listing first, and stored; the editor's 'Tracks'; the model beats a manual price")

# 238 a price in one currency, the accounts in another: ASML's dollar price
#     against euro accounts, TSMC's American share of five Taiwanese ones.
#     The SEC's own currency wins over a dollar translation; Yahoo's figures
#     are converted before a ratio is built; each week of a valuation band
#     at its own rate; Alibaba's 2019 share subdivision rebased; a year the
#     SEC tagged late still counts from its own year; a just-reported
#     quarter shown from the earnings summary; a past date is not the next
#     report; the street panel redraws when the price arrives.
from pf import research as _rs238, sec as _sec238
_sv238 = {k: getattr(_rs238, k) for k in ("usd_hist", "overview", "daily", "fundamentals", "street", "summary")}
_r238 = {}
try:
    _hist = {"EUR": {"d": ["2020-01-01", "2026-01-01"], "r": [1.10, 1.20]}, "GBP": {"d": ["2020-01-01"], "r": [1.25]}}
    _rs238.usd_hist = lambda c: _hist.get(c) if c != "USD" else None
    _fx = _rs238.FX("EUR", "USD")
    _r238["fx"] = (_fx.total("2025-06-01"), _fx.total("2026-02-01"), round(_rs238.FX("USD", "GBp").per_share(), 6),
                   _rs238.FX("USD", "USD").total())
    _f20 = [{"form": "20-F", "val": 1.0}] * 26
    _r238["unit"] = (_sec238.pick_unit({"USD": [{"form": "20-F", "val": 1.0}] * 9, "TWD": _f20}),
                     _sec238.pick_unit({"TWD": _f20[:9], "USD": _f20[:9]}), _sec238.unit_currency("TWD/shares"))
    _ovr = lambda mc, px: {"currency": "USD", "stats": {"market_cap": mc}, "quote": {"price": px}}
    _r238["ratio"] = (_rs238.units_ratio(25.9e9, _ovr(2.14e12, 412.61)), _rs238.units_ratio(1.44e6, _ovr(1.03e12, 480.0)),
                      _rs238.units_ratio(384.9e6, _ovr(632.67e9, 1641.52)))
    _ov1 = _rs238.one_currency({"currency": "USD", "fin_currency": "EUR", "stats": {
        "market_cap": 600e9, "cash": 10e9, "debt": 2e9, "revenue": 40e9, "ebitda": 16e9, "fcf": 10e9, "ocf": 20e9,
        "pb": 1408.9, "ev_ebitda": 2158.5, "ps": 15.0, "ev": 3.49e13, "pe_ttm": 40.4}})
    _r238["ov"] = (round(_ov1["stats"]["ev"] / 1e9, 1), round(_ov1["stats"]["ev_ebitda"], 2), _ov1["stats"]["pb"],
                   _ov1["stats"]["pe_ttm"], round(_ov1["stats"]["ps"], 3))
    _b = _rs238._fix_basis([{"shares": 2.5e9, "eps": 17.5}, {"shares": 20e9, "eps": 2.4}, {"shares": 20.4e9, "eps": 2.6}])
    _r238["basis"] = (_b[0]["shares"], _b[0]["eps"], _b[2]["shares"])
    _r238["late"] = [p["known"] for p in _rs238._annual_points([{"end": "2016-12-31", "filed": "2019-02-06", "revenue": 1.0, "eps": 1.0},
                                                                 {"end": "2024-12-31", "filed": "2025-03-05", "revenue": 1.0, "eps": 1.0}])]

    # a valuation: a dollar price, euro accounts, one quoted share = five of the filer's
    def _fund(eps_q, sh):
        return {"currency": "EUR", "cik": None, "annual": [], "quarters": [
            {"end": e, "filed": f, "eps": eps_q, "shares": sh, "revenue": 10e9, "ocf": 3e9, "capex": 1e9, "fcf": 2e9,
             "op_income": 4e9, "cash": 0, "debt": 0} for e, f in (("2025-03-31", "2025-04-20"), ("2025-06-30", "2025-07-20"),
                                                                 ("2025-09-30", "2025-10-20"), ("2025-12-31", "2026-01-20"))]}
    _bars = [["2026-02-20", 95, 95, 95, 95, 0, 95], ["2026-03-02", 96, 96, 96, 96, 0, 96]]
    _rs238.daily = lambda s: {"bars": _bars, "divs": [], "splits": []}
    _rs238.street = lambda s: {"periods": [{"period": "0y", "eps": {"avg": 5.0, "growth": 10.0}},
                                            {"period": "+1y", "eps": {"avg": 6.0, "growth": 20.0}}], "currency": {"eps": "EUR"}}
    _rs238.overview = lambda s: {"currency": "USD", "fin_currency": "EUR", "quote": {"price": 100.0}, "stats": {"market_cap": 100e9}}
    _rs238.fundamentals = lambda s: _fund(1.0, 1e9)
    _v1 = _rs238._load_valuation("TEST")
    _rs238.fundamentals = lambda s: _fund(0.2, 5e9)
    _v5 = _rs238._load_valuation("TEST")
    _r238["val"] = (_v1["series"]["pe"][-1], _v1["current"]["pe_cur_fy"], _v1["current"]["pe_next_fy"], _v1["ratio"],
                    _v5["series"]["pe"][-1], _v5["ratio"], round(_v5["current"]["market_cap"] / 1e9, 1),
                    round(_v5["series"]["ps"][-1], 3))
    # the quarter in the earnings release, before the statements
    _rs238.summary = lambda s: {"fin_ccy": "EUR", "hist_ccy": "EUR",
                                "chart": [{"cal": "3Q2026", "end": "2026-09-30", "reported": "2026-10-14", "eps": 11.82}],
                                "fin": [{"cal": "3Q2026", "revenue": 11.66e9, "earnings": 4.96e9}]}
    _pv = _rs238._provisional("TEST", [{"end": "2026-06-30", "shares": 384.9e6}], "EUR", "1231",
                              {"currency": "USD", "stats": {"market_cap": 632.67e9}, "quote": {"price": 1641.52}})
    _r238["prov"] = [(p["end"], p["eps"], p["revenue"], p["filed"], p["provisional"]) for p in _pv]
    # the next report is not one that has been
    _rs238.overview = lambda s: {"quote": {"price": None}, "dates": {"earnings": "2020-01-01", "earnings_estimated": False}}
    _rs238.street = lambda s: {"upcoming": {"date": "2099-01-27"}, "periods": [], "surprises": []}
    _r238["next"] = (_rs238._load_catalysts("TEST")["earnings"] or {}).get("date")
finally:
    for _k, _v in _sv238.items():
        setattr(_rs238, _k, _v)
_tj238 = open(os.path.join(HERE, "backend", "static", "ticker.js"), encoding="utf-8").read()
_ok238 = (_r238.get("fx") == (1.10, 1.20, 80.0, 1.0)
          and _r238.get("unit") == ("TWD", "USD", "TWD")
          and _r238.get("ratio") == (5.0, 1 / 1500, 1.0)
          and _r238.get("ov") == (590.4, 30.75, None, 40.4, 12.5)
          and _r238.get("basis") == (20e9, 2.1875, 20.4e9)
          and _r238.get("late") == ["2017-03-16", "2025-03-05"]
          and _r238.get("val") == (20.0, 16.67, 13.89, 1.0, 20.0, 5.0, 100.0, 2.0)
          and _r238.get("prov") == [("2026-09-30", 11.82, 11.66e9, "2026-10-14", True)]
          and _r238.get("next") == "2099-01-27"
          and "if (S.street) renderStreet();" in _tj238 and "ccyTag(SC.rev)" in _tj238 and 'r.provisional ? "ᵖ"' in _tj238
          and "/ (c.unit || 1)" in _tj238 and _BUILD >= 37)
print(f"238 one currency, one share  {ok(_ok238)}  EUR at each date's rate, pence {(_r238.get('fx') or ['?'] * 3)[2]} to a dollar; TWD over a dollar translation; "
      f"ADR ratios {_r238.get('ratio')}; EV/EBITDA {(_r238.get('ov') or ['?', '?'])[1]} not 2,158; P/E {(_r238.get('val') or ['?'])[0]} at five shares to one; "
      f"Q3 from the release; next report {_r238.get('next')}")

# 239 the consensus, kept day by day: the nightly scan's estimates for about
#     a thousand companies and every stock page's, one row a company a day,
#     dated by the day it was READ (a stale cache never passes for today),
#     stored only when something other than the price moved; a later read the
#     same day fills in; a company's other listing fills the days, never with
#     targets in another currency; the record starts from what the scan holds.
from pf import consensus as _cs239, watch as _wl239
import time as _t239
_tmp239 = os.path.join(_tf181.gettempdir(), "pf_cons37check.db")
for _x in (_tmp239, _tmp239 + "-wal", _tmp239 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_orig239 = store.DB_PATH
store.DB_PATH = _tmp239
_r239 = {}
try:
    store.init_db()
    _qs239 = {"quoteSummary": {"result": [{
        "financialData": {"targetMeanPrice": 2155.3, "targetMedianPrice": 2309.3, "numberOfAnalystOpinions": 17,
                          "currentPrice": 1641.5, "recommendationMean": 1.4, "financialCurrency": "EUR"},
        "price": {"currency": "USD"},
        "earningsTrend": {"trend": [
            {"period": "0q", "endDate": "2026-12-31", "earningsEstimate": {"avg": 15.39, "numberOfAnalysts": 7}, "revenueEstimate": {"avg": 14.8e9}},
            {"period": "+1q", "endDate": "2027-03-31", "earningsEstimate": {"avg": 12.94}},
            {"period": "0y", "endDate": "2026-12-31", "earningsEstimate": {"avg": 41.21, "numberOfAnalysts": 25, "earningsCurrency": "EUR"},
             "revenueEstimate": {"avg": 44.47e9, "revenueCurrency": "EUR"}, "epsTrend": {"current": 41.21}},
            {"period": "+1y", "endDate": "2027-12-31", "earningsEstimate": {"avg": 55.93}}]}}]}}
    _e239 = _wl239.parse_summary(_qs239)
    _now239 = _t239.time()
    _e239["fetched_at"] = _now239 - 3 * 86400            # read three days ago, still in the cache
    _r239["parsed"] = (_e239.get("q0q", {}).get("eps"), _e239.get("end0y"), (_e239.get("ccy0y") or {}).get("eps"), "trend0q" not in _e239)
    _n1 = _cs239.note_scan({"ASML.AS": ({}, {**_e239, "info": {**_e239["info"], "currency": "EUR"}}),
                            "ASML": ({}, _e239), "NOEST": ({}, {})})
    _r239["dated"] = [(r["d"], r["eps_0q"], r["n_0y"], r["tgt_median"], r["eps_ccy"]) for r in _cs239.history("ASML")["rows"]]
    _d3 = _cs239.day_of(_now239 - 3 * 86400)
    # the same read again tomorrow, the price moved, nothing else: no row
    _cs239.record("ASML", {**_cs239.from_est(_e239), "price": 1700.0}, "page", ts=_now239 - 2 * 86400)
    # the next day the year's forecast is raised: a row; a second read that day fills the median in
    _cs239.record("ASML", {"eps_0y": 42.0, "tgt_mean": 2200.0, "px_ccy": "USD"}, "page", ts=_now239 - 86400)
    _cs239.record("ASML", {"tgt_median": 2350.0}, "page", ts=_now239 - 86400 + 60)
    _h239 = _cs239.history("ASML")["rows"]
    _r239["kept"] = [(r["d"] == _d3, r["eps_0y"], r["tgt_median"]) for r in _h239]
    # the Amsterdam line fills a day New York was not read, but not with euro targets
    _cs239.record("ASML.AS", {"eps_0y": 43.0, "tgt_mean": 1900.0, "px_ccy": "EUR"}, "scan", ts=_now239)
    _h2 = _cs239.history("ASML", ["ASML.AS"], "USD")["rows"]
    _r239["sibling"] = (_h2[-1]["sym"], _h2[-1]["eps_0y"], _h2[-1]["tgt_mean"], len(_h2))
    _r239["n1"] = _n1
    # the backfill: once, from the cached estimates
    store.cache_put("wl:universe", {"rows": [{"sym": "MSFT", "also": []}]})
    store.cache_put("wl:est:MSFT", {**_e239, "fetched_at": _now239 - 5 * 86400})
    _r239["backfill"] = (_cs239.backfill_once(), _cs239.backfill_once())
finally:
    store.DB_PATH = _orig239
    for _x in (_tmp239, _tmp239 + "-wal", _tmp239 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
_api239 = open(os.path.join(HERE, "backend", "pf", "api.py"), encoding="utf-8").read()
_wls239 = open(os.path.join(HERE, "backend", "pf", "watch.py"), encoding="utf-8").read()
_rss239 = open(os.path.join(HERE, "backend", "pf", "research.py"), encoding="utf-8").read()
_ok239 = (_r239.get("parsed") == (15.39, "2026-12-31", "EUR", True)
          and _r239.get("n1") == 2
          and _r239.get("dated") == [(_d3, 15.39, 25.0, 2309.3, "EUR")]
          and _r239.get("kept") == [(True, 41.21, 2309.3), (False, 42.0, 2350.0)]
          and _r239.get("sibling") == ("ASML.AS", 43.0, None, 3)
          and _r239.get("backfill") == (1, None)
          and 0 <= _api239.find('@router.get("/research/{sym}/consensus"') < _api239.find('@router.get("/research/{sym}/{part}"')
          and "consensus.note_scan(deep)" in _wls239 and 'consensus.record(sym, consensus.from_street(out' in _rss239)
print(f"239 the consensus record     {ok(_ok239)}  read three days ago, stored under that day; a price-only day not kept; a raised forecast kept, "
      f"filled in by a later read; Amsterdam fills a New York day without its euro targets; the backfill once")

# 240 the street panel: periods by their fiscal names (Microsoft's September
#     quarter is Q1 FY27); what followed each report, five and twenty
#     sessions on, and for the latest how the forecasts moved since, against
#     the Yahoo mark just before it; one row per firm, greyed after six
#     months; the consensus target rebuilt from the actions, each firm's
#     latest for up to a year; Zacks' later years, as growth only for a
#     European line.
from pf import research as _rs240
from datetime import date as _d240
_r240 = {}
_svz240 = (_rs240.net.get, _rs240.filer_cik, _rs240.store.cache_through, _rs240.overview)
try:
    _per = [{"period": "0q", "end": "2026-09-30", "trend": {"current": 4.72, "7daysAgo": 4.70, "30daysAgo": 4.60, "90daysAgo": 4.50}},
            {"period": "0y", "end": "2027-06-30", "trend": {"current": 19.8, "7daysAgo": 19.7, "30daysAgo": 19.0, "90daysAgo": 19.4}},
            {"period": "+1y", "end": "2028-06-30", "trend": {"current": 23.7, "30daysAgo": 22.0}}]
    _rs240._fiscal_names(_per)
    _r240["names"] = [p["fiscal"] for p in _per]
    # 30 sessions; the report on day 5 (after the close: the 6th session moves)
    _days = [str(_d240.fromordinal(_d240(2026, 9, 1).toordinal() + i)) for i in range(30)]
    _bars = {d: [d, 0, 0, 0, 100.0 + i, 0, 100.0 + i] for i, d in enumerate(_days)}
    _hist = [{"date": _days[4], "surprise_pct": 5.0, "eps_act": 1.05, "eps_est": 1.0, "reaction": {"day": _days[5], "move": 1.0}}]
    _af = _rs240._after_reports(_hist, _bars, _days, _per, today=_d240.fromisoformat(_days[4]) + _rs240.timedelta(days=12))
    _x = _af[0]
    _r240["after"] = (round(_x["d5"], 3), round(_x["d20"], 3), _x["sessions"], _x["est"]["mark"],
                      [(e["fiscal"], round(e["chg"], 2)) for e in _x["est"]["periods"]])
    # brokers: the latest per firm, a target carried from an earlier action, stale after 180 days
    _today = _d240(2026, 10, 18)
    _acts = [{"firm": "A", "date": "2026-10-15", "ts": 3, "to": "Buy", "action": "main", "pt": 600.0, "pt_prior": 525.0},
             {"firm": "B", "date": "2026-09-01", "ts": 2, "to": "Hold", "action": "down", "pt": None},
             {"firm": "B", "date": "2026-06-01", "ts": 1, "to": "Buy", "action": "main", "pt": 500.0},
             {"firm": "C", "date": "2026-02-01", "ts": 0, "to": "Buy", "action": "init", "pt": 400.0},
             {"firm": "D", "date": "2025-09-01", "ts": -1, "to": "Buy", "action": "main", "pt": 300.0}]
    _bd = {"2025-09-01": ["2025-09-01", 0, 0, 0, 90.0, 0, 90.0], "2026-10-16": ["2026-10-16", 0, 0, 0, 110.0, 0, 110.0]}
    _br, _path = _rs240._brokers(_acts, _bd, sorted(_bd), today=_today)
    _r240["brokers"] = [(b["firm"], b["pt"], b["stale"]) for b in _br]
    _last = _path[-1] if _path else {}
    _r240["path"] = (_last.get("n"), _last.get("mean"), _last.get("price"), _path[0]["d"] if _path else None)
    # Zacks: years with growth; a European line gets the US line's, flagged
    _rs240.net.get = lambda url, h=None, timeout=15: b'{"data":{"yearlyForecast":{"rows":[{"fiscalEnd":"Jun 2027","consensusEPSForecast":19.65,"noOfEstimates":20},{"fiscalEnd":"Jun 2028","consensusEPSForecast":23.24,"noOfEstimates":17}]},"quarterlyForecast":{"rows":[]}}}'
    _rs240.store.cache_through = lambda k, ttl, fn, **kw: fn()
    _rs240.filer_cik = lambda s, ov=None: {"cik": 1, "via": "ASML"}
    _rs240.overview = lambda s: {}
    _z1 = _rs240.zacks("MSFT")
    _z2 = _rs240.zacks("ASML.AS")
    _r240["zacks"] = (_z1["years"][1]["end"], round(_z1["years"][1]["growth"], 2), _z1["same_listing"], _z2["symbol"], _z2["same_listing"],
                      _rs240._month_end("Feb 2028"))
finally:
    _rs240.net.get, _rs240.filer_cik, _rs240.store.cache_through, _rs240.overview = _svz240
_tj240 = open(os.path.join(HERE, "backend", "static", "ticker.js"), encoding="utf-8").read()
_ok240 = (_r240.get("names") == ["Q1 FY27", "FY27", "FY28"]
          and _r240.get("after") == (round((109 / 104 - 1) * 100, 2), round((124 / 104 - 1) * 100, 2), 25, "30daysAgo",
                                     [("Q1 FY27", 2.61), ("FY27", 4.21), ("FY28", 7.73)])
          and _r240.get("brokers") == [("A", 600.0, False), ("B", 500.0, False), ("C", 400.0, True), ("D", 300.0, True)]
          and _r240.get("path") == (3, 500.0, 110.0, "2026-06-06")
          and _r240.get("zacks") == ("2028-06-30", 18.27, True, "ASML", False, "2028-02-29")
          and '"consensus"];' in _tj240 and "recordFig()" in _tj240 and "s.target_path" in _tj240 and "revenueSurprises(af)" in _tj240
          and "Analysts, one row per firm" in _tj240)
print(f"240 the street panel         {ok(_ok240)}  {', '.join(_r240.get('names') or [])}; five sessions {(_r240.get('after') or ['?'])[0]}%, "
      f"forecasts since the report from the {(_r240.get('after') or ['?'] * 4)[3]} mark; firm B's target carried from June; "
      f"the path's mean {(_r240.get('path') or ['?', '?'])[1]} from three standing targets (D's expired); Zacks for ASML.AS as growth only")

# 241 the financials: the forecast as columns, in the table's own currency and
#     share count; the EPS analysts count beside the reported one, a gap of
#     more than a tenth flagged; debt against a year's EBITDA, interest
#     cover, inventory and receivable days, FCF after stock-based pay, q/q
#     growth; warnings only where the amount matters; and every quarter
#     Yahoo has shown kept, so a company outside the SEC's quarters builds a
#     history instead of losing its oldest quarter each time.
from pf import research as _rs241
_sv241 = (_rs241.usd_hist, store.DB_PATH)
_tmp241 = os.path.join(_tf181.gettempdir(), "pf_fin37check.db")
for _x in (_tmp241, _tmp241 + "-wal", _tmp241 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_r241 = {}
try:
    _rs241.usd_hist = lambda c: {"d": ["2020-01-01"], "r": [0.032]} if c == "TWD" else None    # 31.25 TWD to the dollar
    _q = lambda e, **kw: {"end": e, "label": e, "revenue": 100.0, "cost_of_revenue": 50.0, "op_income": 40.0, "dna": 10.0,
                          "interest": 2.0, "net_income": 30.0, "ocf": 20.0, "fcf": 15.0, "sbc": 5.0, "cash": 300.0, "debt": 100.0,
                          "inventory": 40.0, "receivables": 30.0, "shares": 25.0, "eps": 1.2, **kw}
    _qs = [_q("2025-09-30", revenue_yoy=10.0), _q("2025-12-31", revenue_yoy=12.0), _q("2026-03-31", revenue_yoy=15.0),
           _q("2026-06-30", revenue=110.0, revenue_yoy=20.0, eps=2.0)]
    _rs241._ratios(_qs, "q")
    _l = _qs[-1]
    _r241["ratios"] = (_l["nd_ebitda"], _l["int_cover"], _l["inv_days"], _l["rec_days"], _l["fcf_sbc"], _l["revenue_qoq"], _l["rev_accel"])
    # the analysts' EPS: dollars per American share of five, in a Taiwan-dollar table per share
    _ov = {"currency": "USD", "stats": {"market_cap": 1000.0}, "quote": {"price": 200.0}}     # 5 quoted units: 25 filer shares → 5 to 1
    _st = {"currency": {"hist": "USD", "eps": "USD", "rev": "TWD"},
           "surprises": [{"date": "2026-07-16", "eps_act": 0.2}, {"date": "2026-04-16", "eps_act": 0.192}],
           "periods": [{"period": "0q", "end": "2026-06-30", "fiscal": "Q2 FY26", "eps": {"avg": 0.2}, "rev": {"avg": 110.0}},
                       {"period": "+1q", "end": "2026-09-30", "fiscal": "Q3 FY26", "eps": {"avg": 0.24, "growth": 20.0, "n": 9},
                        "rev": {"avg": 120.0, "growth": 18.0}},
                       {"period": "+1y", "end": "2027-12-31", "fiscal": "FY27", "eps": {"avg": 1.0}, "rev": {"avg": 500.0}}],
           "zacks": {"same_listing": True, "years": [{"end": "2027-12-31", "eps": 1.0}, {"end": "2028-12-31", "eps": 1.2, "growth": 20.0, "n": 3}]}}
    _rs241._street_eps(_qs, [], _st, "TWD", _ov)
    _r241["street"] = (_qs[-1]["eps_street"], _qs[-1]["eps_gap"], _qs[-2]["eps_street"], _qs[0].get("eps_street"))
    _est = _rs241._estimate_columns(_st, "TWD", _qs, [{"end": "2025-12-31"}], _ov)
    _r241["est"] = ([(e["label"], e["eps_street"], e["revenue"]) for e in _est["q"]],
                    [(e["label"], e["eps_street"], e.get("zacks", False)) for e in _est["a"]])
    # warnings: profit not turning into cash; receivables only when they matter
    _fl = _rs241._quality_flags(_qs, [])
    _r241["flags"] = sorted(f["k"] for f in _fl)
    # inventory half again on a year: nothing at 27 days of cost of sales, a warning at 55
    _e8 = ("2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31")
    _mat = []
    for _lo, _hi in ((10.0, 15.0), (20.0, 30.0)):
        _q8 = [_q(e, inventory=_lo) for e in _e8] + [_q("2026-06-30", inventory=_hi)]
        _rs241._ratios(_q8, "q")
        _mat.append("inventory" in [f["k"] for f in _rs241._quality_flags(_q8, [])])
    _r241["material"] = tuple(_mat)
    # every quarter kept
    store.DB_PATH = _tmp241
    store.init_db()
    _rs241._kept_quarters("TEST", [dict(x) for x in _qs[:3]])
    _back, _n = _rs241._kept_quarters("TEST", [dict(_qs[3]), {"end": "2026-09-30", "label": "p", "provisional": True}])
    _r241["kept"] = (_n, [b["end"] for b in _back], [bool(b.get("kept")) for b in _back])
    # a quarter later: the four real ones kept, never the provisional one
    _back2, _n2 = _rs241._kept_quarters("TEST", [{"end": "2026-12-31", "label": "Q4"}])
    _r241["kept2"] = (_n2, any(b.get("provisional") for b in _back2))
finally:
    _rs241.usd_hist, store.DB_PATH = _sv241
    for _x in (_tmp241, _tmp241 + "-wal", _tmp241 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
_tj241 = open(os.path.join(HERE, "backend", "static", "ticker.js"), encoding="utf-8").read()
_ok241 = (_r241.get("ratios") == (round((100 - 300) / 200, 2), 20.0, 73, 27, 10.0, 10.0, 5.0)
          and _r241.get("street") == (1.25, 60.0, 1.2, None)
          and _r241.get("est") == ([("Q3 FY26E", 1.5, 120.0)], [("FY27E", 6.25, False), ("FY28E", 7.5, True)])
          and _r241.get("flags") == ["accruals", "gaap", "sbc"] and _r241.get("material") == (False, True)
          and _r241.get("kept") == (3, ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"], [True, True, True, False, False])
          and _r241.get("kept2") == (4, False)
          and '"eps_street", "eps"]' in _tj241 and "function finDetail(" in _tj241 and 'cell(e, k, t, "est")' in _tj241)
print(f"241 the financials           {ok(_ok241)}  net debt/EBITDA {(_r241.get('ratios') or ['?'])[0]}, interest {(_r241.get('ratios') or ['?'] * 2)[1]}×; "
      f"0.20 US$ per American share = {(_r241.get('street') or ['?'])[0]} TWD a share, reported 2.00 flagged {(_r241.get('street') or ['?'] * 2)[1]}%; "
      f"the forecast after the last actual, Zacks after Yahoo; warnings {_r241.get('flags')}; three quarters kept")

# 242 the home screen's earnings week: a lookup that failed because nothing
#     answered (the laptop woke without a network on 19 October and all fifty
#     failed on DNS) is asked again in a quarter of an hour, not kept two days
#     as "no date on Yahoo"; Nasdaq's day calendar, which ERN already reads,
#     places a holding Yahoo did not; a week where most lookups could not be
#     made says so instead of passing for a quiet one.
from pf import calendar as _cal242
from datetime import date as _d242
_r242 = {}
_sv242 = (_cal242._earnings_dates, _cal242._nasdaq_rows, store.DB_PATH, store.cache_age)
_tmp242 = os.path.join(_tf181.gettempdir(), "pf_earn38check.db")
for _x in (_tmp242, _tmp242 + "-wal", _tmp242 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
try:
    _dns = {"ok": False, "tried": ["earnings_dates: DNSError", "calendar: DNSError", "info: DNSError"]}
    _nod = {"ok": False, "tried": ["earnings_dates: empty", "calendar: empty", "info: no earningsTimestamp"]}
    _r242["kind"] = (_cal242.earn_transient(_dns), _cal242.earn_transient(_nod), _cal242.earn_transient({"ok": True}),
                     _cal242.earn_ttl(_dns), _cal242.earn_ttl(_nod), _cal242.earn_ttl({"ok": True, "dates": []}))
    store.DB_PATH = _tmp242
    store.init_db()
    # cached an hour ago: the DNS failure no longer stands, the dates and the no-date answer do
    store.cache_put("earn:AAA", _dns)
    store.cache_put("earn:BBB", {"ok": True, "src": "earnings_dates", "dates": [{"date": "2026-10-21", "time": "16:00"}]})
    store.cache_put("earn:CCC", _nod)
    store.cache_age = lambda k: 3600.0
    _r242["stands"] = tuple(_cal242.cached_dates(t)[0] is not None for t in ("AAA", "BBB", "CCC"))
    # the network down: Yahoo answers nothing, Nasdaq has AAA on Tuesday
    _cal242._earnings_dates = lambda tk: {**_dns, "transient": True}
    _cal242._nasdaq_rows = lambda d: [{"sym": "AAA", "held": "AAA", "when": "amc", "eps_est": 0.82}] if d.isoformat() == "2026-10-20" else []
    _w = _cal242.earnings_week(["AAA", "BBB", "CCC", "DDD"], today=_d242(2026, 10, 20))
    _ev = [(x["ticker"], x["date"], x.get("time"), x.get("src")) for d in _w["days"] for x in d["events"]]
    _r242["week"] = (sorted(_ev), _w["from_nasdaq"], _w["unreachable"], bool(_w["degraded"]), _w["yahoo"]["ok"], _w["error"])
finally:
    _cal242._earnings_dates, _cal242._nasdaq_rows, store.DB_PATH, store.cache_age = _sv242
    for _x in (_tmp242, _tmp242 + "-wal", _tmp242 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
_ix242 = open(os.path.join(HERE, "backend", "static", "index.html"), encoding="utf-8").read()
_en242 = open(os.path.join(HERE, "backend", "pf", "earnings.py"), encoding="utf-8").read()
_ok242 = (_r242.get("kind") == (True, False, False, 15 * 60, 12 * 3600, 48 * 3600)
          and _r242.get("stands") == (False, True, True)
          and _r242.get("week") == ([("AAA", "2026-10-20", "after close", "nasdaq"), ("BBB", "2026-10-21", "16:00", "earnings_dates")],
                                    1, 2, True, False, None)
          and '${E.degraded ? `<div class="note"' in _ix242 and "const nodate = " in _ix242
          and "cal.cached_dates(sym)" in _en242 and _BUILD >= 38)
print(f"242 the earnings week        {ok(_ok242)}  a DNS failure stands {_cal242.EARN_DOWN_TTL // 60} minutes, no date on Yahoo "
      f"{_cal242.EARN_NODATE_TTL // 3600} hours; with the network down, AAA from Nasdaq's calendar and the week marked unknown, not quiet")

# 243 FCF, the free-cash-flow model (build 39): Qualtrim's arithmetic to the
#     cent (Meta, $16.08 a share, 30% for five years at 40x from $748.51:
#     26.12% a year, $1,187.34 for 15%); growth starting from the analysts and
#     the company's own operating cash flow per share and fading to 5%; the
#     multiple from its own price/FCF band, kept between 8x and 35x; the
#     return its risk calls for, the bond in its currency plus its own beta
#     (adjusted toward 1) times 5%; what you type saved per stock, a blank
#     the company's again; a negative FCF says what the normalised base would
#     give; a bank is not modelled.
from pf import fcfmodel as _fm243, research as _rs243
import math as _m243
_r243 = {}
_q243 = _fm243.project(16.08, 30.0, 40.0, 15.0, 5, 748.51)
_r243["qualtrim"] = (_q243["implied"], _q243["entry"], round(_q243["path"][1]), round(_q243["value"]))
_r243["fade"] = (_fm243.faded(30.0, 5), _fm243.faded(5.0, 5), _fm243.faded(10.0, 3))
_c243 = _fm243.cases(15.0, 30.0, 10.0, 90.0, 30.0)                 # a band far wider than 0.7-1.4 of its median
_r243["cases"] = (_c243["bear"]["growth"], _c243["bull"]["growth"], _c243["bear"]["multiple"], _c243["bull"]["multiple"])
_sv243 = {k: getattr(_rs243, k) for k in ("overview", "fundamentals", "valuation", "street", "daily", "_closes", "benchmarks_for")}
_svf243 = (_fm243.rf_for, _fm243.inputs, store.DB_PATH)
_tmp243 = os.path.join(_tf181.gettempdir(), "pf_fcf39check.db")
for _x in (_tmp243, _tmp243 + "-wal", _tmp243 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
try:
    store.DB_PATH = _tmp243
    store.init_db()
    # a market that moves, and a stock that moves half as much again
    import random as _rnd243
    _rnd243.seed(7)
    _mk, _sk, _days = [100.0], [100.0], []
    from datetime import date as _dd243, timedelta as _td243
    for _i in range(800):
        _days.append(str(_dd243(2023, 1, 2) + _td243(days=_i)))
        if _i:
            _r = _rnd243.gauss(0, 0.01)
            _mk.append(_mk[-1] * (1 + _r))
            _sk.append(_sk[-1] * (1 + 1.5 * _r))
    _rs243.daily = lambda s: {"bars": [[d, 0, 0, 0, c, 0, c] for d, c in zip(_days, _sk)], "divs": [], "splits": []}
    _rs243._closes = lambda s: dict(zip(_days, _mk))
    _rs243.benchmarks_for = lambda s, ov: {"market": {"sym": "^GSPC", "label": "S&P 500"}}
    _fm243.rf_for = lambda ccy: (5.0, "the test yield")
    _ov = {"currency": "USD", "fin_currency": "USD", "quote_type": "EQUITY", "sector": "Technology", "industry": "Software",
           "short": "Test", "quote": {"price": 100.0}, "stats": {"market_cap": 1e10, "beta": 1.2}}
    _qs = [{"end": e, "label": e, "ocf": 30.0, "capex": 10.0, "sbc": 5.0, "dna": 4.0, "shares": 100.0}
           for e in ("2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30")]
    _an = [{"end": f"{y}-12-31", "label": f"FY{str(y)[2:]}", "ocf": 60.0 * 1.1 ** (y - 2020), "capex": 20.0, "shares": 100.0, "sbc": 10.0}
           for y in range(2020, 2026)]
    _rs243.overview = lambda s: _ov
    _rs243.fundamentals = lambda s: {"currency": "USD", "quarters": _qs, "annual": _an}
    _rs243.valuation = lambda s: {"ratio": 1.0, "stats": {"pfcf": {"5y": {"median": 50.0, "p25": 40.0, "p75": 60.0}}}}
    _rs243.street = lambda s: {"periods": [{"period": "0y", "rev": {"growth": 20.0}}, {"period": "+1y", "rev": {"growth": 10.0}}]}
    _in = _fm243._load_inputs("TEST")
    _r243["inputs"] = (_in["fps"]["reported"], _in["fps"]["after_sbc"], _in["fps"]["normalised"], _in["growth"]["start"],
                       _in["growth"]["by_horizon"][5], _in["multiple"]["base"], round(_in["ret"]["beta"]["raw"], 1),
                       abs(_in["ret"]["base"] - (5.0 + _in["ret"]["beta"]["adj"] * 5.0)) < 0.011
                       and abs(_in["ret"]["beta"]["adj"] - (0.67 * _in["ret"]["beta"]["raw"] + 0.33)) < 0.011, _in["heavy"])
    # the model with the company's figures, then with yours, then a blank again
    _m1 = _fm243.model("TEST", _in, {})
    _fm243.save("TEST", {"growth": 30.0, "multiple": 40.0, "ret": 15.0, "listed": True})
    _m2 = _fm243.model("TEST", _in)
    _fm243.save("TEST", {"growth": None})
    _m3 = _fm243.model("TEST", _in)
    try:
        _fm243.save("TEST", {"horizon": 4})
        _bad = False
    except ValueError:
        _bad = True
    _r243["model"] = (_m1["base_kind"], _m1["growth_used"], _m1["multiple_used"], _m2["cases"]["base"]["implied"],
                      _m2["cases"]["base"]["entry"], _m3["growth_used"], _m3["multiple_used"], _m3["edited"], _bad,
                      _fm243.listed())
    # negative free cash flow: the normalised base says what it would give
    _rs243.fundamentals = lambda s: {"currency": "USD", "annual": _an,
                                     "quarters": [{**q, "capex": 40.0, "dna": 5.0} for q in _qs]}
    _store_inp = _fm243._load_inputs("NEG")
    _fm243.inputs = lambda s: _store_inp
    _row = _fm243._row({"sym": "NEG", "why": "held"})
    _r243["neg"] = (_row["ok"], "normalised base" in (_row["why_not"] or ""))
    _rs243.overview = lambda s: {**_ov, "sector": "Financial Services", "industry": "Banks - Diversified"}
    _r243["bank"] = _fm243._load_inputs("BANK")["available"]
    _rs243.overview = lambda s: {**_ov, "sector": "Financial Services", "industry": "Credit Services"}
    _r243["visa"] = _fm243._load_inputs("CARD")["available"]
finally:
    for _k, _v in _sv243.items():
        setattr(_rs243, _k, _v)
    _fm243.rf_for, _fm243.inputs, store.DB_PATH = _svf243
    for _x in (_tmp243, _tmp243 + "-wal", _tmp243 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
_kit243 = open(os.path.join(HERE, "backend", "static", "fcfkit.js"), encoding="utf-8").read()
_tk243 = open(os.path.join(HERE, "backend", "static", "ticker.js"), encoding="utf-8").read()
_tj243 = open(os.path.join(HERE, "backend", "static", "term.js"), encoding="utf-8").read()
_app243 = open(os.path.join(HERE, "backend", "portfolio_app.py"), encoding="utf-8").read()
_api243 = open(os.path.join(HERE, "backend", "pf", "api.py"), encoding="utf-8").read()
_ok243 = (_r243.get("qualtrim") == (26.12, 1187.34, 944, 2388)
          and _r243.get("fade") == (17.17, 5.0, 7.48)
          and _r243.get("cases") == (10.5, 19.5, 21.0, 42.0)
          and _r243.get("inputs") == (0.8, 0.6, 1.04, 12.5, _fm243.faded(12.5, 5), 35.0, 1.5, True, True)
          and (_r243.get("model") or [None])[:3] == ("reported", _fm243.faded(12.5, 5), 35.0)
          and _r243.get("model")[3:] == (round(((0.8 * 1.3 ** 5 * 40 / 100) ** 0.2 - 1) * 100, 2), round(0.8 * 1.3 ** 5 * 40 / 1.15 ** 5, 2),
                                         _fm243.faded(12.5, 5), 40.0, True, True, ["TEST"])
          and _r243.get("neg") == (False, True) and _r243.get("bank") is False and _r243.get("visa") is True
          and "function project(fps, g, m, r, n, price)" in _kit243 and "const G = [-5, 40], M = [8, 35];" in _kit243
          and '["s-fcf", "FCF"]' in _tk243 and 'code: "FCF"' in _tj243 and '"FCF": "fcf.html"' in _app243
          and '@router.get("/fcf/{sym}"' in _api243 and _BUILD >= 39)
print(f"243 the FCF model            {ok(_ok243)}  Qualtrim's Meta to the cent: {(_r243.get('qualtrim') or ['?'])[0]}% a year, entry {(_r243.get('qualtrim') or ['?'] * 2)[1]}; "
      f"30% fades to {(_r243.get('fade') or ['?'])[0]}% a year; beta 1.5 asks {5.0 + (0.67 * 1.5 + 0.33) * 5.0:.2f}%; yours saved, a blank the company's; banks left out")

# 244 IDX, the S&P 500 and the Nasdaq 100 from the inside (build 40): breadth
#     rebuilt from the members' closes (above the 200-day, new highs and lows
#     beyond the 251 days before, the advance-decline line); concentration
#     from weights moved back with each price (the top ten, the effective
#     number, the seven against the rest); a sector held from the start of
#     the period, not weighted by today's weights; the index's P/E as total
#     price over total earnings; the season remembering last year's EPS that
#     Nasdaq drops once a company has reported, and every report of the
#     coming week kept; history's falls and their recoveries; your book with
#     a tracker looked through and a turbo at its exposure, held names only
#     the ones you hold yourself; numbers in the lines rounded as the tiles
#     round them; the two years written once to idx_day, a row a night after.
from pf import idx as _ix244, earnings as _ern244, service as _svc244
import pandas as _pd244
from datetime import date as _d244, timedelta as _td244
_r244 = {}
# breadth: one member rising every day, one falling, one flat that jumps on the last day
_days244 = _pd244.bdate_range("2024-01-01", periods=600)
_a = [100 * 1.001 ** i for i in range(600)]
_b = [100 * 0.999 ** i for i in range(600)]
_c = [100.0] * 599 + [101.0]
_df244 = _pd244.DataFrame({"A": _a, "B": _b, "C": _c, "^IDX": [x + y + z for x, y, z in zip(_a, _b, _c)]}, index=_days244)
_br244 = _ix244.breadth_series(_df244, "^IDX")
_r244["breadth"] = (_br244["p200"][-1], _br244["nh"][-1], _br244["nl"][-1], _br244["ad"][-1], _br244["near"][-1], _br244["n"], len(_br244["d"]))
# concentration: twelve members weighted 12 to 1; NVDA, the largest, doubled over the two years
_syms244 = ["NVDA"] + [f"M{i}" for i in range(11)]
_w244 = {s: 12 - i for i, s in enumerate(_syms244)}
_px244 = {s: [100.0] * 504 for s in _syms244}
_px244["NVDA"] = [100.0 * 2 ** (i / 503) for i in range(504)]
_cf244 = _pd244.DataFrame(_px244, index=_pd244.bdate_range("2024-01-01", periods=504))
_cn244 = _ix244.concentration(_ix244.weights_path(_cf244, _w244))
_r244["conc"] = (_cn244["top10"][-1], _cn244["eff_n"][-1], _cn244["top10"][0], _cn244["mag7_syms"],
                 _cn244["mag7_line"][-1], _cn244["rest_line"][-1])
# a sector held from the start: 60 that rose half, 40 that stood still, is 25%, not 30%
_sec244 = _ix244.sector_stats([{"sector": "T", "w": 60.0, "r": {"ytd": 50.0}, "c": {"ytd": 25.0}},
                               {"sector": "T", "w": 40.0, "r": {"ytd": 0.0}, "c": {"ytd": 0.0}},
                               {"sector": "E", "w": 10.0, "r": {"ytd": -50.0}, "c": {"ytd": -5.0}}])
_r244["sector"] = [(x["sector"], x["w"], x["r"]["ytd"], x["c"]["ytd"]) for x in _sec244]
# contribution: each member's start weight times its return
_cd244 = _pd244.bdate_range("2025-06-02", "2026-03-02")
_ya = [100.0 if d.year == 2025 else 100.0 + 50.0 * ((i - sum(1 for x in _cd244 if x.year == 2025) + 1) / sum(1 for x in _cd244 if x.year == 2026))
       for i, d in enumerate(_cd244)]
_ms244, _ir244 = _ix244.member_stats(_pd244.DataFrame({"X": _ya, "Y": [100.0] * len(_cd244), "^IDX": [100.0] * len(_cd244)}, index=_cd244),
                                     {"X": 60.0, "Y": 40.0}, "^IDX")
_r244["contrib"] = tuple((m["sym"], m["r"]["ytd"], m["c"]["ytd"]) for m in _ms244)
# the index's multiple: half at 10x, half at 40x is 16x, not 25x
_r244["harm"] = (_ix244._harm([(50.0, 10.0), (50.0, 40.0)]), _ix244._harm([(50.0, 10.0), (50.0, -5.0)]))
# rounding as the tiles round
_r244["fx"] = (_ix244._fx(5.55), _ix244._fx(-5.55, 1, sign="+"), _ix244._fx(20.65), _ix244._fx(7819.4, 0, comma=True),
               _ix244._fx(-0.04), _ix244.ordinal(53), _ix244.ordinal(12), _ix244.ordinal(97.4), _ix244.dd_bucket(-7.0))
_ins244 = _ix244.insights({"name": "S&P 500", "ix": "spx", "level": {"dd": -5.55, "ath": 7819.4, "ath_d": "2026-10-06", "dist200": 6.2},
                           "breadth": {"p200": [57.0] * 30}})
_r244["insights"] = (_ins244[0]["text"], _ins244[0]["tone"], "(unchanged on a month)" in _ins244[1]["text"])
# history: a long rise, a fall of 30% over thirty days, and back
_hc244 = [100 * 1.001 ** i for i in range(700)]
_pk = _hc244[-1]
_hc244 += [_pk * (1 - 0.01 * k) for k in range(1, 31)]
_hc244 += [_pk * 0.7 * (1 / 0.7) ** (k / 100) for k in range(1, 101)]
_hc244 += [_hc244[-1] * 1.001 ** k for k in range(1, 301)]
_hd244 = [str(x.date()) for x in _pd244.bdate_range("1990-01-01", periods=len(_hc244))]
_hs244 = _ix244.history_study(_hd244, _hc244)
_f244 = _hs244["falls"][0]
_r244["history"] = (_hs244["available"], _hs244["bucket"], len(_hs244["falls"]), round(_f244["depth"]), _f244["down_days"],
                    _f244["recovered"] is not None, _hs244["all"]["21"]["n"] == len([i for i in range(199, len(_hc244) - 1, 21) if i + 21 < len(_hc244)]),
                    _ix244.history_study(_hd244[:300], _hc244[:300])["available"])
_sv244 = (store.DB_PATH, _ern244.nasdaq_day, _svc244.dashboard, store.load_instruments, _ix244.members, _ix244._book_beta)
_tmp244 = os.path.join(_tf181.gettempdir(), "pf_idx40check.db")
for _x in (_tmp244, _tmp244 + "-wal", _tmp244 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
try:
    store.DB_PATH = _tmp244
    store.init_db()
    # the season: last year's EPS seen before the report, gone from the day file after it
    _rows244 = [{"sym": "AAA", "w": 10.0}] + [{"sym": f"N{i}", "w": 1.0} for i in range(50)]
    _after = {"v": False}

    def _nq244(d):
        if d == _d244(2026, 10, 14):
            return [{"sym": "AAA", "fq": "Sep/2026", "eps_est": 1.0, "when": "bmo",
                     **({"eps_act": 1.2} if _after["v"] else {"eps_act": None, "ly_eps": 0.8})}]
        if d == _d244(2026, 10, 22):
            return [{"sym": f"N{i}", "fq": "Sep/2026", "eps_est": 1.0} for i in range(50)]
        return []
    _ern244.nasdaq_day = _nq244
    _s1 = _ix244.season(_rows244, today=_d244(2026, 10, 13))
    _after["v"] = True
    _s2 = _ix244.season(_rows244, today=_d244(2026, 10, 20))
    _r244["season"] = (len(_s1["next"]), _s1["reported"], _s2["reported"], _s2["beat"], _s2["growth_w"], _s2["growth_n"],
                       len(_s2["next"]), _s2["quarter_end"], _s2["since"])
    # your book: a share, a tracker looked through, a turbo at its exposure
    _ix244.members = lambda ix: {"rows": [{"sym": "AAPL", "w": 50.0}, {"sym": "NVDA", "w": 30.0}, {"sym": "MSFT", "w": 20.0}]}
    _ix244._book_beta = lambda ix: {}
    store.load_instruments = lambda: {"TURBO1": {"underlying_ticker": "NVDA"}}
    _svc244.dashboard = lambda *a, **k: {"positions": [
        {"kind": "EQUITY", "ticker": "AAPL", "value_eur": 1000.0},
        {"kind": "ETF", "name": "iShares Core S&P 500 UCITS ETF", "value_eur": 1000.0},
        {"kind": "DERIVATIVE", "isin": "TURBO1", "value_eur": 100.0, "exposure_eur": 500.0},
        {"kind": "EQUITY", "ticker": "ASML.AS", "value_eur": 500.0, "excluded": True}]}
    _bk244 = _ix244.book("spx")
    _r244["book"] = (_bk244["exposure_eur"], _bk244["overlap"], _bk244["in_index"], _bk244["held"],
                     [(x["sym"], x["via"]) for x in _bk244["over"]], [x["sym"] for x in _bk244["under"]])
    # the record: two years written once, then a row a night
    _v244 = {"breadth": {"d": ["2026-10-14", "2026-10-15", "2026-10-16"], "p200": [50.0, 51.0, 52.0], "p50": [40.0, 41.0, 42.0],
                         "nh": [1, 2, 3], "nl": [0, 0, 1], "idx": [1.0, 2.0, 3.0]},
            "conc": {"d": ["2026-10-14", "2026-10-15", "2026-10-16"], "top10": [38.0, 38.5, 39.0], "eff_n": [44.0, 44.0, 43.0],
                     "top10_now": 39.0, "eff_now": 43.0},
            "valuation": {"fwd_pe": 18.0}, "level": {"last": 3.0}}
    _ix244._record("spx", _v244)
    _ix244._record("spx", {**_v244, "valuation": {"fwd_pe": 18.5}})
    with store.conn() as _cn:
        _r244["record"] = (_cn.execute("SELECT src, COUNT(*) FROM idx_day WHERE ix = 'spx' GROUP BY src ORDER BY src").fetchall(),
                           _cn.execute("SELECT fwd_pe FROM idx_day WHERE ix = 'spx' AND d = '2026-10-16'").fetchone()[0])
    _r244["record"] = ([tuple(x) for x in _r244["record"][0]], _r244["record"][1], _ix244.record_series("spx")["top10"])
    # the alerts: near the high on few shares; the VIX curve inverted
    store.cache_put("idx:view:spx", {"breadth": {"p200": [45.0]}, "level": {"dd": -1.0}, "vol": {"inverted": True, "slope": 1.0549}})
    store.cache_put("idx:view:ndx", {"breadth": {"p200": [90.0]}, "level": {"dd": -0.5}})
    _al244 = _ix244.alerts()
    _r244["alerts"] = ([(a["level"], a["text"]) for a in _al244], _ix244.brief_line())
finally:
    (store.DB_PATH, _ern244.nasdaq_day, _svc244.dashboard, store.load_instruments, _ix244.members, _ix244._book_beta) = _sv244
    for _x in (_tmp244, _tmp244 + "-wal", _tmp244 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
_js244 = open(os.path.join(HERE, "backend", "static", "idx.js"), encoding="utf-8").read()
_tj244 = open(os.path.join(HERE, "backend", "static", "term.js"), encoding="utf-8").read()
_app244 = open(os.path.join(HERE, "backend", "portfolio_app.py"), encoding="utf-8").read()
_api244 = open(os.path.join(HERE, "backend", "pf", "api.py"), encoding="utf-8").read()
_mor244 = open(os.path.join(HERE, "backend", "pf", "morning.py"), encoding="utf-8").read()
_svs244 = open(os.path.join(HERE, "backend", "pf", "service.py"), encoding="utf-8").read()
_ok244 = (_r244.get("breadth") == (66.7, 2, 1, 1, 66.7, 3, 504)
          and _r244.get("conc") == (round(75 / 78 * 100, 2), round(78 ** 2 / 650, 1), round(69 / 72 * 100, 2), ["NVDA"], 200.0, 100.0)
          and _r244.get("sector") == [("T", 100.0, 25.0, 25.0), ("E", 10.0, -50.0, -5.0)]
          and _r244.get("contrib") == (("X", 50.0, 25.0), ("Y", 0.0, 0.0))
          and _r244.get("harm") == (16.0, 10.0)
          and _r244.get("fx") == ("5.6", "−5.6", "20.7", "7,819", "0.0", "53rd", "12th", "97th", "5–10% below its high")
          and _r244.get("insights") == ("The S&P 500 is 5.6% below its high of 7,819 on 6 Oct 2026, 6.2% above its 200-day average.", "warn", True)
          and _r244.get("history") == (True, "within 2% of its high", 1, -30, 30, True, True, False)
          and _r244.get("season") == (1, 0, 1, 1, 50.0, 1, 50, "2026-09-30", "2026-10-10")
          and _r244.get("book") == (2500.0, 88.0, 100.0, ["AAPL", "NVDA"], [("AAPL", ["shares", "tracker"]), ("NVDA", ["tracker", "turbo"])], ["MSFT"])
          and _r244.get("record") == ([("backfill", 2), ("daily", 1)], 18.5, [38.0, 38.5, 39.0])
          and [a[0] for a in (_r244.get("alerts") or [[]])[0]] == ["warn", "warn", "info"]
          and "(1.05)" in _r244["alerts"][0][1][1] and "−1.0% from its high" in (_r244["alerts"][1] or "")
          and "function squarify(" in _js244 and "/api/pf/idx/${IX}/book" in _js244 and "/api/pf/idx/compare" in _js244
          and 'code: "IDX"' in _tj244 and 'location.assign("/m/IDX?i=ndx")' in _tj244 and '"IDX": "idx.html"' in _app244
          and "idx.ensure()" in _app244 and '@router.get("/idx/{ix}"' in _api244 and '@router.get("/idx/{ix}/book"' in _api244
          and "idx.alerts()" in _svs244 and "part(\"indices\"" in _mor244 and _BUILD >= 40)
print(f"244 the indices              {ok(_ok244)}  breadth {(_r244.get('breadth') or ['?'])[0]}% above the 200-day, effective number {(_r244.get('conc') or ['?'] * 2)[1]}; "
      f"a sector held from the start {(_r244.get('sector') or [('?', 0, '?')])[0][2]}%; 10x and 40x make {(_r244.get('harm') or ['?'])[0]}x; "
      f"last year's EPS remembered, {(_r244.get('season') or ['?'] * 7)[6]} reports ahead kept; overlap {(_r244.get('book') or ['?'] * 2)[1]}%")

# 245 build 41: a share bought after the seed map was written gets its ticker
#     by itself (Coherent, Applied Digital and Alibaba stood at their purchase
#     price for six days), from Yahoo's listing in the holding's own currency
#     and never a listing in another; looked up once in six hours, its closes
#     stored at once; the alert says whether a ticker or the feed is missing.
#     A share Yahoo gives no beta (Sandisk, listed February 2025) shows a
#     one-year beta measured on five-day returns. And the book's value at
#     risk and expected shortfall: today's book replayed in euros, a turbo at
#     its exposure losing no more than it is worth, against your own money.
from pf import pricing as _pr245, service as _sv245, riskmap as _rm245, research as _rs245
from pf import service as _sv245m
import statistics as _st245
_r245 = {}
_Q245 = [{"symbol": "ASML", "quoteType": "EQUITY"}, {"symbol": "ASML.AS", "quoteType": "EQUITY"},
         {"symbol": "ASME.DE", "quoteType": "EQUITY"}, {"symbol": "ASML.MX", "quoteType": "EQUITY"}]
_r245["pick"] = (_pr245.pick_listing(_Q245, "EUR", "EAM"), _pr245.pick_listing(_Q245, "USD", "NDQ"),
                 _pr245.pick_listing(_Q245, "EUR", "XET"), _pr245.pick_listing(_Q245, "EUR", None),
                 _pr245.pick_listing(_Q245, "GBP", "LSE"), _pr245.pick_listing([{"symbol": "BRK.B", "quoteType": "EQUITY"}], "USD", "NSY"),
                 _pr245.pick_listing([{"symbol": "BABA", "quoteType": "EQUITY"}, {"symbol": "9988.HK", "quoteType": "EQUITY"}], "USD", "NSY"))
_sv245k = (store.DB_PATH, _sv245.missing_tickers, _sv245.enrich_instruments, store.load_instruments, _pr245.get_quotes,
           _rs245.daily, _rs245._closes, _rs245.benchmarks_for)
_tmp245 = os.path.join(_tf181.gettempdir(), "pf_b41check.db")
for _x in (_tmp245, _tmp245 + "-wal", _tmp245 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
try:
    store.DB_PATH = _tmp245
    store.init_db()
    # the lookup: once in six hours, its closes asked for at once
    _ins245 = {"A1": {"ticker": None}, "B2": {"ticker": None}}
    _asked, _calls245 = [], []
    _sv245.missing_tickers = lambda *a, **k: [i for i in ("A1", "B2") if not _ins245[i]["ticker"]]
    _sv245.enrich_instruments = lambda isins: _calls245.extend(isins) or [_ins245[i].update(ticker="COHR") for i in isins if i == "A1"] and 1
    store.load_instruments = lambda: _ins245
    _pr245.get_quotes = lambda t, fresh=False, now=None: _asked.append((tuple(t), fresh)) or {}
    _f1 = _sv245.resolve_missing(now=1_000_000.0)
    _f2 = _sv245.resolve_missing(now=1_000_000.0 + 3600)          # an hour on: B2 not asked again
    _f3 = _sv245.resolve_missing(now=1_000_000.0 + 7 * 3600)      # seven hours on: asked again
    _r245["resolve"] = (_f1, _f2, _f3, _asked, sorted(((store.cache_get("ticker:tried") or {}).get("m") or {}).items()), _calls245)
    # the alert: no ticker, or a ticker the feed has not answered for
    _base = {"totals": {}, "positions": []}
    _a1 = [a for a in _sv245.alerts({**_base, "data_quality": {"unpriced": ["COHERENT CORP."], "unpriced_no_ticker": ["COHERENT CORP."]}}) if a["code"] == "STALE"]
    _a2 = [a for a in _sv245.alerts({**_base, "data_quality": {"unpriced": ["COHERENT CORP."], "unpriced_no_ticker": []}}) if a["code"] == "STALE"]
    _a3 = [a for a in _sv245.alerts({**_base, "data_quality": {"unpriced": [], "unpriced_no_ticker": []}}) if a["code"] == "STALE"]
    _r245["alert"] = ("no ticker yet" in _a1[0]["detail"], "Yahoo did not answer" in _a2[0]["detail"], len(_a3))
    # a one-year beta: a share that moves twice the market, measured on five-day returns
    import random as _rnd245
    from datetime import date as _d245, timedelta as _td245
    _rnd245.seed(11)
    _days = [str(_d245.today() - _td245(days=400 - i)) for i in range(400)]
    _mk, _sk = [100.0], [100.0]
    for _i in range(1, 400):
        _m = _rnd245.gauss(0, 0.01)
        _mk.append(_mk[-1] * (1 + _m))
        _sk.append(_sk[-1] * (1 + 2 * _m + _rnd245.gauss(0, 0.002)))
    _rs245.daily = lambda s: {"bars": [[d, 0, 0, 0, c, 0, c] for d, c in zip(_days, _sk)]}
    _rs245._closes = lambda s: dict(zip(_days, _mk))
    _rs245.benchmarks_for = lambda s, ov: {"market": {"sym": "^GSPC", "label": "S&P 500"}}
    _b245 = _rs245.beta_1y("NEW", {})
    _rs245.daily = lambda s: {"bars": [[d, 0, 0, 0, c, 0, c] for d, c in list(zip(_days, _sk))[-90:]]}
    _r245["beta"] = (round(_b245["beta"], 1), _b245["basis"], _rs245.beta_1y("NEW", {}))
finally:
    (store.DB_PATH, _sv245.missing_tickers, _sv245.enrich_instruments, store.load_instruments, _pr245.get_quotes,
     _rs245.daily, _rs245._closes, _rs245.benchmarks_for) = _sv245k
    for _x in (_tmp245, _tmp245 + "-wal", _tmp245 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
# value at risk and expected shortfall, by hand
_ts245 = _rm245.tail_stats([float(-i) for i in range(1, 101)] + [0.0] * 100)    # losses 1..100 and 100 quiet days
_r245["stats"] = (_ts245["var95"], _ts245["var99"], _ts245["es975"], _ts245["es99"], _ts245["worst"], _ts245["n"],
                  round(_ts245["normal"]["es975"] / _ts245["sd"], 3))
# the replay: a share and a turbo three times leveraged on it; the share halves in one week
_d245s = [f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(300)]
_px245 = [100.0] * 150 + [50.0] + [50.0 * (1 + 0.001 * k) for k in range(149)]
_book245 = {"rows": [{"t": "AAA", "w": 1.0, "ccy": "EUR", "name": "Aaa"}], "total": 9000.0,
            "turbos": [{"t": "AAA", "value": 1000.0, "exposure": 3000.0, "name": "TURBO"}], "cash": -2000.0}
_tl245 = _rm245.tail(_book245, {"AAA": dict(zip(_d245s, _px245))}, _d245s)
_r245["tail"] = (_tl245["value_eur"], _tl245["own_eur"], _tl245["day"]["worst"], _tl245["week"]["worst"], _tl245["week"]["pct"]["worst"],
                 _tl245["week"]["episodes"][0]["loss"], len(_tl245["week"]["episodes"]),
                 [(x["label"], x["loss"]) for x in _tl245["week"]["drivers"]])
_pf245 = open(os.path.join(HERE, "backend", "static", "portfolio.html"), encoding="utf-8").read()
_tk245 = open(os.path.join(HERE, "backend", "static", "ticker.js"), encoding="utf-8").read()
_app245 = open(os.path.join(HERE, "backend", "portfolio_app.py"), encoding="utf-8").read()
_ok245 = (_r245.get("pick") == ("ASML.AS", "ASML", "ASME.DE", "ASML.AS", None, "BRK.B", "BABA")
          and _r245.get("resolve") == ({"A1": "COHR"}, {}, {}, [(("COHR",), True)], [("A1", [1_000_000.0, _sv245m.RESOLVE_VER]), ("B2", [1_000_000.0 + 7 * 3600, _sv245m.RESOLVE_VER])], ["A1", "B2", "B2"])
          and _r245.get("alert") == (True, True, 0)
          and (_r245.get("beta") or [None])[0] == 2.0 and "five-day returns against the S&P 500" in _r245["beta"][1] and _r245["beta"][2] is None
          and _r245.get("stats") == (90.0, 98.0, 98.0, 99.5, 100.0, 200, 2.338)
          and _r245.get("tail") == (10000, 8000, 5500, 5500, 68.8, 5500, 1, [("Aaa", 3210), ("turbo on AAA", 714)])
          and "function tailPanel(R)" in _pf245 and "${tailPanel(R)}" in _pf245
          and 'st.beta_basis ? "Beta 1Y" : "Beta"' in _tk245 and "service.ensure_tickers()" in _app245 and _BUILD >= 41)
print(f"245 tickers, beta, the tail   {ok(_ok245)}  a Dutch ISIN to ASML.AS, never a dollar line for a euro holding; looked up once in six hours; "
      f"a share at twice the market measures {(_r245.get('beta') or ['?'])[0]}; a share halving with a 3x turbo on it loses {(_r245.get('tail') or ['?'] * 3)[2]}, not 6000")

# 246 READ (build 43): the day's live coverage found in Yahoo's index feeds,
#     IBD's "(Live Coverage)" before Fool's "Stock Market Midday", 'live'
#     while New York trades and the last session's at a weekend; the editor's
#     picks kept to importance 3 and up and to today's budget, threads only
#     when they exist; an article without sources refused, a quote of fifteen
#     words or more dropped, a bar chart without a source not drawn; an update
#     is the story's next version, listed and briefed once; three scans a
#     weekday and one at a weekend, a missed one caught up within three hours;
#     a scan writes what fits the budget and stops at the plan's usage limit;
#     a reply that is not JSON is corrected once in its own session.
from pf import reader as _rd246
from datetime import datetime as _dt246, timezone as _tz246
_r246 = {}
_now246 = _dt246(2026, 10, 23, 16, 0, tzinfo=_tz246.utc)          # 12:00 in New York, a Friday
_ts246 = lambda h, d=23: _dt246(2026, 10, d, h, 0, tzinfo=_tz246.utc).timestamp()
_items246 = [
    {"title": "Stock Market Midday, Oct. 23: Energy Surges on Oil Price Spike", "url": "https://www.fool.com/a", "ts": _ts246(15), "source": "The Motley Fool", "summary": ""},
    {"title": "Stock Market Today: Dow Sheds 600 Points As Oil Jumps (Live Coverage)", "url": "https://finance.yahoo.com/m/b", "ts": _ts246(14), "source": "Yahoo Finance", "summary": ""},
    {"title": "Apple Is Up 28% This Year: Take Profits?", "url": "https://x.com/c", "ts": _ts246(15), "source": "x", "summary": ""},
    {"title": "Stock Market Today, Oct. 22: Tech Weakness Drags Nasdaq Lower", "url": "https://www.fool.com/d", "ts": _ts246(20, 22), "source": "The Motley Fool", "summary": ""}]
_l1 = _rd246.pick_live(_items246, _now246)
_l2 = _rd246.pick_live(_items246, _dt246(2026, 10, 23, 21, 30, tzinfo=_tz246.utc))
_l3 = _rd246.pick_live(_items246, _dt246(2026, 10, 25, 10, 0, tzinfo=_tz246.utc))
_l4 = _rd246.pick_live(_items246[2:3], _now246)
_r246["live"] = (_l1["state"], _l1["main"]["url"], [m["url"] for m in _l1["more"]], _l2["state"], _l3["state"], _l3["main"]["url"],
                 _l3["label"], _l4["state"])
# Fool's single-company pieces share the heading; after the close one of them was taken for the day's story
_one246 = {"title": "Stock Market Today, Oct. 23: SpaceX Achieves Rare Double Upgrade to Buy on AI Growth", "url": "https://www.fool.com/e",
           "ts": _ts246(20), "source": "The Motley Fool", "summary": ""}
_r246["ranks"] = ([_rd246.live_rank(t) for t in (_one246["title"], _items246[0]["title"], _items246[1]["title"], _items246[3]["title"],
                                                 "Stock Market Today, Oct. 22: Moderna Stock Plummets 18%", "Apple Is Up 28% This Year")],
                  _rd246.pick_live([_one246, _items246[0]], _dt246(2026, 10, 23, 21, 30, tzinfo=_tz246.utc))["main"]["url"])
_rss246 = (b'<?xml version="1.0"?><rss><channel><item><title>Stock Market Today: Live</title>'
           b'<link>https://finance.yahoo.com/m/x.html?.tsrc=rss</link><pubDate>Fri, 23 Oct 2026 19:17:00 +0000</pubDate>'
           b'<description>&lt;p&gt;Dow &lt;b&gt;down&lt;/b&gt;&lt;/p&gt;</description></item></channel></rss>')
_p246 = _rd246.parse_rss(_rss246)
_r246["rss"] = (_p246[0]["url"], _p246[0]["ts"] == _ts246(19) + 17 * 60, _p246[0]["summary"], _p246[0]["source"])
_d246 = _rd246.dedupe([{"title": "Iran strikes send oil above $110 as Hormuz fears grow", "url": "https://a.com/1", "score": 5},
                       {"title": "Iran strikes send oil above $110, Hormuz fears grow", "url": "https://b.com/2", "score": 4},
                       {"title": "Bloom Energy falls despite a beat", "url": "https://a.com/1?utm_source=x", "score": 3},
                       {"title": "Bloom Energy falls despite a beat", "url": "https://c.com/3", "score": 2}])
_r246["dedupe"] = [x["url"] for x in _d246]
_t246 = _rd246.clean_triage({"summary": "s", "picks": [
    {"kind": "event", "title": "A", "importance": 5, "tickers": ["CL=F", "nvda", "no way"], "thread": 7, "changed": "new",
     "sources": [{"url": "https://www.reuters.com/x"}, {"url": "javascript:alert(1)"}]},
    {"kind": "earnings", "title": "B", "importance": 4, "thread": 99},
    {"kind": "weird", "title": "C", "importance": 3},
    {"kind": "theme", "title": "D", "importance": 2, "why": "listicle"}]}, 2, [{"thread": 7}])
_r246["triage"] = ([(p["title"], p["importance"], p["thread"], p["tickers"], len(p["sources"])) for p in _t246["picks"]],
                   [s["why"] for s in _t246["skipped"]])
_base246 = {"headline": "H", "dek": "d", "importance": 4, "tickers": ["NVDA"], "sections": [{"h": "What happened", "p": "one string"}],
            "figures": [{"type": "bars", "title": "t", "items": [{"k": "a", "v": 1}, {"k": "b", "v": 2}]},
                        {"type": "bars", "title": "t", "items": [{"k": "a", "v": 1}, {"k": "b", "v": 2}], "source": "the 10-Q"},
                        {"type": "price", "tickers": ["NVDA", "^IXIC"], "days": 9999}, {"type": "hologram"},
                        {"type": "map", "countries": ["IR", "XYZ"], "points": [{"lat": 99, "lon": 0}]}],
            "quote": {"text": " ".join(["word"] * 15), "who": "someone"},
            "sources": [{"url": "https://www.reuters.com/x", "title": "x"}]}
_c246 = _rd246.clean_article(_base246)
try:
    _rd246.clean_article({**_base246, "sources": [{"url": "ftp://nope"}]})
    _nosrc = False
except ValueError:
    _nosrc = True
_fg246 = lambda t: next((f for f in _c246["figures"] if f["type"] == t), {})
_r246["article"] = ([f["type"] for f in _c246["figures"]], _fg246("price").get("days"), _fg246("map").get("countries"),
                    _fg246("map").get("points"), _c246["quote"], _c246["sections"][0]["p"], _nosrc,
                    _rd246.clean_article({**_base246, "quote": {"text": "short and sharp", "who": "x"}})["quote"]["text"])
_r246["json"] = (_rd246.parse_json('noise ```json\n{"a": 1}\n``` more ```json\n{"a": 2}\n```')["a"], _rd246.parse_json('x {"b": 3} y')["b"])
_sv246 = (store.DB_PATH, _rd246.run_ai, _rd246.writer_ready, _rd246.gather, _rd246._book, _rd246._calendar, _rd246._market,
          _rd246._ticker_facts, store.get_setting)
_tmp246 = os.path.join(_tf181.gettempdir(), "pf_read43check.db")
for _x in (_tmp246, _tmp246 + "-wal", _tmp246 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
try:
    store.DB_PATH = _tmp246
    store.init_db()
    _day = _rd246.now_ams().date().isoformat()
    # threads: an update is the next version; listed, briefed and alerted once
    _a1 = _rd246.save_article({**_c246, "importance": 5, "headline": "First"}, "manual", _day, {"session": "s1"}, None)
    _sid = _rd246._rows("SELECT story_id FROM read_article WHERE id=?", (_a1,))[0]["story_id"]
    _a2 = _rd246.save_article({**_c246, "importance": 5, "headline": "Second", "changed": "it moved"}, "manual", _day, {}, None, thread=_sid)
    _a3 = _rd246.save_article({**_c246, "headline": "Other"}, "manual", _day, {}, None, thread=12345)
    _r246["threads"] = ([(x["headline"], x["version"]) for x in _rd246.listing()], len(_rd246.open_threads()),
                        _rd246.brief_line().count("Second"), "First" in (_rd246.brief_line() or ""),
                        [(a["text"], a["href"]) for a in _rd246.alerts()], _rd246.written_on(_day))
    # the schedule
    _A = _rd246.AMS
    _due = lambda d, h, m, done=set(): _rd246.due_slot(_dt246(2026, 10, d, h, m, tzinfo=_A), done=done)
    _r246["schedule"] = (_due(23, 7, 0), _due(23, 7, 20), _due(23, 10, 30), _due(23, 10, 10), _due(23, 16, 45),
                         _due(23, 17, 0, {"midday"}), _due(23, 23, 0), _due(24, 9, 45), _due(25, 9, 45), _due(25, 14, 0),
                         _rd246.next_slot(_dt246(2026, 10, 23, 23, 0, tzinfo=_A))["slot"],
                         _rd246.next_slot(_dt246(2026, 10, 25, 10, 0, tzinfo=_A))["at"][:16])
    # a scan, with a writer that answers from a script
    _calls = []
    _replies = {"triage": '```json\n{"summary": "busy", "picks": ['
                          '{"kind": "event", "title": "P1", "importance": 5, "sources": [{"url": "https://r.com/1"}]},'
                          '{"kind": "macro", "title": "P2", "importance": 4},'
                          '{"kind": "theme", "title": "P3", "importance": 3}]}\n```'}

    def _fake(job, system, text, web, key, resume=None, max_uses=5):
        _calls.append((("triage" if system == _rd246.TRIAGE_SYSTEM else "write" if system == _rd246.WRITE_SYSTEM else "other"), web, bool(resume)))
        if system == _rd246.TRIAGE_SYSTEM:
            body = _replies["triage"]
        elif resume:
            body = '```json\n' + json.dumps({**_base246, "headline": "Fixed"}) + '\n```'
        elif "P2" in text and _replies.get("limit"):
            raise RuntimeError("your Claude plan's usage limit is reached for now")
        elif "P1" in text and _replies.get("broken"):
            body = "this is not json"
        else:
            body = '```json\n' + json.dumps({**_base246, "headline": "Written " + ("P1" if "P1" in text else "P2")}) + '\n```'
        return {"text": body, "session": "sess", "model": "fake", "usage": {}, "api_usd": 0.1, "cost_usd": None, "writer": "claude-code", "ms": 1}
    import json
    _rd246.run_ai, _rd246.writer_ready = _fake, (lambda: None)
    _rd246.gather = lambda **k: [{"title": "x", "url": "https://x.com", "ts": None, "source": "x", "via": "news feed"}]
    _rd246._book = lambda: {"total": 1000, "cash": 0, "rows": [{"t": "NVDA", "name": "Nvidia", "turbo": False, "value": 100, "exposure": 100,
                                                                "w": 10.0, "day": 1.0, "sector": "Technology"}], "sectors": {"Technology": 10.0}}
    _rd246._calendar = lambda days=2: {"earnings": [], "macro": []}
    _rd246._market = lambda: []
    _rd246._ticker_facts = lambda syms: []
    _gs = store.get_setting
    store.get_setting = lambda k, d=None: "5" if k == "read_daily_max" else _gs(k, d)
    _j1 = {"text": ""}
    _rd246._run_scan(_j1, "midday")                       # 3 already today, a cap of 5: two may be written
    _s1 = _rd246.scans(_day)[-1]
    _replies.update(limit=True, broken=True)
    store.get_setting = lambda k, d=None: "9" if k == "read_daily_max" else _gs(k, d)
    _calls.clear()
    _j2 = {"text": ""}
    _rd246._run_scan(_j2, "evening")
    _s2 = _rd246.scans(_day)[-1]
    _r246["scan"] = (_j1["state"], len(_j1["written"]), _s1["n_written"], [p["title"] for p in _s1["picks"]],
                     [s["why"] for s in _s1["skipped"]], _j2["state"], len(_j2["written"]),
                     [s["why"].split(":")[0] for s in _s2["skipped"]], _calls,
                     _rd246.article(_j2["written"][0], mark_seen=False, figures=False)["headline"] if _j2.get("written") else None)
    _j3 = {"text": ""}
    store.get_setting = lambda k, d=None: "1" if k == "read_daily_max" else _gs(k, d)
    _rd246._run_scan(_j3, "manual")
    _r246["full"] = (_j3["state"], _j3.get("error", ""))
    # asking: the article's own session, resumed in the folder it was written in (Claude Code keeps sessions per folder)
    _seen246 = []
    _rd246.run_ai = lambda job, system, text, web, key, resume=None, max_uses=5: (_seen246.append((key, resume, system == _rd246.ASK_SYSTEM))
                                                                                  or {"text": "an answer", "session": resume, "model": "fake", "usage": {},
                                                                                      "api_usd": 0.01, "cost_usd": None, "writer": "claude-code", "ms": 1})
    _old = _rd246.save_article({**_c246, "headline": "Old"}, "evening", _day, {"session": "s-old", "pick": {"title": "Iran: oil up"}}, None)
    for _aid in (_j1["written"][0], _old):
        _jj = {"text": ""}
        _rd246._run_ask(_jj, _aid, "what does it mean for me?")
    _r246["ask"] = (_seen246, [len(_rd246.answers(a)) for a in (_j1["written"][0], _old)])
finally:
    (store.DB_PATH, _rd246.run_ai, _rd246.writer_ready, _rd246.gather, _rd246._book, _rd246._calendar, _rd246._market,
     _rd246._ticker_facts, store.get_setting) = _sv246
    for _x in (_tmp246, _tmp246 + "-wal", _tmp246 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
_js246 = open(os.path.join(HERE, "backend", "static", "read.js"), encoding="utf-8").read()
_ix246 = open(os.path.join(HERE, "backend", "static", "index.html"), encoding="utf-8").read()
_tj246 = open(os.path.join(HERE, "backend", "static", "term.js"), encoding="utf-8").read()
_app246 = open(os.path.join(HERE, "backend", "portfolio_app.py"), encoding="utf-8").read()
_api246 = open(os.path.join(HERE, "backend", "pf", "api.py"), encoding="utf-8").read()
_svc246 = open(os.path.join(HERE, "backend", "pf", "service.py"), encoding="utf-8").read()
_mor246 = open(os.path.join(HERE, "backend", "pf", "morning.py"), encoding="utf-8").read()
_gi246 = open(os.path.join(HERE, ".gitignore"), encoding="utf-8").read()
_ok246 = (_r246.get("live") == ("live", "https://finance.yahoo.com/m/b", ["https://www.fool.com/a"], "today", "last", "https://finance.yahoo.com/m/b",
                                "Last session · Fri 23 Oct", "none")
          and _r246.get("rss") == ("https://finance.yahoo.com/m/x.html", True, "Dow down", "Yahoo Finance")
          and _r246.get("ranks") == ([1, 2, 3, 2, 1, 0], "https://www.fool.com/a")
          and _r246.get("dedupe") == ["https://a.com/1", "https://c.com/3"]
          and _r246.get("triage") == ([("A", 5, 7, ["CL=F", "NVDA"], 1), ("B", 4, None, [], 0)],
                                      ["importance 2: listicle", "importance 3, over today's budget"])
          and _r246.get("article") == (["bars", "price", "map"], 400, ["IR"], [], None, ["one string"], True, "short and sharp")
          and _r246.get("json") == (2, 3)
          and _r246.get("threads") and _r246["threads"][0] == [("Other", 1), ("Second", 2)] and _r246["threads"][1] == 2 and _r246["threads"][2] == 1
          and _r246["threads"][3] is False and _r246["threads"][4] == [("Second", f"/m/READ#a={_a2}")] and _r246["threads"][5] == 3
          and _r246.get("schedule") == (None, "morning", None, "morning", "midday", None, "evening", "review", "ahead", None,
                                        "review", "2026-10-26T07:15")
          and (_r246.get("scan") or [None])[:5] == ("done", 2, 2, ["P1", "P2"], ["importance 3, over today's budget"])
          and _r246["scan"][5:8] == ("done", 1, ["could not be written", "postponed"])
          and _r246["scan"][8] == [("triage", "search", False), ("write", "fetch", False), ("write", "", True), ("write", "fetch", False)]
          and _r246["scan"][9] == "Fixed"
          and _r246.get("full") == ("error", "RuntimeError: today's budget of 1 article is used")
          and _r246.get("ask") == ([(_rd246._pick_key(_day, "midday", "P1"), "sess", True), (f"{_day}-evening-iran-oil-up", "s-old", True)], [1, 1])
          and "function liveCard(" in _js246 and "function drawMap(" in _js246 and "/api/pf/read/home" in _ix246
          and "x.href || MOD[x.code].href" in _ix246 and 'code: "READ"' in _tj246 and '"READ": "read.html"' in _app246
          and "reader.ensure()" in _app246 and '@router.get("/read", response_class=Safe)' in _api246 and "reader.alerts()" in _svc246
          and 'part("articles"' in _mor246 and "read_work/" in _gi246 and _BUILD >= 43)
print(f"246 READ, the article writer {ok(_ok246)}  IBD's live page before Fool's midday; picks at importance 3+ within the budget; "
      f"an update is version 2, briefed once; a scan writes {(_r246.get('scan') or ['?'] * 3)[2]} of 3 at a cap of 5 and stops at the usage limit")

# 247 the stock page's journal saves (build 44). api.py declared NoteBody
#     twice: {body} for the journal and {question, web} for the brief's and
#     the oil page's notes. With postponed annotations FastAPI read the name
#     again when the app mounted the router, after the second had replaced
#     the first, so every journal note was a 500 and the Add button looked
#     dead. No request model may share a name, and a note is added and
#     deleted through the mounted app.
import re as _re247
from fastapi.testclient import TestClient as _TC247
import portfolio_app as _pa247
_api247 = open(os.path.join(HERE, "backend", "pf", "api.py"), encoding="utf-8").read()
_names247 = _re247.findall(r"^class (\w+)\(BaseModel\)", _api247, _re247.M)
_dup247 = sorted({n for n in _names247 if _names247.count(n) > 1})
_tmp247 = os.path.join(_tf181.gettempdir(), "pf_journal44check.db")
for _x in (_tmp247, _tmp247 + "-wal", _tmp247 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_sv247 = store.DB_PATH
try:
    store.DB_PATH = _tmp247
    store.init_db()
    _c247 = _TC247(_pa247.app, raise_server_exceptions=False)
    _a247 = _c247.post("/api/pf/research/TEST/note", json={"body": "a dated note"})
    _ids247 = [n["id"] for n in (_a247.json().get("notes") or []) if n.get("body") == "a dated note"] if _a247.status_code == 200 else []
    _d247 = _c247.delete(f"/api/pf/research/note/{_ids247[0]}").status_code if _ids247 else None
    _e247 = _c247.post("/api/pf/research/TEST/note", json={"body": "  "}).status_code
finally:
    store.DB_PATH = _sv247
    for _x in (_tmp247, _tmp247 + "-wal", _tmp247 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
_tk247 = open(os.path.join(HERE, "backend", "static", "ticker.js"), encoding="utf-8").read()
_ok247 = (not _dup247 and _a247.status_code == 200 and len(_ids247) == 1 and _d247 == 200 and _e247 == 400
          and 'id="noteMsg"' in _tk247 and _BUILD >= 44)
print(f"247 the journal saves         {ok(_ok247)}  add {_a247.status_code}, delete {_d247}, an empty note {_e247}; "
      f"request models sharing a name: {', '.join(_dup247) or 'none'}")

# 248 a deposit still only reserved is capital, and Celestica is priced
#     (build 45). An iDEAL payment is in the balance from its reservation, a
#     day or two before the deposit row: capital counted the deposit rows
#     only, and one deposit read as profit, nearly doubling it for two days.
#     And Yahoo's ISIN search listed Celestica's Canadian ISIN only
#     on Toronto, in Canadian dollars, never the NYSE line held: the
#     company's name finds it, kept to results bearing that name, and a
#     share the older lookup missed is tried again at once.
from types import SimpleNamespace as _NS248
from datetime import datetime as _dt248
from pf import service as _sv248, pricing as _pr248
_row248 = lambda d, kind, v, note, bal, seq=0: _NS248(kind=kind, local_value=v, currency="EUR", trade_dt=_dt248.fromisoformat(d),
                                                     balance=bal, balance_ccy="EUR", seq=seq, note=note, source="degiro:account", isin=None)
_tx248 = [_row248("2026-01-28T12:12", "INTERNAL", 2000.0, "Reservation iDEAL", 2153.49),
          _row248("2026-01-29T01:30", "INTERNAL", -2000.0, "Reservation iDEAL", -1791.42, 1),
          _row248("2026-01-29T01:30", "DEPOSIT", 2000.0, "iDEAL Deposit", 208.58, 0),
          _row248("2026-06-17T10:00", "WITHDRAWAL", -500.0, "flatex terugstorting", -291.42),
          _row248("2026-10-20T11:05", "INTERNAL", 3000.0, "Reservation iDEAL", 394.34),
          _row248("2026-10-20T11:10", "INTERNAL", 9999.0, "Degiro Cash Sweep Transfer", 0.0)]
_fl248 = _sv248.cash_account(_tx248)["flows"]
_ci248 = _sv248.capital_in(_tx248, _fl248, lambda t: t.local_value or 0.0)
_none248 = _sv248.capital_in([_row248("2026-10-20T11:05", "INTERNAL", 3000.0, "Reservation iDEAL", 394.34)], [], lambda t: t.local_value)
_r248 = {"capital": (_ci248["deposits"], _ci248["withdrawals"], _ci248["pending"], _ci248["net"], _none248["net"])}
# the lookup: the ISIN finds only Toronto; the name finds the NYSE line among strangers
_sv248k = _pr248._yahoo_search
_asked248 = []


def _fake248(q):
    _asked248.append(q)
    if q.startswith("CA15101Q"):
        return [{"symbol": "CLS.TO", "quoteType": "EQUITY", "shortname": "CELESTICA INC"}]
    return [{"symbol": "CLSK", "quoteType": "EQUITY", "shortname": "CleanSpark, Inc."},
            {"symbol": "CLS.TO", "quoteType": "EQUITY", "shortname": "CELESTICA INC"},
            {"symbol": "CLHI.TO", "quoteType": "ETF", "shortname": "NINEPOINT CELESTICA HIGHSHARES"},
            {"symbol": "CLS", "quoteType": "EQUITY", "shortname": "Celestica, Inc."}]


try:
    _pr248._yahoo_search = _fake248
    _r248["resolve"] = (_pr248.resolve_ticker("CA15101Q2071", "CELESTICA INC.", "USD", "NSY"),
                        _pr248.resolve_ticker("CA15101Q2071", "CELESTICA INC.", "CAD", "TSE"),
                        _pr248.resolve_ticker("CA15101Q2071", "", "USD", "NSY"), len(_asked248))
finally:
    _pr248._yahoo_search = _sv248k
_r248["key"] = (_pr248.name_key("CELESTICA INC."), _pr248.name_key("ADR ON ALIBABA GROUP HOLDING LIMITED"), _pr248.name_key("THE CO"))
# a share tried by the older lookup an hour ago is tried again now; one tried by this lookup waits
_svk248 = (_sv248.missing_tickers, _sv248.enrich_instruments, store.load_instruments, _pr248.get_quotes, store.cache_get, store.cache_put)
_seen248, _box248 = [], {"ticker:tried": {"m": {"OLD": 999_000.0, "NEW": [999_000.0, _sv248.RESOLVE_VER]}}}
try:
    _sv248.missing_tickers = lambda *a, **k: ["OLD", "NEW"]
    _sv248.enrich_instruments = lambda isins: _seen248.extend(isins) or 0
    store.load_instruments = lambda: {}
    store.cache_get = lambda k, max_age=None: _box248.get(k)
    store.cache_put = lambda k, v: _box248.__setitem__(k, v)
    _sv248.resolve_missing(now=1_000_000.0 + 3600)
    _r248["retry"] = (_seen248, _box248["ticker:tried"]["m"]["OLD"])
finally:
    (_sv248.missing_tickers, _sv248.enrich_instruments, store.load_instruments, _pr248.get_quotes, store.cache_get, store.cache_put) = _svk248
_ok248 = (_r248.get("capital") == (2000.0, -500.0, 3000.0, 4500.0, 0.0)
          and _r248.get("resolve") == (("CLS", "yahoo (by name)"), ("CLS.TO", "yahoo"), (None, "unresolved"), 4)
          and _r248.get("key") == ("CELESTICA", "ALIBABA", None)
          and _r248.get("retry") == (["OLD"], [1_000_000.0 + 3600, _sv248.RESOLVE_VER]) and _BUILD >= 45)
print(f"248 reserved deposits, CLS   {ok(_ok248)}  a reserved €3,000 counts as capital (net {(_r248.get('capital') or ['?'] * 4)[3]}, "
      f"not 1,500); Celestica's NYSE line found by name: {(_r248.get('resolve') or [['?']])[0][0]}; the older lookup's misses tried again at once")

# 249 READ opens after a failed scan (build 45). With no scan yet today the
#     page shows the last one before, read raw from its table, and its lists
#     were decoded only when `picks` was text. The 27 October evening scan
#     stopped before picking anything (Claude Code was signed out), so its
#     `picks` was NULL and its `skipped` reached the page as the string "[]":
#     after midnight READ stopped loading ("sk.map is not a function"). The
#     last scan of an earlier day is now always decoded, and the page draws
#     the editor's pass only from a list.
from datetime import timedelta as _td249
from pf import reader as _rd249
_tmp249 = os.path.join(_tf181.gettempdir(), "pf_read45check.db")
for _x in (_tmp249, _tmp249 + "-wal", _tmp249 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_orig249 = store.DB_PATH
_keep249 = (_rd249.live, _rd249.writer_ready)
_r249 = {}


def _scan249(d, state, picks, skipped, meta):
    with store.conn() as _c:
        _c.execute("INSERT INTO read_scan (d, slot, started, state, picks, skipped, meta, error) VALUES (?, 'evening', ?, ?, ?, ?, ?, ?)",
                   (d, d + "T21:45:00+00:00", state, picks, skipped, meta, "stopped" if state == "error" else None))
    _o = _rd249.overview()["last_scan"]
    return (type(_o["picks"]).__name__, type(_o["skipped"]).__name__, type(_o["meta"]).__name__, len(_o["skipped"]))


try:
    store.DB_PATH = _tmp249
    store.init_db()
    _rd249.live = lambda *a, **k: {}
    _rd249.writer_ready = lambda: None
    _today249 = _rd249.now_ams().date()
    _yday249 = (_today249 - _td249(days=1)).isoformat()
    _r249["earlier, done"] = _scan249(_yday249, "done", '[{"title": "a"}]', '[{"title": "b", "why": "c"}]', '{"runs": []}')
    _r249["earlier, failed"] = _scan249(_yday249, "error", None, "[]", None)
    _r249["today, failed"] = _scan249(_today249.isoformat(), "error", None, '[{"title": "d", "why": "e"}]', None)
finally:
    store.DB_PATH = _orig249
    (_rd249.live, _rd249.writer_ready) = _keep249
    for _x in (_tmp249, _tmp249 + "-wal", _tmp249 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
_rj249 = open(os.path.join(HERE, "backend", "static", "read.js"), encoding="utf-8").read()
_ok249 = (_r249.get("earlier, done") == ("list", "list", "dict", 1) and _r249.get("earlier, failed") == ("list", "list", "dict", 0)
          and _r249.get("today, failed") == ("list", "list", "dict", 1) and "Array.isArray(s && s.skipped)" in _rj249)
print(f"249 READ after a failed scan {ok(_ok249)}  yesterday's failed scan reaches the page as lists "
      f"({', '.join((_r249.get('earlier, failed') or ['?'] * 3)[:3])}), not text; the page draws the editor's pass from a list only")

# ---------------------------------------------------------------------------
# Build 47: Koers AI, the paper-trading contest
# ---------------------------------------------------------------------------

# 250 Koers AI keeps to the contest's rules (build 47). Claude's €100,000
#     against Rick and the index, here on a throwaway database with made-up
#     prices and a stand-in for Claude. A buy is cut to 20% of the portfolio
#     when decided and again at its open (a jump to 140 refuses a top-up); an
#     order for a ticker it was not sent, for something not held, under €10
#     or without an amount is refused. Orders carry the time the answer came,
#     and a decision settles whole once every order has its open: at noon on
#     Tuesday Amsterdam has opened and New York has not, so nothing fills;
#     at 16:00 everything does, less 0.01%. What it reads holds nothing that
#     Rick's book shaped: no headline the news feed tied to Rick, no "fit",
#     no pin; but every stock Rick holds, unmarked, as the user asked (build
#     48), beside the broad market (each region's largest, read in full by
#     the scan wherever they stand). What it was sent is kept with the
#     decision. And no web search. Friday from 23:00 and only while the markets are shut; one
#     check an evening for a 5% move or a report; no Sharpe ratio before
#     twenty trading days.
import time as _time250
from datetime import datetime as _dt250
from pf import koersai as _K250, reader as _rd250, watch as _wt250
_tmp250 = os.path.join(_tf181.gettempdir(), "pf_kai47check.db")
for _x in (_tmp250, _tmp250 + "-wal", _tmp250 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_orig250 = store.DB_PATH
_keep250 = (_K250.bars, _K250._ccy_of, _K250.now_ams, _rd250.run_json, _rd250.writer_ready, _rd250._rss, dict(_K250.JOB), dict(_K250._FAILS),
            _wt250.LARGE_N, _K250.earn_dates)
_B250 = {"2026-10-16": (100, 100, 100, 100, 100), "2026-10-19": (100, 101, 99, 100, 100), "2026-10-20": (100, 104, 99, 103, 103),
         "2026-10-21": (103, 104, 100, 102, 102), "2026-10-22": (140, 141, 139, 140, 140), "2026-10-23": (140, 141, 120, 126, 126),
         "2026-10-26": (126, 127, 125, 126, 126)}
_A250 = lambda *a: _dt250(*a, tzinfo=_K250.AMS)
_clock250 = {"t": _A250(2026, 10, 19, 23, 5)}
_seen250, _plan250, _r250 = {}, [], {}


def _fake250(job, system, text, web, key):
    _seen250.update(text=text, web=web, system=system)
    _clock250["t"] = _clock250["t"].replace(minute=_clock250["t"].minute + 4)       # the answer comes four minutes later
    return _plan250.pop(0), {"model": "stand-in", "usage": {}, "api_usd": None, "ms": 1, "session": None}


_ok250 = False
try:
    store.DB_PATH = _tmp250
    store.init_db()
    _K250.bars = lambda s: dict(_B250)
    _K250._ccy_of = lambda s: "EUR"
    _K250.now_ams = lambda: _clock250["t"]
    _rd250.run_json = _fake250
    _rd250.writer_ready = lambda: None
    _rd250._rss = lambda q: []
    _K250.earn_dates = lambda s: []                # Yahoo's report dates (build 56): none for made-up tickers
    _wt250.LARGE_N = {"US": 1, "EU": 1}          # the largest company of each region, in this little universe
    # the scan reads in full: the dips, the pins and holdings, and each region's largest wherever they stand
    _r250["full"] = _wt250.full_read({"A": {}, "B": {}, "C": {}, "D": {}, "E": {}},
                                     {"A": {"dd52": -20}, "B": {"dd52": -5}, "C": {"dd52": -1}, "D": {"dd52": -2}}, {"D"}, {},
                                     [{"sym": "C", "region": "US", "mcap_usd": 9e12}, {"sym": "B", "region": "US", "mcap_usd": 1e9},
                                      {"sym": "E", "region": "EU", "mcap_usd": 5e11}])
    _q250 = {"quality": 100, "growth": 100, "estimates": 100, "value": 50}
    _m250 = {"roic": 30, "fcf_margin": 25, "rev_cagr3": 12, "om": 30, "om_avg3": 29, "nd_ebitda": 0.5}
    _ts250 = lambda *a: _A250(*a).timestamp()
    store.cache_put("wl:scan", {"scanned_at": _time250.time(), "leaders": {"US": ["LEAD"], "EU": []}, "rows": [
        {"sym": "TEST.AS", "name": "Test", "px": 100, "dd52": -20, "gs": _q250, "m": _m250, "pr": {}, "earn_ts": _ts250(2026, 10, 1, 8, 0)},
        {"sym": "TUS", "name": "Test US", "px": 100, "dd52": -25, "gs": _q250, "m": _m250, "pr": {}, "earn_ts": _ts250(2026, 10, 22, 21, 0)},
        {"sym": "LEAD", "name": "Leader", "px": 100, "dd52": -2, "gs": _q250, "m": _m250, "pr": {}},
        {"sym": "HELD.AS", "name": "Rick's", "px": 100, "dd52": -3, "gs": {"quality": 40}, "m": _m250, "pr": {}, "fit": {"held": True, "w": 7.6}},
        {"sym": "PIN.AS", "name": "Pinned", "px": 100, "dd52": -2, "gs": _q250, "m": _m250, "pr": {}},
        {"sym": "BIG", "name": "Big", "px": 100, "dd52": -1, "gs": _q250, "m": _m250, "pr": {}},
        {"sym": "BAD.WA", "name": "Warsaw", "px": 100, "dd52": -30, "gs": _q250, "m": _m250, "pr": {}}]})
    store.cache_put("wl:universe", {"rows": [{"sym": s, "region": rg, "mcap_usd": mc} for s, rg, mc in (
        ("BIG", "US", 3e12), ("TUS", "US", 1e10), ("LEAD", "US", 1e10), ("TEST.AS", "EU", 5e11), ("HELD.AS", "EU", 1e10),
        ("PIN.AS", "EU", 2e10))]})
    store.cache_put("news", {"items": [{"headline": "Rick's own holding jumps", "mine": True, "ts": _ts250(2026, 10, 19, 20, 0), "source": "x"},
                                       {"headline": "Fed holds rates", "mine": False, "ts": _ts250(2026, 10, 19, 20, 0), "source": "y"}]})
    _K250.start(at=_A250(2026, 10, 19, 23, 5))
    # the first decision waits for a scan of the day's close: one from 21:00 is older than New York's 22:00 close
    _sc250 = store.cache_get("wl:scan")
    store.cache_put("wl:scan", {**_sc250, "scanned_at": _A250(2026, 10, 19, 21, 0).timestamp()})
    _f250 = [_K250.first_due()]
    store.cache_put("wl:scan", _sc250)
    _f250.append(_K250.first_due())
    _plan250.append({"date": "2026-10-19", "journal": "j", "message_to_rival": "m", "theses": [{"ticker": "TUS", "thesis": "t", "exit_if": "e"}],
                     "trades": [{"action": "BUY", "ticker": "TEST.AS", "amount_eur": 30000, "reason": "a"},
                                {"action": "buy", "ticker": "TUS", "amount_eur": "15,000", "reason": "b"},
                                {"action": "BUY", "ticker": "LEAD", "amount_eur": 5000, "reason": "c"},
                                {"action": "BUY", "ticker": "BAD.WA", "amount_eur": 5000, "reason": "x"},
                                {"action": "SELL", "ticker": "XYZ", "amount_eur": "ALL", "reason": "x"},
                                {"action": "BUY", "ticker": "TUS", "amount_eur": 5, "reason": "x"},
                                {"action": "BUY", "ticker": "TUS", "amount_eur": "ALL", "reason": "x"}]})
    _d = _K250.decision(_K250.decide({}, "start"))
    _r250["first"] = tuple(_f250 + [_K250.first_due()])
    _r250["orders"] = [(t["ticker"], t["amount"]) for t in _d["trades"]]
    _r250["refused"] = sorted(t["ticker"] for t in _d["rejected"])
    _r250["stamped"] = {o["ts"] for o in _K250._rows("SELECT ts FROM kai_order")} == {"2026-10-19T23:09:00+02:00"}
    _t = _seen250["text"]
    _r250["sight"] = ('"fit"' not in _t and "Rick's own holding" not in _t and "Fed holds rates" in _t and '"t": "HELD.AS"' in _t and '"t": "BIG"' in _t and "PIN.AS" not in _t
                      and "BAD.WA" not in _t and '"t": "LEAD"' in _t and _seen250["web"] == ""
                      and '"rep": "2026-10-22"' in _t and '"rep": "2026-10-01"' not in _t
                      and "moves in a game" in _seen250["system"] and "message_to_rival" in _seen250["system"])
    _r250["noon"] = len(_K250.fill_pending(_A250(2026, 10, 20, 12, 0)))
    _r250["four"] = [(o["ticker"], o["status"], o["fill_d"]) for o in _K250.fill_pending(_A250(2026, 10, 20, 16, 0))]
    _r250["cash1"] = round(_K250.holdings()["cash"], 2)
    with store.conn() as _c:
        _c.execute("INSERT INTO kai_decision (ts, kind, journal) VALUES ('2026-10-21T23:05:00+02:00', 'manual', 'm')")
        for _side, _tk, _amt in (("BUY", "TEST.AS", 1000.0), ("SELL", "TUS", 2000.0)):
            _c.execute("INSERT INTO kai_order (decision_id, ts, ticker, side, amount_eur, amount_all, reason, status) "
                       "VALUES (2, '2026-10-21T23:05:00+02:00', ?, ?, ?, 0, 'r', 'pending')", (_tk, _side, _amt))
    _r250["jump"] = sorted((o["ticker"], o["status"], o.get("note")) for o in _K250.fill_pending(_A250(2026, 10, 22, 16, 0)))
    _K250._remember_reports()
    _e1 = _K250.event_due(_A250(2026, 10, 22, 23, 5))
    _r250["event"] = (_K250.event_due(_A250(2026, 10, 22, 22, 50)), _e1, _K250.event_due(_A250(2026, 10, 22, 23, 30)))
    _wd = _K250.weekly_due
    _r250["weekly"] = (_wd(_A250(2026, 10, 23, 22, 50)), _wd(_A250(2026, 10, 23, 23, 5)), _wd(_A250(2026, 10, 24, 10, 0)),
                       _wd(_A250(2026, 10, 26, 10, 0)))
    _clock250["t"] = _A250(2026, 10, 23, 23, 5)
    _plan250.append({"date": "2026-10-23", "journal": "j2", "trades": [{"action": "SELL", "ticker": "LEAD", "amount_eur": "ALL", "reason": "s"}]})
    _K250.decide({}, "weekly")
    _r250["after"] = _wd(_A250(2026, 10, 24, 10, 0))
    _K250.fill_pending(_A250(2026, 10, 26, 16, 0))
    _clock250["t"] = _A250(2026, 10, 26, 23, 0)
    _v = _K250.value_on("2026-10-26", _K250.FX("2026-10-19"), {})
    _r250["value"] = (round(_v["value"], 2), round(_v["cash"], 2))
    _s250 = _K250.stats(_K250.curve())
    _r250["score"] = (_s250["kai"]["ret"], _s250["kai"]["sharpe"], _s250["kai"]["days"], _s250["spx"]["ret"], _s250["trades"]["n"])
    _K250._FAILS.update(day=None, n=0)
    _K250.JOB.clear(); _K250.JOB.update(state="error", finished=_time250.time() - 10)
    _g = [_K250._may_run()]
    _K250.JOB.update(finished=_time250.time() - 4000)
    _g.append(_K250._may_run())
    _r250["guard"] = tuple(_g)
    _ovd = _K250.overview()["decisions"]
    _r250["kept"] = (_K250.decision(1)["material"] == _t, all("material" not in d for d in _ovd),
                     [d["has_material"] for d in _ovd])
    _ok250 = (_r250["first"] == (False, True, False) and _r250["orders"] == [("TEST.AS", 20000.0), ("TUS", 15000.0), ("LEAD", 5000.0)]
              and _r250["refused"] == ["BAD.WA", "TUS", "TUS", "XYZ"] and _r250["full"] == ["A", "D", "C", "E"] and _r250["stamped"] and _r250["sight"]
              and _r250["noon"] == 0 and _r250["four"] == [("TEST.AS", "filled", "2026-10-20"), ("TUS", "filled", "2026-10-20"),
                                                         ("LEAD", "filled", "2026-10-20")]
              and _r250["cash1"] == 59996.0
              and _r250["jump"] == [("TEST.AS", "rejected", "20% of the portfolio reached at the open"), ("TUS", "filled", "14.2857 at 140.00 EUR (€2,000.00)")]
              and _r250["event"][0] is None and "TEST.AS moved +37.3% today." in (_r250["event"][1] or "")
              and "TUS reported earnings today." in (_r250["event"][1] or "") and _r250["event"][2] is None
              and _r250["weekly"] == (False, True, True, False) and _r250["after"] is False
              and _r250["value"] == (110595.17, 68295.17) and _r250["score"] == (10.59, None, 5, 26.0, 5)
              and _r250["guard"] == (False, True) and _r250["kept"] == (True, True, [True, False, True]))
finally:
    store.DB_PATH = _orig250
    (_K250.bars, _K250._ccy_of, _K250.now_ams, _rd250.run_json, _rd250.writer_ready, _rd250._rss) = _keep250[:6]
    _wt250.LARGE_N, _K250.earn_dates = _keep250[8], _keep250[9]
    _K250.JOB.clear(); _K250.JOB.update(_keep250[6])
    _K250._FAILS.clear(); _K250._FAILS.update(_keep250[7])
    for _x in (_tmp250, _tmp250 + "-wal", _tmp250 + "-shm"):
        try:
            os.remove(_x)
        except OSError:
            pass
print(f"250 Koers AI's contest rules {ok(_ok250)}  a buy cut to 20% when decided and refused at an open that passes it; four "
      f"orders refused; nothing filled until every bell rang, then all less 0.01%; the largest companies and Rick's stocks sent, "
      f"unmarked, no pin, none of Rick's headlines, no web; Friday from 23:00 while the markets are shut; no Sharpe before 20 days")

# 251 Koers AI is wired: six routes, the page and its words, its scheduler
#     from start (idle until Start is pressed), the morning brief's line.
from pf import api as _api251
_paths251 = {r.path for r in _api251.router.routes}
_tj251 = open(os.path.join(HERE, "backend", "static", "term.js"), encoding="utf-8").read()
_ap251 = open(os.path.join(HERE, "backend", "portfolio_app.py"), encoding="utf-8").read()
_mo251 = open(os.path.join(HERE, "backend", "pf", "morning.py"), encoding="utf-8").read()
_kj251 = open(os.path.join(HERE, "backend", "static", "kai.js"), encoding="utf-8").read()
_ok251 = ({"/api/pf/kai", "/api/pf/kai/job", "/api/pf/kai/decision/{did}", "/api/pf/kai/start", "/api/pf/kai/decide", "/api/pf/kai/auto"} <= _paths251
          and 'code: "KAI",  title: "Koers AI",          href: "/m/KAI",       state: "live"' in _tj251
          and '"KOERSAI", "KOERS AI", "RIVAL", "CONTEST"' in _tj251
          and '"KAI": "kai.html"' in _ap251 and "koersai.ensure()" in _ap251 and 'b["kai"]' in _mo251 and "kaimod.brief_line" in _mo251
          and '"/kai/start"' in _kj251 and "confirm(" in _kj251 and _BUILD >= 47)
print(f"251 Koers AI is wired        {ok(_ok251)}  six routes; KAI, KOERSAI, RIVAL and CONTEST open it; its scheduler starts with "
      f"the server and waits for Start; a line in the morning brief")

# ---------------------------------------------------------------------------
# Build 49: POL, politicians' trades
# ---------------------------------------------------------------------------

# 252 POL reads the user's tracker right (build 49). The tracker in
#     backend/ptt runs in its own process; here a small tracker database is
#     made with its own schema (two members, two committees, four trades, one
#     scanned filing), the real export runs on it, and the page's readers are
#     asked about it. A defence buy by an Armed Services member is in remit, the
#     same member's bank buy is not, a senator's bank buy on Banking is; the
#     Clerk's M/D/YYYY dates come out ISO; the OCR row stays apart; the
#     filings tab holds a whole year, this year's unless asked (build 50). Your
#     overlap finds a share's US listing (ASML.AS is ASML, INGA.AS is ING,
#     BRK-B is filed as BRK.B); a buy you hold, filed in the last days, is an
#     alert; the brief counts the day's filings. The nightly run follows the
#     first build only, once after each 05:30, and never beside another run;
#     a failing step stops it under its own name, and a first build that
#     stopped carries on from that step.
import subprocess as _sp252
import sys as _sys252
import json as _js252
from datetime import datetime as _dt252
from pathlib import Path as _P252
from pf import pol as _pol252
_dir252 = os.path.join(_tf181.gettempdir(), "pf_pol49check")
os.makedirs(_dir252, exist_ok=True)
_db252 = os.path.join(_dir252, "tracker.db")
for _x in (_db252, _db252 + "-wal", _db252 + "-shm"):
    if os.path.exists(_x):
        os.remove(_x)
_MAKE252 = r'''
import sys, json
sys.path.insert(0, sys.argv[2])
import db
c = db.connect(sys.argv[1])
x = c.execute
x("INSERT INTO legislators (bioguide_id, full_name, chamber, state, state_district, party) VALUES ('A000001','Alice Able','house','NY','NY01','Democrat')")
x("INSERT INTO legislators (bioguide_id, full_name, chamber, state, state_district, party) VALUES ('B000002','Bob Baker','senate','TX','TX','Republican')")
x("INSERT INTO committees (committee_id, name, chamber, parent_id, sensitivity, sectors) VALUES ('HSAS','House Committee on Armed Services','house',NULL,0.9,'defense')")
x("INSERT INTO committees (committee_id, name, chamber, parent_id, sensitivity, sectors) VALUES ('SSBK','Senate Committee on Banking','senate',NULL,0.9,'financials')")
x("INSERT INTO committee_memberships VALUES ('A000001','HSAS','Chair',1)")
x("INSERT INTO committee_memberships VALUES ('B000002','SSBK',NULL,2)")
for t, s in (("LMT", "defense"), ("JPM", "financials"), ("NVDA", "technology")):
    x("INSERT INTO ticker_meta (ticker, sectors) VALUES (?,?)", (t, s))
for d, f, n, st in (("1001", "10/30/2026", "Hon. Alice Able", "parsed"), ("sen:2002", "10/31/2026", "Bob Baker", "parsed"),
                    ("1003", "10/15/2026", "Hon. Alice Able", "no_text")):
    x("INSERT INTO filings (doc_id, year, member_name, filing_date, parse_status, pdf_url) VALUES (?,2026,?,?,?,?)", (d, n, f, st, "https://x/" + d))
rows = (("1001", 1, "A000001", "Hon. Alice Able", "LMT", "purchase", "2026-10-01", "10/30/2026", 27386.0),
        ("1001", 2, "A000001", "Hon. Alice Able", "NVDA", "sale", "2026-10-02", "10/30/2026", 3875.0),
        ("1001", 3, "A000001", "Hon. Alice Able", "JPM", "purchase", "2026-10-03", "10/30/2026", 3875.0),
        ("sen:2002", 1, "B000002", "Bob Baker", "JPM", "purchase", "2026-10-20", "10/31/2026", 27386.0))
for d, s, bg, nm, tk, ty, td, fd, mid in rows:
    x("INSERT INTO transactions (doc_id, seq, bioguide_id, member_name, ticker, tx_type, tx_date, filing_date, amount_raw, amount_mid, asset_type, owner) "
      "VALUES (?,?,?,?,?,?,?,?,'$15,001 - $50,000',?,'ST','self')", (d, s, bg, nm, tk, ty, td, fd, mid))
x("INSERT INTO trade_performance (transaction_id, entry_date, ret_12m, bench_12m) VALUES (1, '2026-10-30', 0.30, 0.10)")
x("CREATE TABLE trade_grades (transaction_id INTEGER PRIMARY KEY, score REAL, grade TEXT, label TEXT, in_remit INTEGER, cluster_size INTEGER, components TEXT)")
x("INSERT INTO trade_grades (transaction_id, score, grade) VALUES (1, 86.5, 'A')")
x("INSERT INTO politician_stats (bioguide_id, member_name, n_buys, n_scored, avg_alpha_12m, n_scored_12m) VALUES ('A000001','Alice Able',2,1,0.2,12)")
curve = {"dates": [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(280)], "strategy": [1 + i / 1000 for i in range(280)],
         "benchmark": [1 + i / 2000 for i in range(280)], "final_strategy": 1.279, "final_benchmark": 1.1395, "n_trades": 1}
db.set_meta(c, "strategy_v1", json.dumps({"built_at": "2026-11-01T05:40:00", "summary": {}, "curves": {"strategy": curve, "strategy_size": curve},
                                          "members": [{"bioguide_id": "A000001"}], "basket": {"rows": [{"ticker": "LMT", "size_weight": 1.0}]},
                                          "changes": {}, "hold_days": 365}))
db.set_meta(c, "grade_validation_v1", json.dumps({"top_minus_bottom": -0.022, "t_stat": -1.71, "predictive": False}))
x("CREATE TABLE ocr_transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT NOT NULL, page INTEGER NOT NULL, seq INTEGER NOT NULL, owner TEXT, "
  "asset_name TEXT, tx_type TEXT, tx_date TEXT, notification_date TEXT, amount_band TEXT, amount_raw TEXT, confident INTEGER NOT NULL DEFAULT 1, "
  "repaired INTEGER NOT NULL DEFAULT 0, read_at TEXT NOT NULL DEFAULT (datetime('now')))")
x("INSERT INTO ocr_transactions (doc_id, page, seq, asset_name, tx_type, tx_date, amount_raw) VALUES ('1003',1,1,'Lockheed Martin','purchase','10/01/2026','$1,001 - $15,000')")
c.commit()
c.close()
'''
_r252 = {}
_keep252 = (_pol252.DB, _pol252.now_ams, _pol252.my_tickers, _pol252.STATUS, _pol252.LOCK)
_keepS252 = (dict(_pol252.STEPS), _pol252.LOG)
_ok252 = False
try:
    _ptt252 = os.path.join(HERE, "backend", "ptt")
    _a = _sp252.run([_sys252.executable, "-c", _MAKE252, _db252, os.path.join(_ptt252, "src")], capture_output=True, text=True, timeout=120)
    _b = _sp252.run([_sys252.executable, os.path.join(_ptt252, "terminal_export.py"), _db252], capture_output=True, text=True, timeout=300,
                    cwd=_ptt252, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    _r252["export"] = (_a.returncode, _b.returncode, (_js252.loads(_b.stdout.strip().splitlines()[-1]) if _b.returncode == 0 else _b.stderr[-300:]))
    _pol252.DB = _P252(_db252)
    _pol252.STATUS = _P252(_dir252) / "pol_run.json"
    _pol252.LOCK = _P252(_dir252) / "pol_run.lock"
    _pol252._CACHE.clear()
    _clock252 = {"t": _dt252(2026, 11, 1, 10, 0, tzinfo=_pol252.AMS)}
    _pol252.now_ams = lambda: _clock252["t"]
    _pol252.my_tickers = lambda: {"LMT": {"held": "LMT", "name": "Lockheed", "how": "share"},
                                   "JPM": {"held": "JPM", "name": "JPMorgan", "how": "turbo"},
                                   "NVDA": {"held": "NVDA", "name": "NVIDIA", "how": "share"}}
    _t = {(r["doc"], r["ticker"]): r for r in _pol252.latest()}
    _r252["remit"] = (_t[("1001", "LMT")]["remit"], _t[("1001", "JPM")]["remit"], _t[("sen:2002", "JPM")]["remit"])
    _r252["rows"] = ([(r["doc"], r["ticker"], r["filed"]) for r in _pol252.latest()], round(_t[("1001", "LMT")]["alpha"]["12m"], 6),
                     _t[("1001", "LMT")]["grade"], _t[("sen:2002", "JPM")]["chamber"], _t[("sen:2002", "JPM")]["party"],
                     _t[("1001", "LMT")]["lag"])
    _fy252 = _pol252.filings(2026)
    _r252["year"] = (_pol252.filing_years(), _fy252["year"], len(_fy252["trades"]), _fy252["trades"][0]["doc"],
                     _pol252.filings(2025)["trades"], _pol252.filings()["year"])
    _mine = _pol252.mine()
    _r252["mine"] =sorted((b["ticker"], b["buys"], b["sells"], b["how"]) for b in _mine["by"])
    _r252["names"] = (_pol252._us_names("ASML.AS", {"ASML.AS": {"also": ["ASML", "ASME.DE", "0M42.L"]}}, {}),
                      _pol252._us_names("INGA.AS", {}, {"INGA.AS": "ING"}), _pol252._us_names("BRK-B", {}, {}))
    _m = _pol252.member("A000001")
    _r252["member"] = (_m["name"], _m["party"], _m["tracked"], [s["committee_id"] for s in _m["committees"]], _m["remit"],
                       sorted((s["sector"], s["in_remit"]) for s in _m["sectors"]), len(_m["trades"]))
    _c = _pol252.committee("HSAS")
    _r252["committee"] = ([x["bg"] for x in _c["members"]], [(t["ticker"], t["member"]) for t in _c["trades"]])
    _r252["leaders"] = [x["bg"] for x in _pol252.leaders()]
    _s = _pol252.strategy()
    _r252["strategy"] = (len(_s["equal"]["d"]) <= 521, _s["equal"]["d"][-1] == "2026-10-28", _s["basket"][0]["ticker"], _s["n_members"])
    _r252["ocr"] = [(o["doc_id"], o["member"], o["filed"], o["asset_name"]) for o in _pol252.ocr_rows()]
    _r252["alerts"] = [a["text"] for a in _pol252.alerts()]
    _r252["brief"] = _pol252.brief_line()
    _r252["iso"] = (_pol252._iso("8/10/2026"), _pol252._iso("2026-08-10"), _pol252._iso("junk"))
    # the nightly run: only after a first build, once after each 05:30, never beside a live run
    _pol252._save({"done": {"first": "2026-11-01T05:40:00+01:00"}})
    _nd = _pol252.nightly_due
    _r252["due"] = (_nd(_dt252(2026, 11, 2, 5, 20, tzinfo=_pol252.AMS)), _nd(_dt252(2026, 11, 2, 5, 31, tzinfo=_pol252.AMS)))
    _pol252._save({})
    _r252["due0"] = _nd(_dt252(2026, 11, 2, 5, 31, tzinfo=_pol252.AMS))
    _pol252.LOCK.write_text(_js252.dumps({"pid": 999999991, "child": None}), encoding="utf-8")
    _dead = _pol252.busy()
    _pol252._lock("nightly")
    _live = _pol252.busy()
    _pol252.LOCK.unlink()
    _r252["lock"] = (_dead is None, bool(_live and _live["kind"] == "nightly"))
    # A failing step stops the run under its own name: on 4 November a command (a list) was
    # tested against the set of best-effort steps, raised "unhashable type: 'list'" and hid
    # that the market caps step had exited 1. And a first build resumes where it stopped.
    _pol252.LOG = _P252(_dir252) / "pol_run.log"
    _mark252 = os.path.join(_dir252, "ran.txt")
    _one252 = ["-c", "open(%r, 'a').write('1')" % _mark252]
    _pol252.STEPS["first"] = [("one", _one252, False), ("two", ["-c", "import sys; sys.exit(3)"], False), ("three", ["-c", "pass"], False)]
    _pol252._save({})
    _run1 = _pol252.run("first")
    _pol252.STEPS["first"] = [("one", _one252, False), ("two", ["-c", "pass"], False), ("three", ["-c", "pass"], False)]
    _run2 = _pol252.run("first")
    _r252["steps"] = (_run1["state"], _run1["error"], _run2["state"], open(_mark252).read(), "first_steps" in _pol252.status(),
                      bool((_pol252.status().get("done") or {}).get("first")))
    _ok252 = (_r252["export"][:2] == (0, 0) and _r252["export"][2]["trades"] == 4 and _r252["export"][2]["ocr"] == 1
              and _r252["remit"] == (["defense"], [], ["financials"])
              and _r252["rows"][0][0] == ("sen:2002", "JPM", "2026-10-31") and _r252["rows"][1:] == (0.2, "A", "senate", "R", 29)
              and _r252["year"] == ({"2026": 4}, 2026, 4, "sen:2002", [], 2026)
              and _r252["mine"] == [("JPM", 2, 0, "turbo"), ("LMT", 1, 0, "share"), ("NVDA", 0, 1, "share")]
              and _r252["names"] == ({"ASML"}, {"ING"}, {"BRK.B"})
              and _r252["member"] == ("Alice Able", "D", True, ["HSAS"], ["defense"], [("defense", True), ("financials", False)], 3)
              and _r252["committee"] == (["A000001"], [("LMT", "Alice Able")]) and _r252["leaders"] == ["A000001"]
              and _r252["strategy"] == (True, True, "LMT", 1)
              and _r252["ocr"] == [("1003", "Alice Able", "2026-10-15", "Lockheed Martin")]
              and _r252["alerts"] == ["Bob Baker (R-TX) bought JPM", "Alice Able (D-NY) bought JPM", "Alice Able (D-NY) bought LMT"]
              and _r252["brief"] == "Congress: 1 trade disclosed since yesterday; in your holdings: Bob Baker bought JPM"
              and _r252["iso"] == ("2026-08-10", "2026-08-10", None) and _r252["due"] == (False, True) and _r252["due0"] is False
              and _r252["lock"] == (True, True)
              and _r252["steps"] == ("error", "RuntimeError: two: exit 3 (the log is ptt/data/pol_run.log)", "done", "1", False, True))
except Exception as _e252:                 # a broken reader fails this check, not the whole of verify.py
    _r252["error"] = f"{_e252.__class__.__name__}: {_e252}"[:200]
finally:
    (_pol252.DB, _pol252.now_ams, _pol252.my_tickers, _pol252.STATUS, _pol252.LOCK) = _keep252
    _pol252.STEPS.clear()
    _pol252.STEPS.update(_keepS252[0])
    _pol252.LOG = _keepS252[1]
    _pol252.JOB.clear()
    _pol252.JOB.update(state="idle")
    _pol252._CACHE.clear()
    import shutil as _sh252
    _sh252.rmtree(_dir252, ignore_errors=True)
print(f"252 POL reads the tracker    {ok(_ok252)}  an Armed Services chair's defence buy in remit, her bank buy not, a Banking "
      f"senator's is; ISO dates, the OCR row apart; your overlap through ASML.AS, INGA.AS and BRK-B; three alerts, the brief's "
      f"line; the nightly run only after a first build, once after 05:30, never beside another")

# 253 POL is wired: seven routes (the year of filings since build 50), the page and its words, the stock page's
#     Congress panel, its nightly run from start, the alerts and the brief,
#     the tracker's code beside the terminal and its data kept out of git.
from pf import api as _api253
_paths253 = {r.path for r in _api253.router.routes}
_rd253 = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
_tj253, _ap253, _mo253, _sv253 = _rd253("backend", "static", "term.js"), _rd253("backend", "portfolio_app.py"), _rd253("backend", "pf", "morning.py"), _rd253("backend", "pf", "service.py")
_tk253, _gi253, _rq253 = _rd253("backend", "static", "ticker.js"), _rd253(".gitignore"), _rd253("backend", "requirements-portfolio.txt")
_ok253 = ({"/api/pf/pol", "/api/pf/pol/job", "/api/pf/pol/member/{bg}", "/api/pf/pol/committee/{cid}", "/api/pf/pol/ticker/{sym}",
           "/api/pf/pol/build", "/api/pf/pol/filings"} <= _paths253
          and "/pol/filings?year=" in _rd253("backend", "static", "pol.js")
          and 'code: "POL",  title: "Politicians\' trades"' in _tj253 and '"CONGRESS", "CONG", "POLITICIANS"' in _tj253
          and '"POL": "pol.html"' in _ap253 and "pol.ensure()" in _ap253 and 'b["pol"]' in _mo253 and "pol.alerts()" in _sv253
          and '["s-pol", "Congress"]' in _tk253 and "async function loadPol()" in _tk253
          and os.path.exists(os.path.join(HERE, "backend", "ptt", "src", "ptr_parser.py"))
          and os.path.exists(os.path.join(HERE, "backend", "ptt", "src", "winsdk", "windows", "media", "ocr.py"))
          and "backend/ptt/data/" in _gi253 and "pdfplumber==" in _rq253 and "winrt-Windows.Media.Ocr==" in _rq253 and _BUILD >= 49)
print(f"253 POL is wired             {ok(_ok253)}  seven routes; POL, CONGRESS and SENATE open it; a Congress panel on every stock page; "
      f"its nightly run from start; alerts and a brief line; the tracker in backend/ptt, its data out of git")

# 254 POL's Yahoo steps tell a refusal from an answer (4 November). yfinance
#     does not raise when Yahoo refuses it: under a rate-limited crumb every
#     lookup is a 401 that comes back as an empty answer, and the tracker
#     stored that as "no sector" for all 2,104 tickers and "no market cap" for
#     453. The in-remit strategy came out empty, and its CLI fell over
#     printing it. Here, with a fake Yahoo in the tracker's own process: a
#     refused session stores nothing, stops after 25 tries and the canary, and
#     the step exits 75; unknown symbols are still stored once the canary
#     answers; a ticker the market caps already know costs no request; an
#     empty strategy prints "no trades"; and the runner waits out a refusal
#     and carries on, or stops saying Yahoo kept refusing.
_T254 = r"""
import sys, json, io, argparse, contextlib
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import types
import db, market, sectors, cli
class _Tk:                                   # what yfinance hands back: an answer, a refusal (all empty), an error
    def __init__(self, s):
        self.s = s
    @property
    def info(self):
        if self.s == "BAD":
            raise RuntimeError("no network")
        return {"quoteType": "EQUITY", "sector": "Industrials"} if self.s == "LMT" else {"trailingPegRatio": None}
sys.modules["yfinance"] = types.SimpleNamespace(Ticker=_Tk)
info = (market.yahoo_info("LMT") is not None, market.yahoo_info("TWTR"), market.yahoo_info("BAD"))
calls, st = [], {"refuse": True}
KNOWN = {"LMT": ("Industrials", "Aerospace & Defense"), "JPM": ("Financial Services", "Banks - Diversified"),
         market.CANARY: ("Technology", "Consumer Electronics")}
def fake(sym):
    calls.append(sym)
    if st["refuse"] or sym not in KNOWN:
        return None
    return {"quoteType": "EQUITY", "sector": KNOWN[sym][0], "industry": KNOWN[sym][1], "marketCap": 1e11}
market.yahoo_info = fake
c = db.connect(sys.argv[1])
c.execute("INSERT INTO filings (doc_id, year, member_name, filing_date, parse_status, pdf_url) VALUES ('9',2026,'Hon. A','10/30/2026','parsed','x')")
tick = ["JPM", "LMT", "NVDA"] + [f"Q{i:02d}" for i in range(60)]
for n, t in enumerate(tick, 1):
    c.execute("INSERT INTO transactions (doc_id, seq, member_name, ticker, tx_type, tx_date, filing_date, amount_raw, amount_mid, asset_type, owner) "
              "VALUES ('9',?,'Hon. A',?,'purchase','2026-10-01','10/30/2026','$1,001 - $15,000',8000.5,'ST','self')", (n, t))
market.sync_fundamentals(c, tickers=[], progress=False)
c.execute("INSERT INTO market_meta (ticker, fetched_at, payload) VALUES ('NVDA','2026-11-04T12:00:00',?)",
          (json.dumps({"sector": "Technology", "industry": "Semiconductors", "marketCap": 4e12}),))
c.commit()
out = {"info": info}
s1 = sectors.sync_sectors(c, progress=False, delay=0)
out["refused_sectors"] = (s1["stopped_early"], s1["left"], s1["cached"], c.execute("SELECT COUNT(*) FROM ticker_meta").fetchone()[0], len(calls))
calls.clear()
f1 = market.sync_fundamentals(c, progress=False, pause=0)
out["refused_caps"] = (f1["stopped_early"], f1["failed"], c.execute("SELECT COUNT(*) FROM market_meta").fetchone()[0], len(calls))
st["refuse"] = False
calls.clear()
s2 = sectors.sync_sectors(c, progress=False, delay=0)
rows = dict(c.execute("SELECT ticker, sectors FROM ticker_meta").fetchall())
out["working"] = (s2["stopped_early"], len(rows), rows.get("LMT"), rows.get("NVDA"), rows.get("Q59"), len(calls), calls.count(market.CANARY))
c.execute("INSERT INTO transactions (doc_id, seq, member_name, ticker, tx_type, tx_date, filing_date, amount_raw, amount_mid, asset_type, owner) "
          "VALUES ('9',99,'Hon. A','NEW1','purchase','2026-10-01','10/30/2026','$1,001 - $15,000',8000.5,'ST','self')")
c.commit()
c.close()
cli.DB_PATH = Path(sys.argv[1])
st["refuse"] = True
code = None
with contextlib.redirect_stdout(io.StringIO()):
    try:
        cli.cmd_sectors(argparse.Namespace(limit=None))
    except SystemExit as e:
        code = e.code
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cli.cmd_strategy(argparse.Namespace(min_member_trades=None))
out["cli"] = (code, "HEADLINE (equal-weight)    no trades" in buf.getvalue())
print(json.dumps(out))
"""
import subprocess as _sp254
import sys as _sys254
import json as _js254
import tempfile as _tf254
from pathlib import Path as _P254
from pf import pol as _pol254
_dir254 = _tf254.mkdtemp(prefix="pf_pol254")
_r254 = {}
_ok254 = False
_keep254 = (_pol254.STATUS, _pol254.LOCK, _pol254.LOG, _pol254.scan_busy, _pol254.REFUSED_WAIT_S, _pol254.REFUSED_TRIES,
            list(_pol254.STEPS["first"]))
try:
    _a254 = _sp254.run([_sys254.executable, "-c", _T254, os.path.join(_dir254, "t.db"), os.path.join(HERE, "backend", "ptt", "src")],
                       capture_output=True, text=True, timeout=300, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    _r254["tracker"] = _js254.loads(_a254.stdout.strip().splitlines()[-1]) if _a254.returncode == 0 else _a254.stderr[-400:]
    # the runner: a Yahoo step that exits 75 is asked again after a wait, and one that keeps refusing stops the run by name
    _pol254.STATUS = _P254(_dir254) / "pol_run.json"
    _pol254.LOCK = _P254(_dir254) / "pol_run.lock"
    _pol254.LOG = _P254(_dir254) / "pol_run.log"
    _pol254.scan_busy = lambda: False
    _pol254.REFUSED_WAIT_S, _pol254.REFUSED_TRIES = 0, 2
    _n254 = os.path.join(_dir254, "n.txt")
    _once254 = ["-c", "import os, sys; p = %r; n = len(open(p).read()) if os.path.exists(p) else 0; open(p, 'a').write('1'); "
                      "sys.exit(75 if n == 0 else 0)" % _n254]
    _pol254.STEPS["first"] = [("caps", _once254, True), ("after", ["-c", "pass"], False)]
    _pol254._save({})
    _g1 = _pol254.run("first")
    _t1 = open(_n254).read()
    os.remove(_n254)
    _always254 = ["-c", "import sys; open(%r, 'a').write('1'); sys.exit(75)" % _n254]
    _pol254.STEPS["first"] = [("caps", _always254, True), ("after", ["-c", "pass"], False)]
    _pol254._save({})
    _g2 = _pol254.run("first")
    _r254["runner"] = (_g1["state"], _t1, _g2["state"], _g2["error"], open(_n254).read())
    _ok254 = (isinstance(_r254["tracker"], dict) and _r254["tracker"]["info"] == [True, None, None]
              and _r254["tracker"]["refused_sectors"] == [True, 62, 1, 1, 26]
              and _r254["tracker"]["refused_caps"] == [True, 62, 1, 26]
              and _r254["tracker"]["working"] == [False, 63, "aerospace,defense,industrials", "technology", "", 65, 3]
              and _r254["tracker"]["cli"] == [75, True]
              and _r254["runner"] == ("done", "11", "error", "RuntimeError: caps: Yahoo kept refusing requests; press Build later, "
                                      "it carries on from here", "111"))
except Exception as _e254:
    _r254["error"] = f"{_e254.__class__.__name__}: {_e254}"[:200]
finally:
    (_pol254.STATUS, _pol254.LOCK, _pol254.LOG, _pol254.scan_busy, _pol254.REFUSED_WAIT_S, _pol254.REFUSED_TRIES) = _keep254[:6]
    _pol254.STEPS["first"] = _keep254[6]
    _pol254.JOB.clear()
    _pol254.JOB.update(state="idle")
    import shutil as _sh254
    _sh254.rmtree(_dir254, ignore_errors=True)
print(f"254 POL tells a refusal      {ok(_ok254)}  a refused Yahoo stores no sector and no market cap, stops after 25 and the "
      f"canary, exits 75; unknown symbols kept; known caps cost no request; an empty strategy prints; the runner waits it out")
if not _ok254:
    print("     ", _r254)

# 255 FLOW reads its sources: the WSJ's diary (its own timestamp dates the session, 16:00 or later is the final
#     count, one without NYSE volume is refused), Cboe's volumes, FINRA's file (BRK/B is BRK.B, the market summed
#     over every row), SqueezeMetrics, unicorn's archive (the zeros after February 2020 dropped), the OFR (nulls and
#     series it did not ask for dropped), the CFTC (a row with no open interest skipped, the weeks in order)
from pf import flow as _fl255
from datetime import date as _d255, timedelta as _td255
_r255 = {}
_ok255 = False
try:
    def _dj255(ts, upv="1,278,316,775"):
        return {"data": {"timestamp": ts, "instrumentSets": [
            {"headerFields": [{"value": "name", "label": "Issues"}], "instruments": [
                {"name": "Advancing", "NASDAQ": "2,723", "NYSE": "1,432"}, {"name": "Declining", "NASDAQ": "1,897", "NYSE": "1,250"},
                {"name": "Unchanged", "NASDAQ": "116", "NYSE": "48"}]},
            {"headerFields": [{"value": "name", "label": "Issues At"}], "instruments": [
                {"name": "New Highs", "NASDAQ": "43", "NYSE": "18"}, {"name": "New Lows", "NASDAQ": "330", "NYSE": "108"}]},
            {"headerFields": [{"value": "name", "label": "Share Volume"}], "instruments": [
                {"name": "Advancing", "NASDAQ": "4,262,938,056", "NYSE": upv},
                {"name": "Declining", "NASDAQ": "2,333,345,992", "NYSE": "1,201,632,024"},
                {"name": "Unchanged", "NASDAQ": "12,927,227", "NYSE": "9,977,145"}]}]}}
    _a, _b, _c = (_fl255.parse_diary(_dj255("12:55 PM EST 11/06/26")), _fl255.parse_diary(_dj255("4:00 PM EST 11/06/26")),
                  _fl255.parse_diary(_dj255("12:05 AM EST 11/09/26")))
    _r255["diary"] = (_a["d"], _a["asof"], _a["final"], _b["final"], _c["asof"], _a["NYSE"]["upv"], _a["NYSE"]["dec"], _a["NASDAQ"]["nl"])
    try:
        _fl255.parse_diary(_dj255("4:00 PM EST 11/06/26", upv=""))
        _r255["diary_empty"] = "accepted"
    except ValueError:
        _r255["diary_empty"] = "refused"
    _cb = _fl255.parse_cboe({"SUM OF ALL PRODUCTS": [{"name": "VOLUME", "call": 10589764, "put": 11225416},
                                                     {"name": "OPEN INTEREST", "call": 1, "put": 2}],
                             "EQUITY OPTIONS": [{"name": "VOLUME", "call": 2719490, "put": 2430016}]})
    _r255["cboe"] = (_cb["tot_p"], _cb["tot_c"], _cb["eq_p"], _cb.get("spx_p"))
    _rows, _mk = _fl255.parse_finra("Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
                                    "20261105|BRK/B|1740937.185017|5420|3849574.479698|B,Q,N\n20261105|AAPL|100|0|400|Q\n"
                                    "20261105|ZZZ|50|0|100|Q\n12366\n", {"BRK.B", "AAPL"})
    _r255["finra"] = (sorted(_rows), round(_rows["BRK.B"][0]), round(_mk[0], 1), round(_mk[1], 1))
    _dx = _fl255.parse_dix("date,price,dix,gex\n" + "".join(f"{_d255(2020, 1, 1) + _td255(days=i)},100,0.4,1e9\n" for i in range(600)))
    try:
        _fl255.parse_dix("date,price,dix,gex\n2020-01-01,1,0.4,1\n")
        _short255 = "accepted"
    except ValueError:
        _short255 = "refused"
    _r255["dix"] = (len(_dx), _dx[0][0], _dx[0][2], _short255)
    _r255["unicorn"] = _fl255.parse_unicorn("19650301, 2750\n20200210, 1840000000\n20200211, 0\njunk\n")
    _r255["ofr"] = _fl255.parse_ofr({"FPF-ALLQHF_GNE_SUM": {"timeseries": {"aggregation": [["2026-03-31", 4.2e13], ["2026-06-30", None]]}},
                                     "FPF-ALLQHF_NAV_SUM": {"timeseries": {"aggregation": [["2026-03-31", 5e12]]}},
                                     "SOMETHING-ELSE": {"timeseries": {"aggregation": [["2026-03-31", 1]]}}})
    _cf = _fl255.parse_cftc([
        {"report_date_as_yyyy_mm_dd": "2026-11-03T00:00:00.000", "cftc_contract_market_code": "13874A", "open_interest_all": "2000",
         "lev_money_positions_long": "300", "lev_money_positions_short": "700"},
        {"report_date_as_yyyy_mm_dd": "2026-10-27T00:00:00.000", "cftc_contract_market_code": "13874A", "open_interest_all": "2100",
         "lev_money_positions_long": "310", "lev_money_positions_short": "690"},
        {"report_date_as_yyyy_mm_dd": "2026-10-27T00:00:00.000", "cftc_contract_market_code": "X", "open_interest_all": "0"}])
    _r255["cftc"] = (sorted(_cf), [w["d"] for w in _cf["13874A"]], _cf["13874A"][-1]["lf_s"], _cf["13874A"][-1]["am_l"])
    _ok255 = (_r255["diary"] == ("2026-11-06", "2026-11-06 12:55", False, True, "2026-11-09 00:05", 1278316775, 1250, 330)
              and _r255["diary_empty"] == "refused" and _r255["cboe"] == (11225416.0, 10589764.0, 2430016.0, None)
              and _r255["finra"] == (["AAPL", "BRK.B"], 1740937, 1741087.2, 3850074.5)
              and _r255["dix"] == (600, "2020-01-01", 0.4, "refused")
              and _r255["unicorn"] == {"1965-03-01": 2750.0, "2020-02-10": 1840000000.0}
              and _r255["ofr"] == {"gne": [["2026-03-31", 4.2e13]], "nav": [["2026-03-31", 5e12]]}
              and _r255["cftc"] == (["13874A"], ["2026-10-27", "2026-11-03"], 700.0, 0.0))
except Exception as _e255:
    _r255["error"] = f"{_e255.__class__.__name__}: {_e255}"[:200]
print(f"255 FLOW reads its sources   {ok(_ok255)}  the WSJ diary dated by its own clock, final from 16:00, empty refused; Cboe's "
      f"volumes; FINRA's BRK/B as BRK.B and the whole market summed; DIX, unicorn's zeros, the OFR's nulls, the CFTC's weeks")
if not _ok255:
    print("     ", _r255)

# 256 FLOW's rules: Lowry's day (90% of the volume AND the points; by volume alone where the points are unknown;
#     80% by volume), a panic and its answer (two 80% up days within five sessions, or a 90% up day, within the
#     window; one never answered has no session count; a down day after the window starts anew), Zweig's thrust,
#     the NYSE study, and the members' up and down volume from the IDX download (a session with too few bars dropped)
_r256 = {}
_ok256 = False
_keep256 = None
try:
    _k = _fl255.day_kind
    _r256["kind"] = (_k(9, 91), _k(9, 91, 7, 93), _k(9, 91, 15, 85), _k(95, 5, 92, 8), _k(85, 15), _k(50, 50), _k(0, 0))
    _dd = [f"d{i:02d}" for i in range(30)]
    _ks = [None] * 30
    for _i, _v in ((2, "90down"), (3, "90down"), (6, "80up"), (9, "80up"), (12, "90down"), (14, "80up"), (21, "80up"), (22, "90up"), (25, "90down")):
        _ks[_i] = _v
    _ep = _fl255.episodes(_dd, _ks)
    _r256["episodes"] = [(e["start"], e["last"], e["downs"], e["answered"], e["how"], e["sessions"], e["open"]) for e in _ep]
    _ep2 = _fl255.episodes([f"e{i}" for i in range(8)], ["90down", None, None, None, None, "90up", "90down", None], window=3)
    _r256["window"] = [(e["start"], e["answered"], e["open"], e["sessions"]) for e in _ep2]
    _adv = [50] * 25 + [10] * 8 + [95] * 12
    _th, _em = _fl255.zweig([f"z{i:02d}" for i in range(45)], _adv, [100 - a for a in _adv])
    _r256["zweig"] = [(t["d"], t["low_d"], t["low"], t["ema"]) for t in _th]
    # the NYSE study: three panics, each answered by a 90% up day three sessions later, on an index rising 0.01% a day
    _nd = [(_d255(1990, 1, 1) + _td255(days=i)).isoformat() for i in range(2600)]
    _uv, _dv = {x: 50.0 for x in _nd}, {x: 50.0 for x in _nd}
    for _p in (800, 1200, 1600):
        _uv[_nd[_p]], _dv[_nd[_p]] = 5.0, 95.0
        _uv[_nd[_p + 3]], _dv[_nd[_p + 3]] = 95.0, 5.0
    _st = _fl255.nyse_study(_uv, _dv, {x: 100 * 1.0001 ** i for i, x in enumerate(_nd)})
    _r256["study"] = (_st["n_days"], _st["episodes"], _st["answered_share"], _st["median_wait"], _st["stats"]["down"]["21"],
                      _st["stats"]["up"]["21"]["n"], "zweig" in _st["stats"], _st["thrusts"], _fl255.nyse_study({}, {}, {})["available"])
    # the members' up and down volume
    import pandas as _pd256
    from pf import idx as _ix256, watch as _wt256
    _ix = _pd256.to_datetime(["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"])
    _cl = _pd256.DataFrame({"A": [10, 11, 12, 11, 12], "B": [10, 9, 9, 8, 9], "C": [10, 9.5, 9, 9, 9.5], "D": [10, 10, 10.5, 11, 12],
                            "^X": [100, 101, 102, 101, 103]}, index=_ix, dtype=float)
    _vo = _pd256.DataFrame({"A": [1, 100, 5, 7, 9], "B": [1, 50, 5, 8, 10], "C": [1, 50, 5, 9, 11], "D": [1, 999, None, 6, 12],
                            "^X": [0, 0, 0, 0, 0]}, index=_ix, dtype=float)
    _u = _ix256.updown_series(_cl, _vo, "^X")
    _r256["updown"] = (_u["d"], _u["adv"], _u["dec"], _u["upv"], _u["dnv"], _u["ptu"], _u["ptd"], _u["n"], _ix256.updown_series(_cl, None, "^X"))
    # watch.each_series hands the volumes over when asked, from the same download
    _keep256 = _wt256._yf

    class _Yf256:
        @staticmethod
        def download(part, **kw):
            cols = _pd256.MultiIndex.from_tuples([("Close", "A"), ("Close", "B"), ("Volume", "A"), ("Volume", "B")])
            return _pd256.DataFrame([[10.0, 20.0, 100.0, 200.0], [11.0, None, 110.0, None]], index=_ix[:2], columns=cols)
    _wt256._yf = lambda: _Yf256
    _vs = {}
    _got = [(s, d, c) for s, d, c in _wt256.each_series(["A", "B"], volumes=_vs)]
    _r256["each"] = (_got, _vs, [s for s, _, _ in _wt256.each_series(["A", "B"])])
    _ok256 = (_r256["kind"] == ("90down", "90down", "80down", "90up", "80up", None, None)
              and _r256["episodes"] == [("d02", "d03", 2, "d09", "two 80% up days", 6, False),
                                        ("d12", "d12", 1, "d22", "a 90% up day", 10, False),
                                        ("d25", "d25", 1, None, None, 4, True)]
              and _r256["window"] == [("e0", None, False, None), ("e6", None, True, 1)]
              and _r256["zweig"] == [("z37", "z27", 18.0, 66.8)]
              and _r256["study"] == ({"90down": 3, "90up": 3, "80down": 0, "80up": 0}, 3, 100.0, 3,
                                     {"n": 3, "median": 0.21, "mean": 0.21, "pos": 100.0}, 3, False, [], False)
              and _r256["updown"] == (["2026-11-03", "2026-11-05", "2026-11-06"], [1, 1, 4], [2, 2, 0], [100, 6, 42], [100, 15, 0],
                                      [1.0, 0.5, 3.5], [1.5, 2.0, 0.0], [4, 4, 4], None)
              and _r256["each"] == ([("A", ["2026-11-02", "2026-11-03"], [10.0, 11.0]), ("B", ["2026-11-02"], [20.0])],
                                    {"A": [100.0, 110.0], "B": [200.0]}, ["A", "B"]))
except Exception as _e256:
    _r256["error"] = f"{_e256.__class__.__name__}: {_e256}"[:200]
finally:
    if _keep256 is not None:
        _wt256._yf = _keep256
print(f"256 FLOW's rules             {ok(_ok256)}  a 90% day needs the points too; two 80% up days or a 90% up day answer a panic "
      f"in time; Zweig from 18% to 67% in five sessions; the NYSE study; the members' up and down volume, a thin session dropped")
if not _ok256:
    print("     ", _r256)

# 257 FLOW's figures: hedge funds de-grossing (a four-week cut of 25%, the sharpest in the record, at today's index
#     level), Form PF a quarter and a year back, DIX with yesterday's five-year low, put/call against its year, a share's
#     short volume against its own twenty sessions, the two fear & greed parts, the alerts and the brief, NEWS's tag,
#     when the diary is read and that a final count is never overwritten, the sessions (holidays out), Cboe's back
#     files (a recent refusal asked again, an old one recorded, three refusals in a row stop the step)
import tempfile as _tf257
import shutil as _sh257
from datetime import datetime as _dt257
_r257 = {}
_ok257 = False
_dir257 = _tf257.mkdtemp(prefix="pf_flow257")
_keep257 = (store.DB_PATH, _fl255.digest, _fl255.now_ny, _fl255.net.fetch, _fl255.time.sleep, _fl255.mine_syms)
try:
    _today = _d255.today()
    _tue = _today - _td255(days=(_today.weekday() - 1) % 7)
    _wk = [(_tue - _td255(weeks=69 - i)).isoformat() for i in range(70)]
    _g = [2000] * 66 + [1900, 1800, 1700, 1500]
    _by = {"13874A": [{"d": d, "oi": 10000.0, "lf_l": g / 2, "lf_s": g / 2, "lf_sp": 0, "am_l": 3000.0, "am_s": 1000.0, "dl_l": 0, "dl_s": 0}
                      for d, g in zip(_wk, _g)],
           "209742": [{"d": d, "oi": 1.0, "lf_l": 1, "lf_s": 1, "lf_sp": 0, "am_l": 0, "am_s": 0, "dl_l": 0, "dl_s": 0} for d in _wk[-30:]]}
    _pos = _fl255.positioning(_by, {"^GSPC": 4000.0})
    _e = _pos["equity"]
    _r257["pos"] = ([r["short"] for r in _pos["rows"]], _e["chg4"], _e["chg4_rank"], _e["chg4_since"], _e["degrossing"], _e["gross_now"],
                    _e["peak52"], _pos["rows"][0]["lf_gross_4w"], _pos["rows"][0]["am_net_pct"], _pos["asof"] == _tue.isoformat())
    _lv = _fl255.leverage({"gne": [["2025-06-30", 40], ["2025-12-31", 42], ["2026-03-31", 44], ["2026-06-30", 45]],
                           "nav": [["2025-06-30", 5], ["2025-12-31", 5], ["2026-03-31", 5], ["2026-06-30", 5]],
                           "eq_l": [["2026-06-30", 6]], "eq_s": [["2026-06-30", 4]]})
    _r257["lev"] = ({k: _lv["agg"][k] for k in ("d", "last", "prev", "yago", "max", "rank")}, _lv["eq_ratio"]["last"], _lv["lev10"])
    _dd = [(_today - _td255(days=29 - i)).isoformat() for i in range(30)]
    _dk = _fl255.dark([(d, 100.0, 0.45 if i < 28 else (0.38 if i == 28 else 0.40), 1e9) for i, d in enumerate(_dd)])
    _r257["dark"] = (_dk["dix"], _dk["dix_rank"], _dk["low5"], _dk["low5_d"] == _dd[28], _dk["dix_low_since"] == _dd[28], _dk["gex"])
    _ins = _fl255.insights({"dark": _dk})
    _r257["dark_text"] = _ins[0]["text"] if _ins else None
    _pcr = [{"d": f"2026-10-{10 + i:02d}", "tot_p": 100.0 + 10 * i, "tot_c": 100.0, "eq_p": 50.0, "eq_c": 100.0} for i in range(10)]
    _pc = _fl255.putcall(_pcr)
    _r257["pc"] = (_pc["last"]["tot"], _pc["avg5"]["tot"], _pc["rank5"]["tot"], _pc["avg5"]["eq"], _pc["last"]["spx"], _fl255.putcall(_pcr[:5])["available"])
    _sr = [(f"2026-10-{1 + i:02d}", (39.0 if i % 2 else 41.0), 100.0) for i in range(24)] + [("2026-10-25", 60.0, 100.0)]
    _ss = _fl255.short_stats(_sr, {"2026-10-25": 45.0})
    _r257["short"] = (_ss["ratio"], _ss["avg20"], _ss["z"], _ss["mkt"], _ss["n"], _fl255.short_stats([], {}))
    # the two fear & greed parts, the alerts and the brief, from a digest set by hand
    _d1, _d2 = (_today - _td255(days=1)).isoformat(), _today.isoformat()
    _dig = {"breadth": {"available": True, "partial": False, "d": [_d1, _d2], "share": [50.0] * 299 + [9.3], "kind": [None, "90down"],
                        "events": [{"d": _d2, "kind": "90down", "share": 9.3, "adv": 73, "dec": 430}],
                        "episode": {"start": _d2, "last": _d2, "downs": 1, "answered": None, "how": None, "open": True, "sessions": 0}, "zweig": []},
            "equity": _e, "pos_asof": _tue.isoformat(), "dark": _dk,
            "pc": {"available": True, "n": 64, "rank5": {"tot": 95.0}, "avg5": {"tot": 0.9}}}
    _fl255.digest = lambda: _dig
    _fg = _fl255.fg_components()
    _r257["fg"] = ([c["name"] for c in _fg], _fg[0]["score"], 0 <= _fg[1]["score"] <= 100)
    from pf import sentiment as _se257
    _h1, _h0 = _se257.house_fear_greed({}, {}, _fg), _se257.house_fear_greed({}, {}, [])
    _r257["house"] = (_h1["n"], _h1["missing"], _h0["missing"], "not yet" in _h0["note"])
    _r257["alerts"] = [a["text"] for a in _fl255.alerts()]
    _r257["brief"] = _fl255.brief_line()
    _r257["day"] = (_fl255._day("2001-11-05"), _fl255._ord(1), _fl255._ord(12), _fl255._ord(23))
    from pf import news as _nw257
    _it = lambda h: {"headline": h, "summary": "", "tickers": [], "source": "x", "ts": None}
    _s1, _s0 = _nw257.score(_it("Hedge funds de-gross at the fastest pace since 2020, Goldman's prime book shows"), {}), _nw257.score(_it("Apple opens a store"), {})
    _r257["news"] = ("positioning" in _s1["why"], _s1["flow"], "positioning" in _s0["why"], _s0["flow"])
    # the diary, the sessions and Cboe's back files, on a database of their own
    store.DB_PATH = os.path.join(_dir257, "f.db")
    store.init_db()
    _NY = _fl255.NY
    _at = lambda *a: (lambda: _dt257(*a, tzinfo=_NY))
    _r257["due"] = (_fl255.diary_due(_dt257(2026, 11, 6, 17, 0, tzinfo=_NY)), _fl255.diary_due(_dt257(2026, 11, 6, 15, 0, tzinfo=_NY)),
                    _fl255.diary_due(_dt257(2026, 11, 7, 10, 0, tzinfo=_NY)), _fl255.diary_due(_dt257(2026, 11, 9, 8, 0, tzinfo=_NY)))
    _p1 = _fl255.parse_diary(_dj255("4:01 PM EST 11/06/26"))
    _fl255._diary_save(_p1)
    _fl255._diary_save(_fl255.parse_diary(_dj255("1:00 PM EST 11/06/26", upv="5")))
    _r257["diary"] = (_fl255.diary_due(_dt257(2026, 11, 7, 10, 0, tzinfo=_NY)),
                      sorted((r["d"], r["exch"], r["upv"], r["final"]) for r in _fl255.diary_rows(days=100000)))
    _r257["sessions"] = (_fl255.sessions(_d255(2026, 11, 25), _d255(2026, 11, 30)), _fl255.sessions(_d255(2025, 12, 24), _d255(2025, 12, 26)))
    _seen257 = []

    def _fetch257(url, headers=None, timeout=0):
        d = url.rsplit("/", 1)[1][:10]
        _seen257.append(d)
        if d in REFUSE257:
            return {"ok": False, "error": "HTTPError: HTTP Error 403: Forbidden", "body": b""}
        return {"ok": True, "body": json.dumps({"SUM OF ALL PRODUCTS": [{"name": "VOLUME", "call": 100, "put": 90}]}).encode()}
    import json
    REFUSE257 = {"2026-11-09"}
    _fl255.net.fetch = _fetch257
    _fl255.time.sleep = lambda s: None
    _fl255.now_ny = _at(2026, 11, 10, 20, 0)
    _m1 = _fl255.step_pc(False, budget=4)
    _fl255.now_ny = _at(2026, 11, 20, 20, 0)
    _m2 = _fl255.step_pc(False, budget=9)
    with store.conn() as _c257:
        _pcdb = [(r["d"], r["ok"]) for r in _c257.execute("SELECT d, ok FROM flow_pc ORDER BY d")]
    REFUSE257 = {(_d255(2026, 11, 20) - _td255(days=i)).isoformat() for i in range(400)}
    try:
        _fl255.step_pc(False, budget=5)
        _m3 = "went on"
    except RuntimeError as _e3:
        _m3 = str(_e3)[:40]
    with store.conn() as _c257:
        _after = len(_c257.execute("SELECT d FROM flow_pc").fetchall())
    _r257["pc_files"] = (_seen257[:4], _pcdb, _m1.split(",")[0], _m3, _after)
    _ok257 = (_r257["pos"] == (["ES"], -25.0, 0.0, None, True, 0.3, -25.0, -25.0, 20.0, True)
              and _r257["lev"] == ({"d": "2026-06-30", "last": 9.0, "prev": 8.8, "yago": 8.0, "max": 9.0, "rank": 75.0}, 20.0, None)
              and _r257["dark"] == (40.0, 3.0, 38.0, True, True, 1.0)
              and _r257["dark_text"] == f"DIX 40.0% on {_fl255._day(_dd[-1])}, the 3rd percentile of five years (38.0% on {_fl255._day(_dd[28])} "
                                         f"was the lowest since the record began, {_fl255._day(_dd[0])}); dealers' gamma $1.0bn (0th percentile)."
              and _r257["pc"] == (1.9, 1.7, 90.0, 0.5, None, False)
              and _r257["short"] == (60.0, 40.0, 20.0, 45.0, 25, None)
              and _r257["fg"] == (["Put / call ratio", "Volume breadth"], 5.0, True)
              and _r257["house"] == (2, [], ["Put / call ratio", "Volume breadth"], True)
              and _r257["alerts"] == [f"{_fl255._day(_d2)}: a 90% downside day among the S&P 500's members",
                                      "Hedge funds cut their equity-futures gross -25% in four weeks", "DIX 40.0%, a five-year low"]
              and _r257["brief"] == f"Flow: 9% of the S&P 500 members' volume rose on {_fl255._day(_d2)} (a 90% downside day); no follow-through "
                                    f"yet to the selling of {_fl255._day(_d2)}; hedge funds' equity-futures gross -25% in four weeks; DIX 40.0% (3rd percentile of five years)"
              and _r257["day"] == ("5 Nov 2001", "1st", "12th", "23rd")
              and _r257["news"] == (True, True, False, False)
              and _r257["due"] == ("2026-11-06", None, "2026-11-06", "2026-11-06")
              and _r257["diary"] == (None, [("2026-11-06", "NASDAQ", 4262938056.0, 1), ("2026-11-06", "NYSE", 1278316775.0, 1)])
              and _r257["sessions"] == (["2026-11-30", "2026-11-27", "2026-11-25"], ["2025-12-26", "2025-12-24"])
              and _r257["pc_files"] == (["2026-11-10", "2026-11-09", "2026-11-06", "2026-11-05"],
                                        [("2026-11-05", 1), ("2026-11-06", 1), ("2026-11-09", 0), ("2026-11-10", 1), ("2026-11-11", 1),
                                         ("2026-11-12", 1), ("2026-11-13", 1), ("2026-11-16", 1), ("2026-11-17", 1), ("2026-11-18", 1),
                                         ("2026-11-19", 1), ("2026-11-20", 1)],
                                        "3 read", "Cboe refused three files in a row (HTTPE", 12))
except Exception as _e257:
    import traceback as _tb257
    _r257["error"] = f"{_e257.__class__.__name__}: {_e257}"[:200] + " " + _tb257.format_exc()[-300:]
finally:
    (store.DB_PATH, _fl255.digest, _fl255.now_ny, _fl255.net.fetch, _fl255.time.sleep, _fl255.mine_syms) = _keep257
    _fl255._CACHE.clear()
    _fl255._REFUSED.clear()
    _sh257.rmtree(_dir257, ignore_errors=True)
print(f"257 FLOW's figures           {ok(_ok257)}  a 25% cut in four weeks is de-grossing; Form PF a quarter and a year back; DIX "
      f"beside yesterday's low; put/call and short volume against their own record; fear & greed whole; three alerts and the brief; "
      f"NEWS tags it; the diary's final count kept; Cboe's holidays and refusals")
if not _ok257:
    print("     ", _r257)

# 258 FLOW is wired: three routes, the page and its code, FLOW and its aliases on the command line, its collector
#     started with the app, the alerts, the brief, the house fear & greed, NEWS's filter, IDX's up-volume chart, the
#     three tables, and the build number
from pf import api as _api258
_paths258 = {r.path for r in _api258.router.routes}
_rd258 = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
_tj258, _ap258, _mo258, _sv258 = _rd258("backend", "static", "term.js"), _rd258("backend", "portfolio_app.py"), _rd258("backend", "pf", "morning.py"), _rd258("backend", "pf", "service.py")
_fj258, _ij258, _nh258 = _rd258("backend", "static", "flow.js"), _rd258("backend", "static", "idx.js"), _rd258("backend", "static", "news.html")
_bf258, _ip258, _sq258 = _rd258("backend", "pf", "brief.py"), _rd258("backend", "pf", "idx.py"), _rd258("backend", "pf", "store.py")
_ok258 = ({"/api/pf/flow", "/api/pf/flow/job", "/api/pf/flow/refresh"} <= _paths258
          and os.path.exists(os.path.join(HERE, "backend", "static", "flow.html")) and 'src="/flow.js"' in _rd258("backend", "static", "flow.html")
          and 'api("/flow")' in _fj258 and '"/flow/refresh"' in _fj258 and '"/flow/job"' in _fj258
          and 'code: "FLOW", title: "Institutional flow"' in _tj258 and '"UVOL", "DVOL"' in _tj258 and '"DIX", "GEX"' in _tj258
          and '"FLOW": "flow.html"' in _ap258 and "flow.ensure()" in _ap258 and 'b["flow"] = part("flow"' in _mo258 and 'out.append(b["flow"])' in _mo258 and "flow.alerts()" in _sv258
          and "flow.fg_components" in _bf258 and "house_fear_greed(hists, rates, extra" in _bf258 and '"updown": ud' in _ip258 and "with_volume=True" in _ip258 and "function drawUpVol()" in _ij258
          and 'FILTER === "flow"' in _nh258
          and all(f"CREATE TABLE IF NOT EXISTS {t} (" in _sq258 for t in ("flow_diary", "flow_pc", "flow_short")) and _BUILD >= 51)
print(f"258 FLOW is wired            {ok(_ok258)}  three routes; FLOW, UVOL and DIX open it; its collector starts with the app; "
      f"alerts, the brief, fear & greed, NEWS's filter, IDX's up volume, three tables, build {_BUILD}")

# 259 the 8 November audit's fixes (build 52): VSTOXX from STOXX's own file (Yahoo dropped V2TX.DE); the terminal
#     answers only to its own name and refuses a change sent from another site's page; the cache keeps no stale
#     copies (records such as ern:* untouched) and gives the space back to the disk; a turbo's underlying is in the
#     earnings week and the news, an ETF is not among the reporters; POL lists the scanned filings it could not read
import tempfile as _tf259
import shutil as _sh259
import sqlite3 as _sq259
from pathlib import Path as _P259
from pf import market as _mk259, brief as _bf259, pol as _po259
import portfolio_app as _pa259
_r259 = {}
_ok259 = False
_dir259 = _tf259.mkdtemp(prefix="pf_audit259")
_keep259 = (store.DB_PATH, _po259.DB, dict(_po259._CACHE))
try:
    _v = next(r for r in _mk259.BOARD if r["k"] == "V2TX")
    _r259["stoxx"] = (_v["src"], _v["s"], "V2TX.DE" in _mk259.YAHOO_SYMBOLS,
                      _mk259.parse_stoxx("Date;Symbol;Indexvalue\n05.11.2026;V2TX;20.6798\n06.11.2026;V2TX;21.2793\njunk;x;y\n"))
    _f = _pa259.foreign
    _h = lambda **k: {kk.replace("_", "-"): vv for kk, vv in k.items()}
    _r259["foreign"] = (
        _f("GET", _h(host="127.0.0.1:8090")), _f("GET", _h(host="localhost:8090")), _f("GET", _h(host="[::1]:8090")),
        _f("GET", _h(host="evil.example:8090")), _f("GET", _h(host="127.0.0.1:8090", origin="https://evil.example")),
        _f("POST", _h(host="127.0.0.1:8090", origin="http://127.0.0.1:8090", sec_fetch_site="same-origin")),
        _f("POST", _h(host="127.0.0.1:8090")), _f("POST", _h(host="127.0.0.1:8090", origin="https://evil.example")),
        _f("POST", _h(host="127.0.0.1:8090", origin="null")), _f("DELETE", _h(host="127.0.0.1:8090", sec_fetch_site="cross-site")))
    from fastapi.testclient import TestClient as _TC259
    _c259 = _TC259(_pa259.app, raise_server_exceptions=False)
    _r259["mounted"] = (_c259.get("/api/pf/health").status_code, _c259.get("/api/pf/health", headers={"host": "evil.example"}).status_code,
                        _c259.post("/api/pf/flow/refresh", headers={"origin": "https://evil.example"}).status_code)
    # the cache prune and the vacuum, on a database of their own
    store.DB_PATH = os.path.join(_dir259, "c.db")
    store.init_db()
    from datetime import datetime as _dt259
    _now = _dt259.utcnow().timestamp()
    _keys = {"rs:day:X:2026-01-01": 50, "rs:day:Y:2026-11-01": 10, "ern:snap:NVDA:2025-11-19": 400, "riskmap:px:abc": 30,
             "rs:own:NVDA": 70, "rs:own:AMD": 20, "idx:view:spx": 100, "rsx:other": 100, "ed:txt:old": 46}
    with store.conn() as _c:
        for _k, _days in _keys.items():
            _c.execute("INSERT INTO cache (k, v, fetched_at) VALUES (?, ?, ?)", (_k, '"' + "x" * 20000 + '"', _now - _days * 86400))
    _was = store.vacuum_mode()
    _inc = store.make_incremental()
    _m = store.maintain()
    with store.conn() as _c:
        _left = sorted(r[0] for r in _c.execute("SELECT k FROM cache"))
        _free = _c.execute("PRAGMA freelist_count").fetchone()[0]
    _r259["prune"] = (_was, _inc, store.vacuum_mode(), _m["pruned"], _left, _free, bool(store.get_setting("db_maintained")))
    # the brief's tickers
    _pos = [{"kind": "EQUITY", "ticker": "AAPL", "weight": 3.0, "value_eur": 300, "name": "Apple"},
            {"kind": "EQUITY", "ticker": "WDC", "weight": 1.0, "value_eur": 100, "name": "Western Digital"},
            {"kind": "ETF", "ticker": "EQQQ.DE", "weight": 2.0, "value_eur": 200, "name": "EQQQ"},
            {"kind": "DERIVATIVE", "ticker": None, "underlying_ticker": "WDC", "underlying": "WESTERN DIGITAL", "exposure_eur": 700, "value_eur": 100},
            {"kind": "DERIVATIVE", "ticker": None, "underlying_ticker": None, "exposure_eur": 50, "value_eur": 10},
            {"kind": "EQUITY", "ticker": "XYZ", "weight": 9.0, "value_eur": 900, "excluded": True}]
    _t, _w, _n, _rp = _bf259.book_tickers(_pos)
    _r259["tickers"] = (_t, {k: round(v, 2) for k, v in _w.items()}, _n["WDC"], _rp)
    # POL's unread scanned filings
    _tdb = _P259(_dir259) / "t.db"
    _c = _sq259.connect(_tdb)
    _c.executescript("CREATE TABLE filings (doc_id TEXT, year INTEGER, member_name TEXT, filing_date TEXT, parse_status TEXT, pdf_url TEXT);"
                     "CREATE TABLE ocr_transactions (doc_id TEXT);"
                     "INSERT INTO filings VALUES ('1', 2026, 'Ann A', '9/23/2026', 'no_text', 'https://x/1.pdf'),"
                     "('2', 2026, 'Ben B', '8/3/2026', 'no_text', 'https://x/2.pdf'), ('3', 2026, 'Cy C', '7/1/2026', 'ok', 'https://x/3.pdf'),"
                     "('4', 2025, 'Di D', '7/1/2025', 'no_text', 'https://x/4.pdf');"
                     "INSERT INTO ocr_transactions VALUES ('2');")
    _c.commit()
    _c.close()
    _po259.DB = _tdb
    _po259._CACHE.clear()
    _u = _po259.unread(2026)
    _r259["unread"] = (_u["year"], [(x["doc"], x["member"], x["filed"], x["url"]) for x in _u["filings"]], len(_po259.unread(2025)["filings"]))
    _pj = open(os.path.join(HERE, "backend", "static", "pol.js"), encoding="utf-8").read()
    _ap = open(os.path.join(HERE, "backend", "portfolio_app.py"), encoding="utf-8").read()
    _r259["wired"] = ("function unreadNote()" in _pj and "${unreadNote()}" in _pj and "unread=unread()" in open(os.path.join(HERE, "backend", "pf", "pol.py"), encoding="utf-8").read()
                      and "store.make_incremental()" in _ap and "store.ensure_maintenance()" in _ap and _BUILD >= 52)
    _ref = "a change from another site's page is refused"
    _ok259 = (_r259["stoxx"] == ("stoxx", "V2TX", False, {"2026-11-05": 20.6798, "2026-11-06": 21.2793})
              and _r259["foreign"] == (None, None, None, "this terminal answers only to 127.0.0.1", None, None, None, _ref, _ref, _ref)
              and _r259["mounted"] == (200, 403, 403)
              and _r259["prune"] == (0, True, 2, {"riskmap:px:": 1, "rs:day:": 1, "ed:txt:": 1, "rs:": 1},
                                     ["ern:snap:NVDA:2025-11-19", "idx:view:spx", "rs:day:Y:2026-11-01", "rs:own:AMD", "rsx:other"], 0, True)
              and _r259["tickers"] == (["WDC", "AAPL", "EQQQ.DE"], {"AAPL": 3.0, "WDC": 99.59, "EQQQ.DE": 2.0}, "Western Digital", ["WDC", "AAPL"])
              and _r259["unread"] == (2026, [("1", "Ann A", "2026-09-23", "https://x/1.pdf")], 1)
              and _r259["wired"])
except Exception as _e259:
    import traceback as _tb259
    _r259["error"] = f"{_e259.__class__.__name__}: {_e259}"[:200] + _tb259.format_exc()[-300:]
finally:
    store.DB_PATH, _po259.DB = _keep259[0], _keep259[1]
    _po259._CACHE.clear()
    _po259._CACHE.update(_keep259[2])
    _sh259.rmtree(_dir259, ignore_errors=True)
print(f"259 the audit's fixes        {ok(_ok259)}  VSTOXX from STOXX; only 127.0.0.1 answered, a change from another site refused; "
      f"stale copies pruned, records kept, the space given back; a turbo's share in the earnings week, no ETF; POL's unread scans")
if not _ok259:
    print("     ", _r259)

# 260 SYS and WEEK (build 53). SYS: a job within its rhythm is ok, beyond it late, a day beyond that failing; a
#     weekday job owes nothing for the weekend; a failed last try after a success is late; the home screen gets one
#     line for the failing jobs and none when all is well. WEEK: the week reviewed is the last complete one; it is
#     due from Saturday 08:00 to Monday 09:00 when missing; each holding's euros split into price, currency and what
#     the week's trades made; the time-weighted return neutralises a deposit; the first lines; the alert record
#     keeps each alert once a day and skips the quiet line
import tempfile as _tf260
import shutil as _sh260
from datetime import datetime as _dt260, date as _d260
from pf import health as _he260, weekly as _wk260
_r260 = {}
_ok260 = False
_dir260 = _tf260.mkdtemp(prefix="pf_sysweek260")
_keep260 = (store.DB_PATH, _he260.jobs, dict(_he260._ALERT), set(_wk260._SEEN))
try:
    _A = _he260.AMS
    _now = _dt260(2026, 11, 9, 9, 0, tzinfo=_A).timestamp()            # a Monday morning
    _h = lambda d, hh: _dt260(2026, 11, d, hh, 0, tzinfo=_A).timestamp()
    _j = _he260.judge
    _r260["judge"] = (
        _j({"every_h": 2, "last_ok": _now - 3600}, _now)["status"],
        _j({"every_h": 2, "last_ok": _now - 5 * 3600}, _now)["status"],
        _j({"every_h": 2, "last_ok": _now - 30 * 3600}, _now)["status"],
        _j({"idle": True, "last_ok": None}, _now)["status"],
        _j({"every_h": 26, "weekdays": True, "last_ok": _h(6, 23)}, _now)["status"],         # Friday 23:00: the weekend is free
        _j({"every_h": 26, "last_ok": _h(6, 23)}, _now)["status"],                           # every day: 58 hours is a day late
        _j({"every_h": 2, "last_ok": _now - 3600, "last_try": _now - 60, "error": "boom"}, _now)["status"],
        _j({"every_h": 2, "last_ok": None, "error": "boom"}, _now)["status"],
        round(_he260.weekend_hours(_h(6, 20), _h(9, 8)), 1))
    _he260.jobs = lambda now=None: [{"name": "Watchlist scan", "status": "failing", "error": "HTTP 429", "age_h": 60.0},
                                   {"name": "News", "status": "ok"}]
    _he260._ALERT.update(t=0.0, v=[])
    _al = _he260.alerts()
    _he260.jobs = lambda now=None: [{"name": "News", "status": "ok"}, {"name": "X", "status": "late"}]
    _he260._ALERT.update(t=0.0, v=[])
    _r260["alerts"] = ([(a["code"], a["text"], a["detail"]) for a in _al], _he260.alerts())
    # WEEK: the week, the schedule
    _lw = _wk260.last_week
    _r260["week"] = (_lw(_d260(2026, 11, 7)).isoformat(), _lw(_d260(2026, 11, 8)).isoformat(), _lw(_d260(2026, 11, 11)).isoformat())
    store.DB_PATH = os.path.join(_dir260, "w.db")
    store.init_db()
    _due = lambda *a: (lambda x: x.isoformat() if x else None)(_wk260.due(_dt260(*a, tzinfo=_A)))
    _r260["due"] = (_due(2026, 11, 7, 7, 59), _due(2026, 11, 7, 8, 0), _due(2026, 11, 8, 20, 0), _due(2026, 11, 9, 8, 59),
                    _due(2026, 11, 9, 9, 0), _due(2026, 11, 11, 12, 0))
    with store.conn() as _c:
        _c.execute("INSERT INTO week_review (wk, built_at, payload) VALUES ('2026-11-02', 1, '{}')")
    _r260["due_built"] = _due(2026, 11, 7, 8, 0)
    # the attribution: a dollar share bought into during the week, and one whose only move is the euro
    class _T:
        def __init__(self, **k):
            self.__dict__.update(k)
    _px = {"AAA": {"2026-10-30": 100.0, "2026-11-06": 110.0}, "BBB": {"2026-10-30": 100.0, "2026-11-06": 100.0}}
    _fx = {"EURUSD": {"2026-10-30": 1.10, "2026-11-06": 1.10}, "EURCAD": {"2026-10-30": 1.00, "2026-11-06": 1.25}}
    _pos = [{"ticker": "AAA", "isin": "A1", "qty": 10.0, "currency": "USD", "value_eur": 1000.0, "kind": "EQUITY", "sector": "Tech", "name": "A"},
            {"ticker": "BBB", "isin": "B1", "qty": 10.0, "currency": "CAD", "value_eur": 800.0, "kind": "EQUITY", "sector": "Energy", "name": "B"},
            {"ticker": None, "isin": "T1", "qty": 3.0, "kind": "DERIVATIVE", "value_eur": 300.0}]
    _tx = [_T(kind="BUY", trade_dt=_dt260(2026, 11, 4, 15, 0), isin="A1", quantity=5.0, value_eur=-480.0),
           _T(kind="BUY", trade_dt=_dt260(2026, 10, 20, 15, 0), isin="A1", quantity=5.0, value_eur=-400.0)]
    _at = _wk260.attribution(_pos, _tx, _d260(2026, 11, 2), _d260(2026, 11, 6), lambda t: _px.get(t, {}), lambda p: _fx.get(p, {}))
    _r260["attr"] = ([(r["ticker"], r["eur"], r["price_eur"], r["fx_eur"], r["traded_eur"], r["pct"], r["pct_eur"]) for r in _at["rows"]],
                     _at["sum_eur"], _at["fx_eur"], [(s["sector"], s["eur"]) for s in _at["sectors"]])
    # the time-weighted return, a deposit in the middle
    _ser = [{"d": "2026-10-30", "v": 1000.0, "f": 0.0}, {"d": "2026-11-02", "v": 1010.0, "f": 0.0},
            {"d": "2026-11-03", "v": 1520.0, "f": 500.0}, {"d": "2026-11-06", "v": 1530.4, "f": 0.0}]
    _r260["twr"] = _wk260._twr(_ser, "2026-11-02", "2026-11-06")
    _v = {"book": {"available": True, "eur": -812.4, "twr_pct": -3.95, "bench_pct": -2.1, "attribution": {"fx_eur": -41.0},
                   "best": [{"ticker": "APP", "pct_eur": 12.4, "eur": 86.0}], "worst": [{"ticker": "CRDO", "pct_eur": -9.8, "eur": -64.0}]},
          "markets": {"rows": [{"k": "SPX", "name": "S&P 500", "w_pct": -2.68}, {"k": "US10Y", "name": "US 10-year", "w_bp": -10}], "flow": {}},
          "ahead": {"earnings": [{"sym": "WDC", "date": "2026-11-12", "held": True, "turbo": True}], "macro": [], "central_banks": []}}
    _r260["lines"] = _wk260.lines(_v)
    # the alert record
    _wk260._SEEN.clear()
    _ra = _wk260.record_alerts([{"code": "FLOW", "text": "a 90% day", "level": "warn"}, {"code": "QUIET", "text": "nothing"},
                                {"code": "WEEK", "text": "the review"}], today="2026-11-05")
    _rb = _wk260.record_alerts([{"code": "FLOW", "text": "a 90% day", "level": "warn"}], today="2026-11-05")
    _wk260._SEEN.clear()
    _rc = _wk260.record_alerts([{"code": "FLOW", "text": "a 90% day", "level": "warn"}], today="2026-11-05")
    _tm = _wk260.terminal(_d260(2026, 11, 2), _d260(2026, 11, 6))
    # an alert already written today never reaches the database again (the home screen asks every few seconds)
    _db260 = store.DB_PATH
    store.DB_PATH = os.path.join(_dir260, "no-such-folder", "x.db")
    try:
        _rd = _wk260.record_alerts([{"code": "FLOW", "text": "a 90% day", "level": "warn"}], today="2026-11-05")
    except Exception:
        _rd = "touched the database"
    store.DB_PATH = _db260
    _r260["record"] = (_ra, _rb, _rc, _rd, [(a["d"], a["code"], a["text"]) for a in _tm["alerts"]], _tm["alerts_since"])
    _ok260 = (_r260["judge"] == ("ok", "late", "failing", "idle", "ok", "failing", "late", "failing", 48.0)
              and _r260["alerts"] == ([("SYS", "1 background job failing: Watchlist scan", "HTTP 429 · SYS")], [])
              and _r260["week"] == ("2026-11-02", "2026-11-02", "2026-11-02")
              and _r260["due"] == (None, "2026-11-02", "2026-11-02", "2026-11-02", None, None) and _r260["due_built"] is None
              and _r260["attr"] == ([("AAA", 65.45, 45.45, 0.0, 20.0, 10.0, 10.0), ("BBB", -200.0, 0.0, -200.0, None, 0.0, -20.0)],
                                    -134.55, -200.0, [("Tech", 65.45), ("Energy", -200.0)])
              and _r260["twr"] == (2.7, 30.4, 1000.0)
              and _r260["lines"] == ["Your book fell €812 (-3.95%) this week, behind the S&P 500 in euros (-2.10%); the currency took away €41.",
                                     "Most of it: APP +12.4% (+€86); against: CRDO -9.8% (−€64).",
                                     "Markets: S&P 500 -2.7%, the US 10-year -10bp.",
                                     "Next week your holdings report: WDC (turbo) 12/11."]
              and _r260["record"] == (1, 0, 0, 0, [("2026-11-05", "FLOW", "a 90% day")], "2026-11-05"))
except Exception as _e260:
    import traceback as _tb260
    _r260["error"] = f"{_e260.__class__.__name__}: {_e260}"[:200] + _tb260.format_exc()[-300:]
finally:
    store.DB_PATH, _he260.jobs = _keep260[0], _keep260[1]
    _he260._ALERT.clear()
    _he260._ALERT.update(_keep260[2])
    _wk260._SEEN.clear()
    _wk260._SEEN.update(_keep260[3])
    _sh260.rmtree(_dir260, ignore_errors=True)
print(f"260 SYS and WEEK             {ok(_ok260)}  ok, late and failing by each job's rhythm, weekends free; one home-screen line; "
      f"the last complete week, due Saturday 08:00 to Monday 09:00; price, currency and trades add up; a deposit neutralised; the alert record")
if not _ok260:
    print("     ", _r260)

# 261 SYS and WEEK are wired: five routes, two pages, their codes and words on the command line, the weekly
#     scheduler started with the app, SYS's and WEEK's alerts and the alert record in the home screen's list,
#     the three tables, the Claude work folder ignored by git, and the build number
from pf import api as _api261
_paths261 = {r.path for r in _api261.router.routes}
_rd261 = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
_tj261, _ap261, _sv261, _sq261 = _rd261("backend", "static", "term.js"), _rd261("backend", "portfolio_app.py"), _rd261("backend", "pf", "service.py"), _rd261("backend", "pf", "store.py")
_ok261 = ({"/api/pf/sys", "/api/pf/week", "/api/pf/week/job", "/api/pf/week/build", "/api/pf/week/note"} <= _paths261
          and all(os.path.exists(os.path.join(HERE, "backend", "static", f)) for f in ("sys.html", "sys.js", "week.html", "week.js"))
          and 'fetch("/api/pf/sys")' in _rd261("backend", "static", "sys.js") and 'api("/week"' in _rd261("backend", "static", "week.js")
          and 'code: "WEEK", title: "The week in review"' in _tj261 and 'code: "SYS",  title: "The terminal\'s health"' in _tj261
          and '"WEEKLY", "REVIEW"' in _tj261 and '"HEALTH", "STATUS"' in _tj261
          and '"SYS": "sys.html", "WEEK": "week.html"' in _ap261 and "weekly.ensure()" in _ap261
          and "health.alerts()" in _sv261 and "weekly.alerts()" in _sv261 and "weekly.record_alerts(out)" in _sv261
          and all(f"CREATE TABLE IF NOT EXISTS {t} (" in _sq261 for t in ("week_review", "week_note", "alert_log"))
          and "week_work/" in _rd261(".gitignore") and _BUILD >= 53)
print(f"261 SYS and WEEK are wired   {ok(_ok261)}  five routes; SYS, HEALTH, WEEK and REVIEW open them; the Saturday run starts with "
      f"the app; the home screen hears from both and keeps every alert; three tables; build {_BUILD}")

# 262 Koers AI sees its own holdings' figures (build 54). On 13 November it reported for the fourth time that it had
#     only a price, a P/E and a yield for ResMed: it bought RMD on 30 October 15% below its high, the watchlist scan
#     reads in full only dips of 15%, your stocks and the 300 largest, and RMD (400th) recovered out of the dip. The
#     scan now always reads Koers AI's holdings in full, and until it has, Koers AI builds a holding's figures from the
#     scan's cached statements and estimates without fetching anything
import tempfile as _tf262
import shutil as _sh262
import json as _js262
from pf import koersai as _ka262, watch as _wa262
_r262 = {}
_ok262 = False
_dir262 = _tf262.mkdtemp(prefix="pf_kai262")
_keep262 = (store.DB_PATH, _ka262.started, _ka262.holdings, _ka262.universe)
try:
    # the scan's side: Koers AI's holdings join the full read, only once it has started
    _ka262.started = lambda: {"at": "2026-10-28T21:08:30+01:00", "d": "2026-10-28", "capital": 100000.0}
    _ka262.holdings = lambda upto=None: {"pos": {"RMD": {"qty": 27.3}, "ADP": {"qty": 15.3}}, "cash": 0.0}
    _on = sorted(_wa262._kai_held())
    _ka262.started = lambda: None
    _off = _wa262._kai_held()
    _src = open(os.path.join(HERE, "backend", "pf", "watch.py"), encoding="utf-8").read()
    _r262["scan"] = (_on, _off, "cand = full_read(by_sym, pm, pinned, set(held) | kai, rows)" in _src,
                     "list(pinned) + list(held) + sorted(kai)" in _src)
    # Koers AI's side: a holding with no full row in the scan gets its figures from the cached statements
    with store.conn() as _c:
        _f = _c.execute("SELECT k, v FROM cache WHERE k LIKE 'wl:fin:%' AND k IN (SELECT REPLACE(k, 'wl:est:', 'wl:fin:') FROM cache "
                        "WHERE k LIKE 'wl:est:%') ORDER BY (k = 'wl:fin:RMD') DESC LIMIT 1").fetchone()
        _sym = _f[0][7:] if _f else None
        _e = _c.execute("SELECT v FROM cache WHERE k = ?", (f"wl:est:{_sym}",)).fetchone() if _sym else None
        _u = _js262.loads((_c.execute("SELECT v FROM cache WHERE k = 'wl:universe'").fetchone() or ['{"rows": []}'])[0])
    _urow = next((r for r in _u.get("rows") or [] if r.get("sym") == _sym), None)
    store.DB_PATH = os.path.join(_dir262, "k.db")
    store.init_db()
    if _sym and _urow:
        store.cache_put(f"wl:fin:{_sym}", _js262.loads(_f[1]))
        store.cache_put(f"wl:est:{_sym}", _js262.loads(_e[0]))
        store.cache_put("wl:universe", {"rows": [_urow], "fx": _u.get("fx") or {}})
        store.cache_put("wl:scan", {"rows": []})
        _ka262.universe = lambda: {_sym}

        class _FX262:
            def eur(self, amount, ccy, d):
                return amount
        _held_row = next((x for x in _ka262.market_rows({_sym}, _FX262(), "2026-11-13") if x["t"] == _sym), {})
        _hf = _ka262.holding_figures(_sym, _urow, _u.get("fx") or {})
        _r262["kai"] = (bool(_sym), all(k in _held_row for k in ("om", "om3", "gm", "rg", "fcfm", "q", "g")),
                        _held_row.get("om") == round(_hf[0]["om"], 1) if _hf[0].get("om") is not None else None,
                        _ka262.holding_figures("NOPE", {"sym": "NOPE"}, {}))
    else:
        _r262["kai"] = ("no symbol with cached statements and estimates in this database",)
    _ok262 = (_r262["scan"] == (["ADP", "RMD"], set(), True, True)
              and _r262["kai"] == (True, True, True, ({}, {})))
except Exception as _e262:
    import traceback as _tb262
    _r262["error"] = f"{_e262.__class__.__name__}: {_e262}"[:200] + _tb262.format_exc()[-300:]
finally:
    store.DB_PATH, _ka262.started, _ka262.holdings, _ka262.universe = _keep262
    _sh262.rmtree(_dir262, ignore_errors=True)
print(f"262 Koers AI's holdings read {ok(_ok262)}  its holdings always read in full by the scan, once its contest has started; "
      f"until then a holding's figures from the cached statements, nothing fetched; none for a stock with nothing cached")
if not _ok262:
    print("     ", _r262)

# ==========================================================================
# build 55: the read, the board's day change, FRED's gaps, EUR, Koers AI's headlines
# ==========================================================================

# 263 the read (build 55). Every move is measured against its own normal day, so a 6% day in the VIX counts as much
#     as a 1% day in the S&P 500; groups are averaged before the verdict, so four equity indices cannot outvote the
#     rest; FRED's spreads, a session behind, are shown dated and kept out of the session's score; the curve's shape,
#     the stress gauges, the disagreements, sixty sessions of history and what the session implies for the book
from pf import market as _mk263
_r263 = {}
_ok263 = False
try:
    _d0 = date(2025, 9, 1)
    _days263 = [(_d0 + _td(days=i)).isoformat() for i in range(400)]
    _spx, _vix, _v, _w = {}, {}, 100.0, 20.0
    for _i, _d in enumerate(_days263):
        _v *= 1.01 if _i % 2 else 0.99
        _w *= 1.06 if _i % 2 else 0.94
        _spx[_d], _vix[_d] = _v, _w
    _sd = (_mk263.normal_day(_spx, "pct", 9.0), _mk263.normal_day(_vix, "pct", 9.0), _mk263.normal_day({}, "pct", 1.3))
    _S = "2026-11-13"
    _rows = lambda **kw: {k: {"asof": _S, **v} for k, v in kw.items()}
    # a 1% day for stocks and a 6% day for the VIX against it: one normal day each, opposite ways, so mixed
    _a = _mk263.risk_read(_rows(SPX={"d_pct": 1.0}, VIX={"d_pct": 6.0}), {"SPX": _spx, "VIX": _vix})
    # the same stocks with the VIX down 3%: half a normal day of calm on top, so risk-on
    _b = _mk263.risk_read(_rows(SPX={"d_pct": 1.0}, VIX={"d_pct": -3.0}), {"SPX": _spx, "VIX": _vix})
    # four equity indices up one normal day and gold up one: the groups weigh equally, so it is mixed, not 4 against 1
    _c = _mk263.risk_read(_rows(SPX={"d_pct": 1.0}, NDX={"d_pct": 1.3}, SX5E={"d_pct": 1.0}, AEX={"d_pct": .9},
                                GOLD={"d_pct": 1.0}))
    # credit dated the session before: shown, not scored; the same move dated today is scored, and with stocks up
    # it is a disagreement
    _lag = _mk263.risk_read({"SPX": {"d_pct": 1.5, "asof": _S}, "USHY": {"d_abs": 0.10, "asof": "2026-11-12", "last": 3.2}})
    _now = _mk263.risk_read({"SPX": {"d_pct": 1.5, "asof": _S}, "USHY": {"d_abs": 0.10, "asof": _S, "last": 3.2}})
    _cr = next(g for g in _lag["groups"] if g["group"] == "Credit")
    _r263["scale"] = (round(_sd[0], 1), round(_sd[1], 1), _sd[2], _a["verdict"], _b["verdict"], _c["verdict"], _c["of"])
    _r263["lag"] = (_cr["z"], _cr["signals"][0]["lag"], _lag["composite"], _lag["of"], _now["of"],
                    any("not confirming" in f for f in _now["flags"]), any("not in today's score" in s for s in _lag["story"]))
    _ry = lambda be_d: _mk263.risk_read({"SPX": {"d_pct": .1, "asof": _S}, "US10Y": {"d_abs": 0.10, "asof": "2026-11-12"},
                                         "US10BE": {"d_abs": 0.04, "asof": be_d}})["rates"]["real"]
    _r263["real"] = (_ry("2026-11-12"), _ry(_S))
    _r263["shape"] = tuple((_mk263.curve_shape(*x) or {}).get("kind") for x in ((13, 12), (-10, -2), (-2, -10), (1, -1)))
    _st = _mk263.risk_read(_rows(SPX={"d_pct": .1}, VIX={"d_pct": 0.0, "pctile": 90, "last": 30.0},
                                 USHY={"d_abs": 0.0, "pctile": 95, "last": 5.0}))["stress"]
    _r263["stress"] = (_st["pctile"], _st["label"])
    _bk = _mk263.book_read({"SPX": {"d_pct": 1.0}, "EURUSD": {"d_pct": 0.5}}, {"beta": 1.2, "usd_pct": 80.0})
    _r263["book"] = (_bk["price"], _bk["fx"], _bk["total"], _mk263.book_read({}, None))
    _hist = _mk263.history_read({"SPX": _spx, "VIX": _vix}, {"SPX": 1.0, "VIX": 6.0})
    _r263["hist"] = (len(_hist), _hist[-1]["d"] == _days263[-1], {h["h"] for h in _a["horizons"]})
    _src = open(os.path.join(HERE, "backend", "static", "market.html"), encoding="utf-8").read()
    _bsrc = open(os.path.join(HERE, "backend", "pf", "brief.py"), encoding="utf-8").read()
    _r263["wired"] = ("rd-grid" in _src and "histSvg(R.history)" in _src and "R.story" in _src and "DATA.risk = DATA.risk" not in _src
                      and "market.book_read(by_key, you)" in _bsrc and 'store.cache_get("read:live")' in _bsrc)
    _ok263 = (_r263["scale"] == (1.0, 6.0, 1.3, "mixed", "risk-on", "mixed", 2)
              and _r263["lag"] == (None, True, 1.5, 1, 2, True, True)
              and _r263["shape"] == ("bear flattener", "bull steepener", "bull flattener", "little changed")
              and _r263["real"] == (6.0, None) and _r263["stress"] == (92, "stressed") and _r263["book"] == (1.2, -0.4, 0.8, None)
              and _r263["hist"] == (60, True, {"1D", "1W", "1M"}) and _r263["wired"])
except Exception as _e263:
    import traceback as _tb263
    _r263["error"] = f"{_e263.__class__.__name__}: {_e263}"[:200] + _tb263.format_exc()[-400:]
print(f"263 the read                 {ok(_ok263)}  each move in its own normal days (S&P 1%, VIX 6%), groups weighed alike, "
      f"FRED's day-old credit shown and not scored, the curve's shape, stress {_r263.get('stress')}, the book's share, 60 sessions")
if not _ok263:
    print("     ", _r263)

# 264 FRED's gaps (build 55). On 14 November one batch of ten series (the effective fed funds rate, SOFR, the
#     breakevens, the term premium) failed while the rest answered, and the gap was cached for six hours as a
#     success: the next-Fed-move panel said "no effective rate" with FRED fine. A missing series is asked once more,
#     and one still missing is kept from the last copy and listed as kept
import tempfile as _tf264
import shutil as _sh264
import threading as _th264
from pf import bonds as _bo264, feeds as _fe264
_r264 = {}
_ok264 = False
_dir264 = _tf264.mkdtemp(prefix="pf_fred264")
_keep264 = (store.DB_PATH, _fe264.fred)
try:
    store.DB_PATH = os.path.join(_dir264, "f.db")
    store.init_db()
    _calls264, _lk264 = {"n": 0, "sofr": 0}, _th264.Lock()

    def _fred264(ids, days=500):
        with _lk264:
            _calls264["n"] += 1
            if "SOFR" in ids:
                _calls264["sofr"] += 1
                if _calls264["sofr"] == 1:
                    return {"series": {}, "error": "TimeoutError: the batch timed out"}
        return {"series": {i: {"2026-11-12": 1.0} for i in ids if i != "DFF"}, "error": None}
    _fe264.fred = _fred264
    store.cache_put("bonds:us", {**_bo264._pack({"DFF": {"2026-11-11": 3.88}, "DGS10": {"2026-11-11": 5.0}}), "missing": []})
    _p = _bo264._load_us()
    _u = _bo264._unpack(_p)
    _r264["us"] = (_u.get("DFF"), "SOFR" in _u, _p["carried"], _p["missing"], _calls264["sofr"])
    _ser, _miss = _bo264._fred(["AAA", "DFF"], "2026-01-01")
    _r264["fred"] = (sorted(_ser), _miss)
    _ok264 = (_r264["us"] == ({"2026-11-11": 3.88}, True, ["DFF"], [], 2) and _r264["fred"] == (["AAA"], ["DFF"]))
except Exception as _e264:
    import traceback as _tb264
    _r264["error"] = f"{_e264.__class__.__name__}: {_e264}"[:200] + _tb264.format_exc()[-300:]
finally:
    store.DB_PATH, _fe264.fred = _keep264
    _sh264.rmtree(_dir264, ignore_errors=True)
print(f"264 FRED's gaps              {ok(_ok264)}  a failed batch asked again; a series still missing kept from the last copy "
      f"and listed as kept, not cached as gone")
if not _ok264:
    print("     ", _r264)

# 265 EUR is served at once (build 55). The page's cache lives half an hour and its build reads the ECB's effective
#     rate since 1999: on 14 November the first visit after the half hour waited 32 seconds. A copy past its half
#     hour is served while a new one is built behind it; past eight hours, or by another build, it is rebuilt first
import tempfile as _tf265
import shutil as _sh265
from pf import euro as _eu265
_r265 = {}
_ok265 = False
_dir265 = _tf265.mkdtemp(prefix="pf_eur265")
_keep265 = (store.DB_PATH, _eu265.build)
try:
    store.DB_PATH = os.path.join(_dir265, "e.db")
    store.init_db()
    _n265 = {"n": 0}

    def _build265():
        _n265["n"] += 1
        return {"available": True, "n": _n265["n"]}
    _eu265.build = _build265

    def _aged265(h):
        store.cache_put("eur:view", {"available": True, "n": 0})
        with store.conn() as _c:
            _c.execute("UPDATE cache SET fetched_at = fetched_at - ? WHERE k = 'eur:view'", (h * 3600,))
    _aged265(0.2)
    _fresh = _eu265.view()["n"]                      # young: served, nothing built
    _aged265(1.0)
    _served = _eu265.view()["n"]                     # an hour old: served at once ...
    _t = _eu265._REBUILD["t"]
    if _t:
        _t.join(10)
    _after = (store.cache_get("eur:view") or {}).get("n")   # ... and rebuilt behind it
    _aged265(9.0)
    _old = _eu265.view()["n"]                        # nine hours: rebuilt first
    _r265["v"] = (_fresh, _served, _after, _old, _n265["n"])
    _ok265 = _r265["v"] == (0, 0, 1, 2, 2)
except Exception as _e265:
    import traceback as _tb265
    _r265["error"] = f"{_e265.__class__.__name__}: {_e265}"[:200] + _tb265.format_exc()[-300:]
finally:
    store.DB_PATH, _eu265.build = _keep265
    _sh265.rmtree(_dir265, ignore_errors=True)
print(f"265 EUR is served at once    {ok(_ok265)}  a copy past its half hour served while a new one is built behind it; "
      f"past eight hours rebuilt first {_r265.get('v')}")
if not _ok265:
    print("     ", _r265)

# 266 Koers AI's headlines and late evenings (build 55). Its Friday decision of 13 November was sent thirty headlines,
#     all from 21:28 to 22:49 that evening, a fifth of them listicles. It now gets two from each holding's own feed,
#     then the market's at most six a day over four days, the news log's untagged ones after the feeds', no
#     listicles, and never a headline tagged with a ticker (those were fetched for Rick's stocks). An evening's check
#     that waits for a scan running past midnight still runs, until 03:00, for the evening before
import tempfile as _tf266
import shutil as _sh266
import json as _js266
from datetime import datetime as _dt266
from pf import koersai as _ka266, reader as _rd266
_r266 = {}
_ok266 = False
_dir266 = _tf266.mkdtemp(prefix="pf_kai266")
_keep266 = (store.DB_PATH, _rd266._rss, _ka266.events, _ka266._data_ready)
try:
    store.DB_PATH = os.path.join(_dir266, "k.db")
    store.init_db()
    _AMS266 = _ka266.AMS
    _now266 = _dt266(2026, 11, 13, 23, 0, tzinfo=_AMS266)
    _t0 = _now266.timestamp()

    def _rss266(q):
        if q == _ka266.MARKET_FEEDS:
            return [{"title": f"Index story {i}", "ts": _t0 - 600 * i, "source": "Yahoo"} for i in range(4)]
        return [{"title": f"{q} news {i}", "ts": _t0 - 3600 * i, "source": "Yahoo"} for i in range(5)] + \
               [{"title": "5 Stocks to Buy According to Reddit", "ts": _t0 - 60, "source": "Yahoo"}]
    _rd266._rss = _rss266
    store.cache_put("news", {"items": [{"headline": "Rick's own holding news", "mine": True, "ts": _t0 - 100, "source": "y"},
                                       {"headline": "Fed holds rates", "mine": False, "ts": _t0 - 200, "source": "y"}]})
    with store.conn() as _c:
        for _i in range(40):                         # ten a day over four days, untagged, plus one tagged
            _c.execute("INSERT INTO news_log (k, ts, seen, headline, source, tickers, category) VALUES (?,?,?,?,?,?,?)",
                       (f"u{_i}", _t0 - 3600 * 9.6 * _i - 900, _t0, f"Log story {_i}", "Reuters", "[]",
                        "merger" if _i % 5 == 0 else "business"))
        _c.execute("INSERT INTO news_log (k, ts, seen, headline, source, tickers, category) VALUES "
                   "('t1', ?, ?, 'A tagged headline on ASML', 'Yahoo', '[\"ASML\"]', 'company')", (_t0 - 3600 * 80, _t0))
    _hl = _ka266.headlines({"AAA", "BBB"}, _now266)
    _days = {}
    for _l in _hl:
        if "Log story" in _l or "Index story" in _l or "Fed holds" in _l:
            _days[_l[2:8]] = _days.get(_l[2:8], 0) + 1
    _r266["hl"] = (sum("AAA news" in l for l in _hl), sum("BBB news" in l for l in _hl), _hl[0].endswith("AAA news 0") or _hl[0].endswith("BBB news 0"),
                   any("Reddit" in l for l in _hl), any("tagged" in l for l in _hl), any("Rick's own" in l for l in _hl),
                   any("Fed holds rates" in l for l in _hl), max(_days.values()) <= _ka266.PER_DAY, len(_days) >= 4,
                   len(_hl) <= 2 * _ka266.PER_HOLDING + _ka266.MARKET_MAX)
    # the late evening
    _ka266.events = lambda d: [f"AAA moved on {d.isoformat()}."]
    _ka266._data_ready = lambda n: True
    _A = lambda *a: _dt266(*a, tzinfo=_AMS266)
    _ev = (_ka266.event_due(_A(2026, 11, 11, 0, 30)),     # Wednesday 00:30: Tuesday's evening, its scan ran late
           _ka266.event_due(_A(2026, 11, 11, 1, 0)),      # done once
           _ka266.event_due(_A(2026, 11, 11, 22, 50)),    # Wednesday before 23:00: not yet
           _ka266.event_due(_A(2026, 11, 11, 23, 5)),     # Wednesday's own evening
           _ka266.event_due(_A(2026, 11, 14, 0, 30)),     # Saturday 00:30 is Friday's evening: the weekly's, not a check
           _ka266.event_due(_A(2026, 11, 12, 4, 0)))      # 04:00: no evening
    _r266["ev"] = _ev
    _ok266 = (_r266["hl"] == (2, 2, True, False, False, False, True, True, True, True)
              and _ev == ("AAA moved on 2026-11-10.", None, None, "AAA moved on 2026-11-11.", None, None))
except Exception as _e266:
    import traceback as _tb266
    _r266["error"] = f"{_e266.__class__.__name__}: {_e266}"[:200] + _tb266.format_exc()[-300:]
finally:
    store.DB_PATH, _rd266._rss, _ka266.events, _ka266._data_ready = _keep266
    _sh266.rmtree(_dir266, ignore_errors=True)
print(f"266 Koers AI's headlines     {ok(_ok266)}  two per holding first, then the market's at most six a day over four days, "
      f"no listicles, nothing tagged to a ticker; a check after a scan past midnight runs for the evening before")
if not _ok266:
    print("     ", _r266)

# ==========================================================================
# build 56: Koers AI's results, markets, orders and weights, and its monthly review
# ==========================================================================

# 267 What Koers AI is sent (build 56, the user's choice of 14 November). Each holding's latest quarter: EPS against
#     the estimate as Yahoo records it, revenue against the consensus the terminal recorded before the report (only
#     in the same currency), the year-on-year change, margins, the shares on the session the report reached (after the
#     close: the next one), how the next quarter's consensus moved since, a report after its last turn marked new.
#     MKT's read of the markets, never its part about Rick's book. What became of its orders: filled, cut, refused at
#     the open or at the decision. Its weights by sector, currency and listing
import tempfile as _tf267
import shutil as _sh267
from datetime import datetime as _dt267
from zoneinfo import ZoneInfo as _ZI267
from pf import koersai as _ka267, watch as _wa267
_r267 = {}
_ok267 = False
_dir267 = _tf267.mkdtemp(prefix="pf_kai267")
_keep267 = (store.DB_PATH, _ka267.earn_dates, _ka267.bars)
try:
    store.DB_PATH = os.path.join(_dir267, "k.db")
    store.init_db()
    _NY, _AM = _ZI267("America/New_York"), _ka267.AMS
    _rep = _dt267(2026, 11, 3, 16, 5, tzinfo=_NY).timestamp()
    _rep_eu = _dt267(2026, 10, 14, 7, 0, tzinfo=_AM).timestamp()
    _nxt = _dt267(2027, 2, 24, 16, 5, tzinfo=_NY).timestamp()
    _ED = {"XUS": [{"ts": _rep, "est": 4.5, "act": 4.66, "sur": 3.56}, {"ts": _nxt, "est": 2.1, "act": None, "sur": None}],
           "XEU.AS": [{"ts": _rep_eu, "est": 10.76, "act": 11.82, "sur": 9.85}]}
    _ka267.earn_dates = lambda s: _ED.get(s, [])
    _BARS = {"XUS": {"2026-11-02": (100, 100, 100, 100, 100), "2026-11-03": (100, 100, 100, 100, 100),
                     "2026-11-04": (105, 110, 104, 109.8, 109.8), "2026-11-13": (102, 103, 101, 102.5, 102.5)},
             "XEU.AS": {"2026-10-13": (50, 50, 50, 50, 50), "2026-10-14": (51, 51, 50, 50.6, 50.6), "2026-11-13": (45, 45, 44, 44.85, 44.85)}}
    _ka267.bars = lambda s: dict(_BARS.get(s, {}))
    _V = _wa267.WL_VER
    store.cache_put("wl:fin:XUS", {"ver": _V, "q": [{"end": "2025-09-30", "revenue": 9.0e9, "op_margin": 43.7},
                                                     {"end": "2026-06-30", "revenue": 7.35e9, "op_margin": 34.0},
                                                     {"end": "2026-09-30", "revenue": 8.89e9, "op_margin": 38.4, "fcf_margin": 12.0}]})
    store.cache_put("wl:est:XUS", {"ver": _V, "info": {"financialCurrency": "USD"}})
    store.cache_put("wl:fin:XEU.AS", {"ver": _V, "q": [{"end": "2025-09-30", "revenue": 7.5e9}, {"end": "2026-09-30", "revenue": 11.66e9}]})
    store.cache_put("wl:est:XEU.AS", {"ver": _V, "info": {"financialCurrency": "EUR"}})
    with store.conn() as _c:
        for _row in (("XUS", "2026-10-30", "2026-09-30", 9.54e9, "2026-12-31", 6.79e9, 12.37, "2027-12-31", 233.0),
                     ("XUS", "2026-11-13", "2026-12-31", 6.50e9, "2027-03-31", 5.9e9, 12.26, "2027-12-31", 216.0),
                     ("XEU.AS", "2026-10-10", "2026-09-30", 11.0e9, "2026-12-31", 14.0e9, 50.0, "2027-12-31", None)):
            _c.execute("INSERT INTO est_day (sym, d, src, end_0q, rev_0q, end_1q, rev_1q, eps_1y, end_1y, tgt_mean, rev_ccy, px_ccy, at) "
                       "VALUES (?,?,'scan',?,?,?,?,?,?,?,'USD','USD',0)", _row)
    _now = _dt267(2026, 11, 13, 23, 0, tzinfo=_AM)
    _x = _ka267.latest_result("XUS", _now, {})
    _e = _ka267.latest_result("XEU.AS", _now, {})
    _r267["us"] = (_x.get("eps_sur"), _x.get("rev_sur"), _x.get("rev_yoy"), _x.get("om"), _x.get("om_ya"), _x.get("nq_chg"),
                   _x.get("eps_y_chg"), (_x.get("tgt_before"), _x.get("tgt_now")), _x.get("move_d"), _x.get("move"), _x.get("since"),
                   _x.get("when"), _x.get("next"))
    _r267["eu"] = (_e.get("when"), _e.get("move_d"), "rev_sur" in _e, _e.get("rev_yoy"))
    _l1 = _ka267.result_line({**_x, "n": "Xus Inc."}, "2026-11-01T00:00:00+01:00")
    _l2 = _ka267.result_line(_x, "2026-11-06T23:00:00+01:00")
    _r267["line"] = ("NEW since your last turn" in _l1, "NEW" not in _l2, "against 9.54bn expected (-6.8%)" in _l1,
                     "6.79bn -> 6.50bn USD (-4.3%)" in _l1, "Next report 24 Feb 2027" in _l1,
                     "no reported quarter" in _ka267.result_line({"t": "ZZZ"}))
    # the markets: the read and the levels, never the parts about Rick's book
    store.cache_put("market", {"rows": [{"k": "SPX", "last": 7561.67, "u": "idx", "d_pct": -0.4, "w_pct": 2.5, "m_pct": -2.4, "y_pct": 10.5},
                                        {"k": "US10Y", "last": 5.1, "u": "pct", "d_abs": 0.12, "w_abs": 0.24, "m_abs": 0.0, "y_abs": 0.92}],
                               "risk": {"horizons": [{"h": "1D", "verdict": "risk-off", "composite": -0.52},
                                                     {"h": "1W", "verdict": "risk-on", "composite": 0.6},
                                                     {"h": "1M", "verdict": "mixed", "composite": -0.24}],
                                        "session": "2026-11-13", "story": ["Stocks: the S&P 500 -0.4%."], "flags": ["Gold rose with stocks."],
                                        "stress": {"label": "elevated", "pctile": 77}, "book": {"note": "RICKSBOOK"},
                                        "coverage": {"main": {"title": "RICKSREAD"}}},
                               "you": {"beta": "RICKSYOU"}, "correlations": {"labels": {"YOU": "RICKSCORR"}}})
    _mt = "\n".join(_ka267.markets_block())
    _r267["markets"] = ("Session risk-off (-0.52), week risk-on (+0.60)" in _mt, "- Stocks: the S&P 500 -0.4%." in _mt,
                        "- Note: Gold rose with stocks." in _mt, "S&P 500 7,561.67 (day -0.4%" in _mt,
                        "US 10-year 5.10% (day +12bp, week +24bp" in _mt, "RICKS" not in _mt)
    # its orders: filled and cut, refused at the open, refused at the decision; a decision three weeks old is left out
    with store.conn() as _c:
        _c.execute("INSERT INTO kai_decision (id, ts, kind, rejected) VALUES (1, '2026-10-20T23:00:00+02:00', 'weekly', '[]')")
        _c.execute("INSERT INTO kai_decision (id, ts, kind, rejected) VALUES (2, '2026-11-06T23:00:00+01:00', 'weekly', ?)",
                   ('[{"action": "BUY", "ticker": "BAD", "amount": 5000, "why": "not in this turn\'s market data"}]',))
        _c.execute("INSERT INTO kai_order (id, decision_id, ts, ticker, side, amount_eur, amount_all, status, fill_d) VALUES "
                   "(1, 1, '2026-10-20T23:00:00+02:00', 'OLD', 'BUY', 1000, 0, 'filled', '2026-10-21')")
        _c.execute("INSERT INTO kai_order (id, decision_id, ts, ticker, side, amount_eur, amount_all, status, fill_d) VALUES "
                   "(2, 2, '2026-11-06T23:00:00+01:00', 'XUS', 'BUY', 12000, 0, 'filled', '2026-11-09')")
        _c.execute("INSERT INTO kai_order (id, decision_id, ts, ticker, side, amount_eur, amount_all, status, fill_d, note) VALUES "
                   "(3, 2, '2026-11-06T23:00:00+01:00', 'XEU.AS', 'SELL', NULL, 1, 'rejected', '2026-11-09', 'no longer held')")
        _c.execute("INSERT INTO kai_trade (order_id, d, ticker, side, qty, px, ccy, unit_eur, value_eur, cost_eur, note) VALUES "
                   "(2, '2026-11-09', 'XUS', 'BUY', 80.5, 149.0, 'USD', 136.6, 11000.0, 1.1, 'cut to 20% at the open')")
    _ob = "\n".join(_ka267.orders_block(_now))
    _r267["orders"] = ("BUY XUS EUR 12,000 -> filled at the open on 9 Nov: 80.5000 at 149.00 USD, EUR 11,000.00 (cut to 20% at the open)" in _ob,
                       "SELL XEU.AS ALL -> refused at the open: no longer held" in _ob,
                       "BUY BAD EUR 5,000 -> refused at the decision: not in this turn's market data" in _ob, "OLD" not in _ob)
    store.cache_put("wl:scan", {"rows": [{"sym": "XUS", "sector": "Technology"}, {"sym": "XEU.AS", "sector": "Industrials"}]})
    _wl = _ka267.weights_lines({"value": 100000.0, "cash": 10000.0, "rows": [{"ticker": "XUS", "value_eur": 60000.0, "ccy": "USD"},
                                                                            {"ticker": "XEU.AS", "value_eur": 30000.0, "ccy": "EUR"}]})
    _r267["weights"] = _wl == ["Weights by sector: Technology 60.0% (XUS), Industrials 30.0% (XEU.AS), cash 10.0%.",
                               "By currency: USD 60.0%, EUR 30.0%, cash (EUR) 10.0%. By listing: US 60.0%, Europe 30.0%."]
    _ks = open(os.path.join(HERE, "backend", "pf", "koersai.py"), encoding="utf-8").read()
    _r267["wired"] = all(x in _ks for x in ("L += weights_lines(book)", "L += orders_block(n)", "L += results_block(held, names, n, last_turn)",
                                            "L += last_review_block()", "L += markets_block()", "- Your holdings' latest reported quarters"))
    _ok267 = (_r267["us"] == (3.6, -6.8, -1.2, 38.4, 43.7, -4.3, -0.9, (233.0, 216.0), "2026-11-04", 9.8, 2.5, "after the close", "2027-02-24")
              and _r267["eu"] == ("before the open", "2026-10-14", False, 55.5)
              and all(_r267["line"]) and all(_r267["markets"]) and all(_r267["orders"]) and _r267["weights"] and _r267["wired"])
except Exception as _e267:
    import traceback as _tb267
    _r267["error"] = f"{_e267.__class__.__name__}: {_e267}"[:200] + _tb267.format_exc()[-400:]
finally:
    store.DB_PATH, _ka267.earn_dates, _ka267.bars = _keep267
    _sh267.rmtree(_dir267, ignore_errors=True)
print(f"267 what Koers AI is sent    {ok(_ok267)}  each holding's last quarter against the consensus and the session it moved, "
      f"MKT's read without Rick's book, its orders' fates, its weights")
if not _ok267:
    print("     ", _r267)

# 268 Koers AI's monthly self-review (build 56). From the first Saturday of a month at 10:00, at weekends only, once a
#     month, for the days since the last review (or the start); a period under ten New York sessions waits (the
#     contest's first three days in October roll into November's). It grades its theses, judges its process, names
#     mistakes and rule changes; it places no orders; the next decisions read it. Turnover is the lesser of bought and
#     sold, so the first deployment is not turnover
import tempfile as _tf268
import shutil as _sh268
import json as _js268
from datetime import datetime as _dt268, date as _d268
from pf import koersai as _ka268, reader as _rd268, api as _api268
_r268 = {}
_ok268 = False
_dir268 = _tf268.mkdtemp(prefix="pf_kai268")
_keep268 = (store.DB_PATH, _ka268.started, _ka268.curve, _ka268.bars, _ka268._ccy_of, _ka268.earn_dates, _rd268.run_json,
            _rd268.writer_ready, _ka268.start_review, _ka268._remember_reports, _ka268.fill_pending, _ka268._may_run,
            _ka268.first_due, _ka268.weekly_due, _ka268.event_due, _ka268.review_due, dict(_ka268.JOB))
try:
    store.DB_PATH = os.path.join(_dir268, "r.db")
    store.init_db()
    _A = lambda *a: _dt268(*a, tzinfo=_ka268.AMS)
    _ka268.started = lambda: {"at": "2026-10-28T21:08:30+01:00", "d": "2026-10-28", "capital": 100000.0}
    _due = (_ka268.review_due(_A(2026, 11, 7, 10, 30)), _ka268.review_due(_A(2026, 12, 5, 9, 0)), _ka268.review_due(_A(2026, 12, 5, 10, 5)),
            _ka268.review_due(_A(2026, 12, 7, 12, 0)), _ka268.review_due(_A(2026, 12, 12, 11, 0)))
    _r268["due"] = (_due[0], _due[1], _due[2], _due[3], _due[4] == _due[2], _ka268.next_review(_A(2026, 11, 14, 15, 0)),
                    _ka268.next_review(_A(2026, 12, 8, 12, 0)))
    # the material: two buys of EUR 94,000 in all and no sale is no turnover; each position with its first and latest thesis
    _ka268.curve = lambda: {"available": False}
    _ka268.bars = lambda s: {"2026-10-28": (100, 100, 100, 100, 100), "2026-11-13": (110, 110, 110, 110, 110)}
    _ka268._ccy_of = lambda s: "EUR"
    _ka268.earn_dates = lambda s: []
    with store.conn() as _c:
        _c.execute("INSERT INTO kai_decision (id, ts, kind, journal, trades, rejected, theses, meta) VALUES "
                   "(1, '2026-10-28T23:05:00+01:00', 'start', 'First.', '[]', '[]', ?, '{}')",
                   ('[{"ticker": "AAA", "thesis": "t1", "exit_if": "e1"}]',))
        _c.execute("INSERT INTO kai_decision (id, ts, kind, journal, trades, rejected, theses, meta, trigger) VALUES "
                   "(2, '2026-11-04T23:05:00+01:00', 'event', 'Checked.', '[]', '[]', ?, '{}', 'AAA moved +6.0% today.')",
                   ('[{"ticker": "AAA", "thesis": "t2", "exit_if": "e2"}]',))
        for _i, (_tk, _v) in enumerate((("AAA", 54000.0), ("BBB", 40000.0)), 1):
            _c.execute("INSERT INTO kai_trade (order_id, d, ticker, side, qty, px, ccy, unit_eur, value_eur, cost_eur, note) "
                       "VALUES (?, '2026-10-29', ?, 'BUY', ?, 100, 'EUR', 100, ?, ?, '')", (_i, _tk, _v / 100, _v, _v * 1e-4))
    _m = _ka268.review_material(_d268(2026, 10, 28), _d268(2026, 11, 13), "monthly")
    _r268["material"] = ("No trades this turn" in _m, "THE PERIOD'S SCOREBOARD" in _m, "- AAA: P/L in the period EUR +5,394.60" in _m,
                         "Your first thesis (28 Oct 2026): t1 Exit if: e1" in _m, "Your latest (4 Nov 2026, after 1 change): t2" in _m,
                         "1 event check, 1 first decision; 0 of the 1 event check led to a trade" in _m,
                         "turnover (the lesser of the two) 0.0%" in _m, "because AAA moved +6.0% today." in _m)
    # the run: a stand-in answers, its words are kept to the shape, nothing is ordered, the next turns read it
    _rd268.writer_ready = lambda: None
    _rd268.run_json = lambda job, system, text, web, key: ({
        "grades": [{"ticker": "aaa", "grade": "a+", "thesis": "Intact", "what_happened": "w", "lesson": "l"},
                   {"ticker": "BBB", "grade": "D", "thesis": "weakened", "what_happened": "w2", "lesson": "l2"},
                   {"grade": "A"}],
        "process": "p", "mistakes": ["m1", 5], "keep_doing": ["k1"], "rule_changes": ["r1"], "summary": "S."},
        {"model": "stand-in", "usage": {}, "api_usd": None, "ms": 1, "session": None})
    _rid = _ka268.review({}, "monthly", _d268(2026, 10, 28), _d268(2026, 11, 30))
    _rv = _ka268.review_one(_rid)
    _b = _rv["body"]
    _lb = "\n".join(_ka268.last_review_block())
    _r268["run"] = (_rv["month"], _rv["kind"], [(g["ticker"], g["grade"], g["thesis"]) for g in _b["grades"]], _b["mistakes"],
                    _rv["material"].startswith("DATE:"), _ka268._rows("SELECT COUNT(*) AS n FROM kai_order")[0]["n"],
                    "Summary: S." in _lb and "- r1" in _lb and "BBB (D, weakened): l2" in _lb,
                    _ka268.review_due(_A(2026, 12, 12, 11, 0)), _ka268.review_period(_d268(2026, 12, 31))[0])
    # the scheduler starts it when it is due and nothing else is
    _calls = []
    _ka268._remember_reports = lambda: None
    _ka268.fill_pending = lambda now=None: []
    _ka268._may_run = lambda: True
    _ka268.first_due = lambda now=None: False
    _ka268.weekly_due = lambda now=None: False
    _ka268.event_due = lambda now=None: None
    _ka268.review_due = lambda now=None: (_d268(2026, 12, 1), _d268(2026, 12, 31))
    _ka268.start_review = lambda kind="manual", d0=None, d1=None: _calls.append((kind, d0, d1))
    _ka268._tick()
    _r268["tick"] = _calls
    _paths = {r.path for r in _api268.router.routes}
    _kj = open(os.path.join(HERE, "backend", "static", "kai.js"), encoding="utf-8").read()
    _kh = open(os.path.join(HERE, "backend", "static", "kai.html"), encoding="utf-8").read()
    _r268["wired"] = ({"/api/pf/kai/review", "/api/pf/kai/review/{rid}"} <= _paths and "reviewsHtml()" in _kj and '"/kai/review"' in _kj
                      and "confirm(\"Ask Koers AI for a self-review" in _kj and ".gr .g.A" in _kh
                      and "CREATE TABLE IF NOT EXISTS kai_review (" in open(os.path.join(HERE, "backend", "pf", "store.py"), encoding="utf-8").read()
                      and _BUILD >= 56)
    _ok268 = (_r268["due"] == (None, None, (_d268(2026, 10, 28), _d268(2026, 11, 30)), None, True, "2026-12-05T10:00+01:00",
                               "2026-12-12T10:00+01:00")
              and all(_r268["material"])
              and _r268["run"] == ("2026-11", "monthly", [("AAA", "A", "intact"), ("BBB", "D", "weakened")], ["m1"], True, 0, True, None,
                                   _d268(2026, 12, 1))
              and _r268["tick"] == [("monthly", _d268(2026, 12, 1), _d268(2026, 12, 31))] and _r268["wired"])
except Exception as _e268:
    import traceback as _tb268
    _r268["error"] = f"{_e268.__class__.__name__}: {_e268}"[:200] + _tb268.format_exc()[-400:]
finally:
    (store.DB_PATH, _ka268.started, _ka268.curve, _ka268.bars, _ka268._ccy_of, _ka268.earn_dates, _rd268.run_json, _rd268.writer_ready,
     _ka268.start_review, _ka268._remember_reports, _ka268.fill_pending, _ka268._may_run, _ka268.first_due, _ka268.weekly_due,
     _ka268.event_due, _ka268.review_due) = _keep268[:16]
    _ka268.JOB.clear(); _ka268.JOB.update(_keep268[16])
    _sh268.rmtree(_dir268, ignore_errors=True)
print(f"268 Koers AI reviews itself  {ok(_ok268)}  the first Saturday of a month from 10:00, at weekends, once; a period under ten "
      f"sessions waits; theses graded, no orders, the next turns read it; turnover the lesser of bought and sold")
if not _ok268:
    print("     ", _r268)

# 269 NVIDIA's EPS and the writer's model (build 57). On 17 November 2026 the earnings page showed NVIDIA's EPS as
#     $115.80: its summary table opens with "($ in billions, except earnings per share)", the parser took that caption
#     for an EPS heading, and the first row under it was revenue. A heading now names only the row directly under it
#     (Verizon's "Diluted" over its net income line still reads), a unit caption is no heading, a row that says EPS
#     wins, and a figure 15 times the consensus is dropped as a misread. A live record read by an older parser is read
#     again. And every write-up is Claude Opus 5.5 at high effort: Claude Code left on "your plan's default" had run
#     a model the user's plan does not have
import tempfile as _tf269
import shutil as _sh269
import json as _js269
from pf import earndocs as _ed269, earnai as _ea269, earnlive as _el269, earnings as _en269
_r269 = {}
_ok269 = False
_dir269 = _tf269.mkdtemp(prefix="pf_eps263")
_keep269 = (store.DB_PATH, _en269.release_parsed)
try:
    def _tbl(kind, eps):
        return (f"<table><tr><td>{kind}</td></tr><tr><td>($ in billions, except earnings per share)</td><td>Q3 FY27</td><td>Q2 FY27</td>"
                f"<td>Q3 FY26</td></tr><tr><td>Revenue</td><td>$115.8</td><td>$96.2</td><td>$57.0</td></tr>"
                f"<tr><td>Gross margin</td><td>74.8</td><td>75.0</td><td>73.4</td></tr><tr><td>Net income</td><td>$65.3</td><td>$59.7</td>"
                f"<td>$31.9</td></tr><tr><td>Diluted earnings per share</td><td>${eps}</td><td>$2.46</td><td>$1.30</td></tr></table>")
    _nv = ("<html><body><p>NVIDIA today reported revenue for the third quarter ended October 25, 2026, of $115.8 billion.</p>"
           + _tbl("GAAP", "2.70") + _tbl("Non-GAAP", "2.68") + "</body></html>").encode()
    _p = _ed269.parse_release([_nv], 108.7e9, 2.4707)
    _p0 = _ed269.parse_release([_nv])
    _vz = (b"<html><body><table><tr><td></td><td>3 Months Ended 3/31/26</td><td>3 Months Ended 3/31/25</td></tr>"
           b"<tr><td>Revenue</td><td>34,000</td><td>33,500</td></tr><tr><td>Diluted</td></tr>"
           b"<tr><td>Net income attributable to Verizon</td><td>0.92</td><td>1.15</td></tr></table></body></html>")
    _pv = _ed269.parse_release([_vz])
    # a misread a table gives no other way to catch: no row says EPS, and the caption is the only "heading"
    _bad = ("<html><body><table><tr><td>($ in millions, except per share data)</td><td>Q3 2026</td><td>Q3 2025</td></tr>"
            "<tr><td>Revenue</td><td>$41.5</td><td>$38.2</td></tr></table></body></html>").encode()
    _pb = _ed269.parse_release([_bad], None, 1.10)
    # an EPS heading names only the row under it: here a share count (skipped), and revenue further down is not EPS
    _far = (b"<html><body><table><tr><td></td><td>Q3 2026</td><td>Q3 2025</td></tr><tr><td>Net income per share</td></tr>"
            b"<tr><td>Weighted average diluted shares</td><td>2,450</td><td>2,500</td></tr>"
            b"<tr><td>Revenue</td><td>41.5</td><td>38.2</td></tr></table></body></html>")
    _pf = _ed269.parse_release([_far])
    _r269["eps"] = ((_p["eps_gaap"] or {}).get("value"), (_p["eps_adj"] or {}).get("value"), _p["eps_basis"],
                    (_p0["eps_gaap"] or {}).get("value"), (_p0["eps_adj"] or {}).get("value"),
                    (_pv["eps_gaap"] or {}).get("value"), _pb["eps_gaap"], _pb["eps_adj"], _pf["eps_gaap"])
    # the guard on its own: a table figure fifteen times the consensus
    _orig = _ed269._eps_pick
    _ed269._eps_pick = lambda t, adjusted=False, cols=None: (None if adjusted else {"label": "revenue", "value": 115.8, "year_ago": 57.0,
                                                                                    "forecast": None, "row": 2})
    try:
        _pg = _ed269.parse_release([_nv], None, 2.4707)
    finally:
        _ed269._eps_pick = _orig
    _r269["guard"] = (_pg["eps_gaap"] is None or _pg["eps_gaap"]["value"] != 115.8, any("consensus: left out" in n for n in _pg["notes"]))
    # a live record from an older build is read again
    store.DB_PATH = os.path.join(_dir269, "e.db")
    store.init_db()
    _en269.release_parsed = lambda sym, d, name=None, max_age=0: {
        "ok": True, "parsed": {"revenue": {"value": 115.8e9, "yoy": 103.2}, "eps_adj": {"value": 2.68, "label": "diluted earnings per share"},
                               "eps_gaap": {"value": 2.70}, "eps_basis": "eps_adj", "currency": "USD"},
        "consensus": {"eps": 2.4707, "rev": 108.7e9}, "doc": {"url": "u"}, "filing": {"index": "i"}}
    with store.conn() as _c:
        _c.execute("INSERT INTO cache (k, v, fetched_at) VALUES (?, ?, 1)", ("ern:live:NVDA:2026-11-17", _js269.dumps(
            {"symbol": "NVDA", "found": {"t": 1}, "numbers": {"eps": 115.8, "eps_adj": 115.8, "read_s": 3.1}, "_build": 1})))
    _st = _el269.state("NVDA", "2026-11-17")
    _again = store.cache_get("ern:live:NVDA:2026-11-17")
    _r269["live"] = (_st["numbers"]["eps"], _st["numbers"]["eps_gaap"], _st["numbers"]["eps_surprise"], _st["numbers"]["read_s"],
                     _again.get("_build") == _BUILD, _again["numbers"]["eps"])
    # the writer: Opus 5.5 at high effort, on both paths, whatever a setting says
    store.set_setting("ern_model", "claude-fable-5-2")
    store.set_setting("ern_cc_model", "")
    _cp = _ea269.cc_path
    _ea269.cc_path = lambda: "claude.exe"
    try:
        _cmd = _ea269.cc_command("Read")
    finally:
        _ea269.cc_path = _cp
    _src = {m: open(os.path.join(HERE, "backend", "pf", f"{m}.py"), encoding="utf-8").read()
            for m in ("earnai", "morning", "oilnote", "supply", "watchnote", "reader", "weekly")}
    # nothing but earnai starts Claude Code or names a model: every writer goes through its pinned helpers
    import glob as _gl269
    import re as _re269
    _stray269 = []
    for _f269 in _gl269.glob(os.path.join(HERE, "backend", "pf", "*.py")):
        if os.path.basename(_f269) == "earnai.py":
            continue
        _t269 = open(_f269, encoding="utf-8").read()
        if "cc_path()" in _t269 or '"-p", "--output-format"' in _t269 or _re269.search(r'"claude-(fable|opus|sonnet|haiku|mythos)[\w-]*"', _t269):
            _stray269.append(os.path.basename(_f269))
    _r269["stray"] = sorted(_stray269)
    _r269["writer"] = (_ea269.model(), _ea269.cc_model(), _cmd[_cmd.index("--model") + 1], _cmd[_cmd.index("--effort") + 1],
                       _ea269.WRITE_EFFORT, [m for m, s in _src.items() if '"effort": "medium"' in s or '"effort": "low"' in s],
                       all("earnai.WRITE_EFFORT" in _src[m] for m in ("morning", "oilnote", "supply", "watchnote", "reader", "weekly")))
    _ok269 = (_r269["eps"] == (2.7, 2.68, "eps_adj", 2.7, 2.68, 0.92, None, None, None)
              and _r269["guard"] == (True, True)
              and _r269["live"] == (2.68, 2.7, 8.47, 3.1, True, 2.68)
              and _r269["writer"] == ("claude-opus-5-5", "claude-opus-5-5", "claude-opus-5-5", "high", "high", [], True)
              and _r269["stray"] == [])
except Exception as _e269:
    import traceback as _tb269
    _r269["error"] = f"{_e269.__class__.__name__}: {_e269}"[:200] + _tb269.format_exc()[-300:]
finally:
    store.DB_PATH, _en269.release_parsed = _keep269
    _sh269.rmtree(_dir269, ignore_errors=True)
print(f"269 NVIDIA's EPS, the writer  {ok(_ok269)}  $2.70 and $2.68 under a unit caption, not the $115.8bn revenue; Verizon's "
      f"heading still read; a figure 15× the consensus dropped; an old live record read again; Opus 5.5 at high effort everywhere")
if not _ok269:
    print("     ", _r269)
