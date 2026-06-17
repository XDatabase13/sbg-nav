import sys
import json
import os
import time
import yfinance as yf
from datetime import datetime, timezone, timedelta, date as _date

JST = timezone(timedelta(hours=9))

sys.stdout.reconfigure(encoding="utf-8")

# ── 設定値（後から調整しやすいよう上部にまとめる） ─────────────────────────────
RETRY_MAX      = 3     # リトライ上限回数
RETRY_INTERVAL = 300   # リトライ間隔（秒）= 5分
CHANGE_LIMIT   = 0.20  # 前日比変動がこの割合超で異常と判定（0.20 = ±20%）
# ★ 株式分割・大型TOB等のコーポレートアクション時は誤検知になりうる。
#    その際は CHANGE_LIMIT を一時的に引き上げるか data.json を手動修正すること。
STALE_DAYS     = 5     # 取得日付が本日から何日を超えるとデータが古いとみなすか

OUTPUT_PATH   = "data.json"
OFFICIAL_PATH = "official_nav.json"

TICKERS = {
    "arm":    "ARM",
    "tmus":   "TMUS",
    "sbkk":   "9434.T",
    "sbg":    "9984.T",
    "usdjpy": "USDJPY=X",
}


def _fetch_once(ticker: str) -> dict:
    """yfinanceから終値を1回取得する（リトライなし）。"""
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
    except Exception as e:
        print(f"    取得例外: {e}")
        return {"close": None, "prev_close": None, "date": None, "status": "failed"}


def fetch_close(key: str, ticker: str) -> dict:
    """
    リトライ付き終値取得。
    失敗時は RETRY_INTERVAL 秒待って最大 RETRY_MAX 回再試行。
    全試行失敗なら status="failed" を返す。
    """
    for attempt in range(1, RETRY_MAX + 1):
        result = _fetch_once(ticker)
        if result["status"] == "ok":
            if attempt > 1:
                print(f"  [{key}] 試行 {attempt} 回目で成功")
            return result
        if attempt < RETRY_MAX:
            print(f"  [{key}] 取得失敗 ({attempt}/{RETRY_MAX}) → {RETRY_INTERVAL}秒後に再試行...")
            time.sleep(RETRY_INTERVAL)
        else:
            print(f"  [{key}] {RETRY_MAX}回試行後も失敗")
    return result


def validate(key: str, new: dict, prev_market: dict) -> tuple:
    """
    新しい取得値を検証し、異常なら前回値にフォールバックする。

    異常と判定する条件:
      1. 取得失敗（status != "ok"）
      2. 取得日付が STALE_DAYS 日以上前（古いデータが返ってきた）
      3. 前日比変動が CHANGE_LIMIT 超（急変動 = 誤取得の疑い）
         ※ 株式分割・大型コーポレートアクション時は誤検知になりうる

    戻り値: (採用する dict, "ok" | "stale" | "failed")
    """
    prev = {k: v for k, v in prev_market.get(key, {}).items() if k != "ticker"}

    # ── 取得失敗 ─────────────────────────────────────────────────────────────
    if new["status"] != "ok":
        if prev.get("close") is not None:
            print(f"  [{key}] 取得失敗 → 前回値 {prev['close']} を保持")
            return {**prev, "status": "stale"}, "stale"
        return new, "failed"

    new_close = new["close"]
    prev_day  = new.get("prev_close")  # yfinanceが返す前日終値（close[-2]）

    # ── 日付チェック ─────────────────────────────────────────────────────────
    if new.get("date"):
        age = (_date.today() - _date.fromisoformat(new["date"])).days
        if age > STALE_DAYS:
            print(f"  [{key}] 警告: データが {age}日前 ({new['date']}) → 前回値保持")
            if prev.get("close") is not None:
                return {**prev, "status": "stale"}, "stale"

    # ── 前日比変動チェック ───────────────────────────────────────────────────
    # yfinanceが返す前日終値(prev_close)を基準に変動率を計算する
    # ★ CHANGE_LIMIT を超えた場合は怪しい値として前回値を採用
    if prev_day is not None and new_close is not None:
        change = abs(new_close - prev_day) / prev_day
        if change > CHANGE_LIMIT:
            print(
                f"  [{key}] 警告: 前日比 {change*100:.1f}% "
                f"> 閾値 {CHANGE_LIMIT*100:.0f}% → 前回値保持"
            )
            if prev.get("close") is not None:
                return {**prev, "status": "stale"}, "stale"

    return new, "ok"


