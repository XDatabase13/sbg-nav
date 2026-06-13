import sys
import json
import yfinance as yf
import pandas as pd
from datetime import datetime, timezone, date as _date

sys.stdout.reconfigure(encoding="utf-8")

OUTPUT_PATH  = "data.json"
BASE_DATE    = "2026-03-31"
START_FETCH  = "2026-03-28"   # 3/31を確実に含むよう少し前から
TODAY        = _date.today().strftime("%Y-%m-%d")
TICKERS      = ["ARM", "9434.T", "9984.T", "USDJPY=X", "TMUS"]

# 固定パラメータ（3月末公式値）
BASE_ARM_VALUE_TN   = 19.15
BASE_SBKK_VALUE_TN  = 2.85
BASE_TMUS_VALUE_TN  = 0.05
UNLISTED_TN         = 26.22   # SVF1+SVF2+LatAm+その他 合計（全期間固定）
NET_DEBT_TN         = 8.21
SHARES              = 5_698_923_701

UNLISTED_BREAKDOWN = {
    "svf2_tn":  17.19,
    "svf1_tn":   3.38,
    "latam_tn":  1.04,
    "other_tn":  4.61,
}

# フォールバック基準値（実取得できなかった場合）
FALLBACK = {
    "ARM":      151.0,
    "9434.T":   211.1,
    "USDJPY=X": 159.88,
    "TMUS":     210.03,
}

# ── データ取得 ────────────────────────────────────────────────────────────────
print(f"yfinance 取得中: {START_FETCH} → {TODAY} ...")
raw   = yf.download(TICKERS, start=START_FETCH, auto_adjust=False, progress=False)
close = raw["Close"].copy()

# 前日値で補完（米日祝日ズレ対応）→ 全NaN行（週末等）を除外
close = close.ffill()
close = close.dropna(how="all")

# ── 3月末基準値を実取得 ───────────────────────────────────────────────────────
def get_base(ticker: str) -> tuple[float, str]:
    rows = close[close.index.strftime("%Y-%m-%d") == BASE_DATE]
    if not rows.empty:
        v = rows[ticker].iloc[0]
        if pd.notna(v):
            return float(v), "実取得"
    return FALLBACK[ticker], "フォールバック"

base = {}
print(f"\n▼ 3月末基準値")
for t in ["ARM", "9434.T", "USDJPY=X", "TMUS"]:
    v, src = get_base(t)
    base[t] = v
    fb = FALLBACK[t]
    diff = f"  ← 指定値 {fb} と差: {v - fb:+.4f}" if abs(v - fb) > 0.001 else f"  (指定値 {fb} と一致)"
    print(f"  {t:12s} {v:.4f}  [{src}]{diff}")

# ── 1日分の②NAV計算 ──────────────────────────────────────────────────────────
def calc_nav(row) -> dict | None:
    arm_p    = row["ARM"]
    sbkk_p   = row["9434.T"]
    tmus_p   = row["TMUS"]
    usdjpy_p = row["USDJPY=X"]
    sbg_p    = row["9984.T"]

    if any(pd.isna(v) for v in [arm_p, sbkk_p, tmus_p, usdjpy_p]):
        return None

    arm_val  = BASE_ARM_VALUE_TN  * (float(arm_p)  / base["ARM"])      * (float(usdjpy_p) / base["USDJPY=X"])
    sbkk_val = BASE_SBKK_VALUE_TN * (float(sbkk_p) / base["9434.T"])
    tmus_val = BASE_TMUS_VALUE_TN * (float(tmus_p) / base["TMUS"])     * (float(usdjpy_p) / base["USDJPY=X"])

    holdings      = arm_val + sbkk_val + tmus_val + UNLISTED_TN
    nav_tn        = holdings - NET_DEBT_TN
    nav_per_share = int(nav_tn * 1e12 / SHARES)
    discount_pct  = (
        round((1 - float(sbg_p) / nav_per_share) * 100, 1)
        if pd.notna(sbg_p) and nav_per_share else None
    )

    return {
        "nav_tn":        round(nav_tn, 4),
        "nav_per_share": nav_per_share,
        "discount_pct":  discount_pct,
        "arm_val_tn":    round(arm_val,  4),
        "sbkk_val_tn":   round(sbkk_val, 4),
        "tmus_val_tn":   round(tmus_val, 4),
        "holdings_tn":   round(holdings, 4),
    }

