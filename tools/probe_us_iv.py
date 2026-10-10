"""IVが極端に出る銘柄の板を生のまま見るための使い捨てスクリプト。"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import us_fetch as uf
import us_options as uo

SYMS = ["T", "VZ", "TMUS", "V", "AMT", "NVDA"]

bars_map = uf.fetch_bars_many(SYMS)
asof = uf.board_date_from_clock()
print("基準日", asof)

for sym in SYMS:
    bars = bars_map.get(sym) or []
    if not bars:
        print(f"\n### {sym}: 日足なし")
        continue
    close = bars[-1].close
    print(f"\n### {sym}  日足終値 {close:.2f}（{bars[-1].date}）")
    exps = uf.fetch_expiries(sym, asof)
    for e in exps:
        dte = e.dte(asof)
        t = e.t(asof)
        f = uo.implied_forward(e, close, asof)
        iv = uo.atm_iv(e, f, t)
        mark = " ←対象" if uo.MIN_DTE <= dte <= uo.MAX_DTE else ""
        print(f"  {e.expiry} DTE{dte:3d} t={t:.4f} 行使価格{len(e.rows):4d} "
              f"F={f:.2f} ATM IV={iv}{mark}")
        if not (uo.MIN_DTE <= dte <= uo.MAX_DTE):
            continue
        near = sorted(e.rows, key=lambda r: abs(r.strike - f))[:4]
        for r in near:
            for q, is_call in ((r.call, True), (r.put, False)):
                v = uo.iv_of(q, f, r.strike, t, is_call)
                print(f"      K={r.strike:8.2f} {'C' if is_call else 'P'} "
                      f"bid={q.bid} ask={q.ask} last={q.last} mid={q.mid} "
                      f"OI={q.oi} vol={q.volume} "
                      f"IV={'—' if v is None else format(v * 100, '.1f')}")
