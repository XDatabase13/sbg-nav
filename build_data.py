import sys
import json
import os
import yfinance as yf
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

OUTPUT_PATH   = "data.json"
OFFICIAL_PATH = "official_nav.json"

TICKERS = {
    "arm":    "ARM",
    "tmus":   "TMUS",
    "sbkk":   "9434.T",
    "sbg":    "9984.T",
    "usdjpy": "USDJPY=X",
}

def fetch_close(ticker: str) -> dict:
    try:
        hist = yf.Ticker(ticker).history(period="10d", auto_adjust=False)
        if hist.empty:
            return {"close": None, "prev_close": None, "date": None, "status": "failed"}
        valid = hist["Close"].dropna()
        if valid.empty:
            return {"close": None, "prev_close": None, "date": None, "status": "failed"}
        return {
            "close":      round(float(valid.iloc[-1]), 4),
            "prev_close": round(float(valid.iloc[-2]), 4) if len(valid) >= 2 else None,
            "date":       valid.index[-1].strftime("%Y-%m-%d"),
            "status":     "ok",
        }
    except Exception:
        return {"close": None, "prev_close": None, "date": None, "status": "failed"}

with open(OFFICIAL_PATH, encoding="utf-8") as f:
    official = json.load(f)

market = {}
for key, ticker in TICKERS.items():
    result = fetch_close(ticker)
    market[key] = {"ticker": ticker, **result}

failed_count  = sum(1 for v in market.values() if v["status"] == "failed")
overall_status = "complete" if failed_count == 0 else "partial"

# ── ①-a 公式確定NAV ─────────────────────────────────────────────────────────
official_nav_per_share = official["official_nav_per_share_jpy"]
sbg_price = market["sbg"]["close"]

official_a = {
    "nav_per_share_jpy": official_nav_per_share,
    "total_tn":          official.get("nav_total_tn",
                             round(official["holdings_total_jpy_tn"] - official["net_debt_jpy_tn"], 4)),
    "as_of":             official["as_of"],
}

discount_official = round((1 - sbg_price / official_nav_per_share) * 100, 1) if sbg_price else None

# ── ②半公式NAV計算 ──────────────────────────────────────────────────────────
comp    = official["components"]
listed  = comp["listed"]
fx_asof = official["as_of_fx_usdjpy"]
fx_now  = market["usdjpy"]["close"]

def semi_value(key):
    c     = listed[key]
    p_now = market[key]["close"]
    if p_now is None or fx_now is None:
        return None
    if c["currency"] == "USD":
        return c["value_tn_jpy"] * (p_now / c["price_asof"]) * (fx_now / fx_asof)
    return c["value_tn_jpy"] * (p_now / c["price_asof"])

arm_val  = semi_value("arm")
sbkk_val = semi_value("sbkk")
tmus_val = semi_value("tmus")

if None not in (arm_val, sbkk_val, tmus_val, fx_now):
    unlisted = comp["unlisted_total_tn_jpy"]
    net_debt = comp["net_debt_tn_jpy"]
    shares   = official["shares_outstanding"]

    holdings_tn        = arm_val + sbkk_val + tmus_val + unlisted
    semi_nav_tn        = holdings_tn - net_debt
    semi_nav_per_share = int(semi_nav_tn * 1e12 / shares)
    semi_discount_pct  = round((1 - sbg_price / semi_nav_per_share) * 100, 1) if sbg_price else None

    nav_breakdown = {
        "arm_value_tn_jpy":      round(arm_val,  4),
        "sbkk_value_tn_jpy":     round(sbkk_val, 4),
        "tmus_value_tn_jpy":     round(tmus_val, 4),
        "unlisted_tn_jpy":       unlisted,
        "holdings_total_tn_jpy": round(holdings_tn, 4),
        "net_debt_tn_jpy":       net_debt,
        "semi_nav_tn_jpy":       round(semi_nav_tn, 4),
    }
else:
    semi_nav_tn        = None
    semi_nav_per_share = None
    semi_discount_pct  = None
    nav_breakdown      = None

# ── 既存 timeseries/snapshots/official_points を保持 ─────────────────────────
existing = {}
if os.path.exists(OUTPUT_PATH):
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            existing = json.load(f)
    except Exception:
        pass

# ── 出力 ────────────────────────────────────────────────────────────────────
output = {
    "generated_at":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "reference_note": "上場株は当日終値、東証銘柄は直近営業日終値。基準は米国市場クローズ後を想定。",
    "status":         overall_status,
    "market":         market,
    "nav": {
        "official_a": official_a,
        "official_b": official.get("official_b"),
        "semi": {
            "nav_per_share_jpy": semi_nav_per_share,
            "nav_tn":            round(semi_nav_tn, 4) if semi_nav_tn else None,
            "sbg_price_jpy":     sbg_price,
            "discount_pct":      semi_discount_pct,
        },
        # backward-compat keys（旧index.html・スクリプト向け）
        "official_nav_per_share_jpy": official_nav_per_share,
        "sbg_actual_price_jpy":       sbg_price,
        "discount_pct":               discount_official,
        "semi_nav_per_share_jpy":     semi_nav_per_share,
        "semi_discount_pct":          semi_discount_pct,
    },
    "openai":       official.get("openai"),
    "nav_breakdown": nav_breakdown,
    "official":     official,
    # 既存タイムシリーズを保持（build_history.py が生成）
    "timeseries":              existing.get("timeseries"),
    "snapshots":               existing.get("snapshots"),
    "official_points":         existing.get("official_points"),
    "timeseries_generated_at": existing.get("timeseries_generated_at"),
    "timeseries_base":         existing.get("timeseries_base"),
}

with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"--- 書き出し完了: {OUTPUT_PATH} ---")
print(f"② 理想株価: ¥{semi_nav_per_share:,}" if semi_nav_per_share else "② 計算失敗")
print(f"② ディスカウント: {semi_discount_pct}%" if semi_discount_pct else "")
print(f"①-a ディスカウント: {discount_official}%" if discount_official else "")
print(f"SBG終値: ¥{sbg_price:,}  前日比: " +
      (f"¥{sbg_price - market['sbg']['prev_close']:+,.0f}" if market["sbg"]["prev_close"] else "—"))