# ── 公式NAV読み込み ──────────────────────────────────────────────────────────
with open(OFFICIAL_PATH, encoding="utf-8") as f:
    official = json.load(f)

# ── 既存 data.json 読み込み（フォールバック用 & timeseries等保持用） ────────────
existing    = {}
prev_market = {}
if os.path.exists(OUTPUT_PATH):
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            existing = json.load(f)
        prev_market = existing.get("market", {})
    except Exception:
        pass

# ── 市場データ取得（リトライ + バリデーション） ──────────────────────────────
print("▼ 市場データ取得")
market   = {}
vstats   = []
for key, ticker in TICKERS.items():
    raw            = fetch_close(key, ticker)
    result, vstatus = validate(key, raw, prev_market)
    market[key]    = {"ticker": ticker, **result}
    vstats.append(vstatus)
    close_str = str(result["close"]) if result.get("close") is not None else "—"
    print(f"  {key:8s} {close_str:12s} [{vstatus}]  {result.get('date', '—')}")

if all(s == "ok" for s in vstats):
    overall_status = "complete"
elif all(s == "failed" for s in vstats):
    overall_status = "failed"
else:
    overall_status = "partial"

# ── ①-a 公式確定NAV ─────────────────────────────────────────────────────────
official_nav_per_share = official["official_nav_per_share_jpy"]
sbg_price = market["sbg"]["close"]

official_a = {
    "nav_per_share_jpy": official_nav_per_share,
    "total_tn":          official.get(
                             "nav_total_tn",
                             round(official["holdings_total_jpy_tn"] - official["net_debt_jpy_tn"], 4),
                         ),
    "as_of":             official["as_of"],
}

discount_official = round((1 - sbg_price / official_nav_per_share) * 100, 1) if sbg_price else None

# ── ② リアルタイムNAV計算 ───────────────────────────────────────────────────
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

# ── 出力 ────────────────────────────────────────────────────────────────────
output = {
    "generated_at":   datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
    "reference_note": "上場株は最新終値、東証銘柄は直近営業日終値。基準は米国市場クローズ後を想定。",
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
        "official_nav_per_share_jpy": official_nav_per_share,
        "sbg_actual_price_jpy":       sbg_price,
        "discount_pct":               discount_official,
        "semi_nav_per_share_jpy":     semi_nav_per_share,
        "semi_discount_pct":          semi_discount_pct,
    },
    "openai":        official.get("openai"),
    "nav_breakdown": nav_breakdown,
    "official":      official,
    # timeseries系は build_history.py が生成するためそのまま保持
    "timeseries":              existing.get("timeseries"),
    "snapshots":               existing.get("snapshots"),
    "official_points":         existing.get("official_points"),
    "timeseries_generated_at": existing.get("timeseries_generated_at"),
    "timeseries_base":         existing.get("timeseries_base"),
}

with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"\n--- 書き出し完了: {OUTPUT_PATH} ({overall_status}) ---")
print(f"② 理論株価: ¥{semi_nav_per_share:,}" if semi_nav_per_share else "② 計算失敗")
print(f"② ディスカウント: {semi_discount_pct}%" if semi_discount_pct else "")
if sbg_price and market["sbg"].get("prev_close"):
    diff = sbg_price - market["sbg"]["prev_close"]
    print(f"SBG終値: ¥{sbg_price:,}  前日比: ¥{diff:+,.0f}")