# ── timeseries 生成 ───────────────────────────────────────────────────────────
series = []
for dt in close.index:
    if dt.strftime("%Y-%m-%d") < BASE_DATE:
        continue
    row    = close.loc[dt]
    result = calc_nav(row)
    if result is None:
        continue
    series.append({
        "date":          dt.strftime("%Y-%m-%d"),
        "nav_tn":        result["nav_tn"],
        "nav_per_share": result["nav_per_share"],
        "sbg_price":     round(float(row["9984.T"]), 2) if pd.notna(row["9984.T"]) else None,
        "discount_pct":  result["discount_pct"],
        "arm":           round(float(row["ARM"]),      4),
        "sbkk":          round(float(row["9434.T"]),   2),
        "usdjpy":        round(float(row["USDJPY=X"]), 4),
    })

# ── スナップショット生成（円グラフ用） ────────────────────────────────────────
def make_snapshot(label: str, target_date: str) -> dict | None:
    candidates = close[close.index.strftime("%Y-%m-%d") >= target_date]
    if candidates.empty:
        return None
    dt  = candidates.index[0]
    row = candidates.iloc[0]
    r   = calc_nav(row)
    if r is None:
        return None
    return {
        "label":         label,
        "date":          dt.strftime("%Y-%m-%d"),
        "arm_tn":        r["arm_val_tn"],
        "sbkk_tn":       r["sbkk_val_tn"],
        "tmus_tn":       r["tmus_val_tn"],
        **UNLISTED_BREAKDOWN,
        "net_debt_tn":   NET_DEBT_TN,
        "holdings_tn":   r["holdings_tn"],
        "nav_tn":        r["nav_tn"],
        "nav_per_share": r["nav_per_share"],
        "sbg_price":     round(float(row["9984.T"]), 2) if pd.notna(row["9984.T"]) else None,
    }

latest_date = series[-1]["date"] if series else TODAY
snapshots = [
    make_snapshot("mar31",  "2026-03-31"),
    make_snapshot("may12",  "2026-05-12"),
    make_snapshot("latest", latest_date),
]
snapshots = [s for s in snapshots if s is not None]

# ── data.json に書き込み ──────────────────────────────────────────────────────
with open(OUTPUT_PATH, encoding="utf-8") as f:
    data = json.load(f)

data["timeseries"]              = series
data["snapshots"]               = snapshots
data["timeseries_generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
data["timeseries_base"]         = {
    "as_of":          BASE_DATE,
    "arm_price_usd":  base["ARM"],
    "sbkk_price_jpy": base["9434.T"],
    "tmus_price_usd": base["TMUS"],
    "usdjpy":         base["USDJPY=X"],
    "arm_value_tn":   BASE_ARM_VALUE_TN,
    "sbkk_value_tn":  BASE_SBKK_VALUE_TN,
    "tmus_value_tn":  BASE_TMUS_VALUE_TN,
    "unlisted_tn":    UNLISTED_TN,
    "net_debt_tn":    NET_DEBT_TN,
    "shares":         SHARES,
}

with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

# ── 確認表示 ──────────────────────────────────────────────────────────────────
print(f"\n✓ {len(series)} 営業日分 → {OUTPUT_PATH} に保存")

def show_rows(label: str, rows: list):
    print(f"\n▼ {label}")
    for r in rows:
        d = r.get("discount_pct")
        print(f"  {r['date']}  NAV {r['nav_tn']}兆  "
              f"理想株価 ¥{r['nav_per_share']:,}  "
              f"SBG ¥{r.get('sbg_price') or 0:,.0f}  "
              f"DIS {d}%  ARM ${r['arm']}")

show_rows("先頭 3件", series[:3])
show_rows("5/12 付近", [r for r in series if "2026-05-09" <= r["date"] <= "2026-05-14"])
show_rows("最新 3件", series[-3:])

print("\n▼ スナップショット（円グラフ用）")
for s in snapshots:
    print(f"  [{s['label']:6s}] {s['date']}  "
          f"Arm {s['arm_tn']}兆  SBKK {s['sbkk_tn']}兆  "
          f"NAV {s['nav_tn']}兆  ¥{s['nav_per_share']:,}")
