"""実データで取得層が動くかを確かめるだけの使い捨てスクリプト。"""
import sys
from datetime import date
sys.path.insert(0, "/home/user/market-dashboard")
import us_fetch as uf, us_options as uo, us_report as ur

syms = ("NVDA", "AAPL", "SPY", "QQQ", "TSLA", "AMD", "MU", "SMH")
print("=== 決算カレンダー ===")
em = uf.fetch_earnings_map()
print("件数", len(em), "| 例:", {k: em[k] for k in list(em)[:5]})
for s in syms:
    print(" ", s, em.get(s))

print("\n=== 日足 ===")
for s in syms[:3]:
    b = uf.fetch_bars(s)
    print(f"  {s}: {len(b)}本 最終 {b[-1].date if b else '—'} 終値 {b[-1].close if b else '—'}")

print("\n=== 板 ===")
b = uf.fetch_bars("NVDA")
asof = date.fromisoformat(b[-1].date)
print("  基準日", asof)
exps = uf.fetch_expiries("NVDA", asof)
for e in exps:
    oi = sum(r.call.oi + r.put.oi for r in e.rows)
    tradable = sum(1 for r in e.rows for q in (r.call, r.put) if q.tradable(True))
    print(f"  {e.expiry} DTE{e.dte(asof):3d} 行使価格{len(e.rows):4d} 建玉計{oi:>9,} 約定可{tradable:4d}")
    f = uo.implied_forward(e, b[-1].close, asof)
    print(f"      フォワード {f:.2f}（現値 {b[-1].close:.2f}） ATM IV "
          f"{uo.atm_iv(e, f, e.t(asof))}")

u = uo.Underlying(symbol="NVDA", bars=b, expiries=exps, earnings=em.get("NVDA"))
t, found = uo.screen_symbol(u, asof)
print(f"\n  テクニカル: 乖離{t.dev_atr:.2f}ATR RSI{t.rsi:.1f} {t.gc_state} "
      f"MAD{t.mad_vol:.1f} HV20ex{t.hv20ex:.1f} モメンタム{t.momentum.score}/5")
g = uo.build_gex(exps, u.spot, asof)
print(f"  GEX: CALLウォール {g.call_wall} PUTウォール {g.put_wall} "
      f"フリップ {None if g.flip is None else round(g.flip,2)} "
      f"現値GEX {g.at_spot/1e6:.1f}M")
print("  候補:", {k: len(v) for k, v in found.items()})
