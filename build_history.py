import sys
import json
import yfinance as yf
import pandas as pd
from datetime import datetime, timezone, date as _date, timedelta

sys.stdout.reconfigure(encoding="utf-8")

OUTPUT_PATH   = "data.json"
OFFICIAL_PATH = "official_nav.json"
TICKERS       = ["ARM", "9434.T", "9984.T", "USDJPY=X", "TMUS"]

# ── 公式NAV（epochs配列）読み込み ────────────────────────────────────────────
with open(OFFICIAL_PATH, encoding="utf-8") as f:
    _nav_file = json.load(f)

nav_epochs = _nav_file["epochs"]


def resolve_nav_epoch(nav_epochs: list, date_str: str) -> dict | None:
    """valid_from <= date_str を満たす最新エポックを返す。"""
    valid = [e for e in nav_epochs if e["valid_from"] <= date_str]
    if not valid:
        return None
    return max(valid, key=lambda e: e["valid_from"])


# ── 日付範囲をepochsから導出 ──────────────────────────────────────────────────
_first_date = min(_date.fromisoformat(e["valid_from"]) for e in nav_epochs)
BASE_DATE   = _first_date.isoformat()
START_FETCH = (_first_date - timedelta(days=3)).isoformat()
TODAY       = _date.today().strftime("%Y-%m-%d")

# ── データ取得 ────────────────────────────────────────────────────────────────
print(f"yfinance 取得中: {START_FETCH} → {TODAY} ...")
raw   = yf.download(TICKERS, start=START_FETCH, auto_adjust=False, progress=False)
close = raw["Close"].copy()

# ── quote補追 ────────────────────────────────────────────────────────────────
# 2026-07-06頃からYahooのチャートAPIが東証銘柄で「引け後〜翌営業日の反映まで」
# 直近セッションの日足バーを返さなくなった。チャートAPIメタ（quote相当）は
# 常に直近約定を持つので、日足の終端より新しい日付があれば行を補追する。
print("▼ quote補追チェック")
for _tkr in TICKERS:
    try:
        _t = yf.Ticker(_tkr)
        _t.history(period="5d")
        _meta  = _t.get_history_metadata()
        _price = _meta.get("regularMarketPrice")
        _epoch = _meta.get("regularMarketTime")
        _tz    = _meta.get("exchangeTimezoneName")
        if _price is None or _epoch is None or _tz is None:
            continue
        _q_str    = str(pd.Timestamp(_epoch, unit="s", tz="UTC").tz_convert(_tz).date())
        _series   = close[_tkr].dropna()
        if _series.empty:
            continue
        _last_str = _series.index[-1].strftime("%Y-%m-%d")
        if _q_str > _last_str and float(_price) > 0:
            _new_idx = (pd.Timestamp(_q_str, tz=close.index.tz)
                        if close.index.tz is not None else pd.Timestamp(_q_str))
            close.loc[_new_idx, _tkr] = float(_price)
            print(f"  [補追] {_tkr}: {_q_str} = {_price}（quote、日足は{_last_str}止まり）")
    except Exception as _ex:
        print(f"  [補追失敗] {_tkr}: {_ex}")
close = close.sort_index()

# 前日値で補完（米日祝日ズレ対応）→ 全NaN行（週末等）を除外
close = close.ffill()
close = close.dropna(how="all")


# ── エポック別基準値の確定（実取得 or price_asofへのフォールバック） ──────────
def _pick_base(date_str: str, ticker: str, fallback: float) -> tuple[float, str]:
    """指定日付の終値を取得し、失敗時はfallbackを返す。"""
    rows = close[close.index.strftime("%Y-%m-%d") == date_str]
    if not rows.empty:
        v = rows[ticker].iloc[0]
        if pd.notna(v):
            return float(v), "実取得"
    return fallback, "フォールバック"


epoch_bases: dict[str, dict[str, float]] = {}

print("\n▼ エポック別基準値")
for e in nav_epochs:
    vf     = e["valid_from"]
    listed = e["components"]["listed"]
    fb_fx  = e["as_of_fx_usdjpy"]

    arm_b,  arm_src  = _pick_base(vf, "ARM",      listed["arm"]["price_asof"])
    sbkk_b, sbkk_src = _pick_base(vf, "9434.T",   listed["sbkk"]["price_asof"])
    tmus_b, tmus_src = _pick_base(vf, "TMUS",     listed["tmus"]["price_asof"])
    fx_b,   fx_src   = _pick_base(vf, "USDJPY=X", fb_fx)

    epoch_bases[vf] = {
        "ARM":      arm_b,
        "9434.T":   sbkk_b,
        "TMUS":     tmus_b,
        "USDJPY=X": fx_b,
    }
    print(f"  [{vf}]  ARM ${arm_b:.4f}[{arm_src}]  "
          f"SBKK ¥{sbkk_b:.2f}[{sbkk_src}]  "
          f"TMUS ${tmus_b:.4f}[{tmus_src}]  "
          f"USDJPY {fx_b:.4f}[{fx_src}]")


# ── 1日分の②NAV計算 ──────────────────────────────────────────────────────────
def calc_nav(row, epoch: dict) -> dict | None:
    arm_p    = row["ARM"]
    sbkk_p   = row["9434.T"]
    tmus_p   = row["TMUS"]
    usdjpy_p = row["USDJPY=X"]
    sbg_p    = row["9984.T"]

    if any(pd.isna(v) for v in [arm_p, sbkk_p, tmus_p, usdjpy_p]):
        return None

    listed      = epoch["components"]["listed"]
    base        = epoch_bases[epoch["valid_from"]]
    unlisted_tn = epoch["components"]["unlisted_total_tn_jpy"]
    net_debt_tn = epoch["components"]["net_debt_tn_jpy"]
    shares      = epoch["shares_outstanding"]

    arm_val  = listed["arm"]["value_tn_jpy"]  * (float(arm_p)  / base["ARM"])      * (float(usdjpy_p) / base["USDJPY=X"])
    sbkk_val = listed["sbkk"]["value_tn_jpy"] * (float(sbkk_p) / base["9434.T"])
    tmus_val = listed["tmus"]["value_tn_jpy"] * (float(tmus_p) / base["TMUS"])     * (float(usdjpy_p) / base["USDJPY=X"])

    holdings      = arm_val + sbkk_val + tmus_val + unlisted_tn
    nav_tn        = holdings - net_debt_tn
    nav_per_share = int(nav_tn * 1e12 / shares)
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
    date_str = dt.strftime("%Y-%m-%d")
    if date_str < BASE_DATE:
        continue
    epoch = resolve_nav_epoch(nav_epochs, date_str)
    if epoch is None:
        continue
    row    = close.loc[dt]
    result = calc_nav(row, epoch)
    if result is None:
        continue
    series.append({
        "date":          date_str,
        "nav_epoch":     epoch["valid_from"],
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
    dt    = candidates.index[0]
    row   = candidates.iloc[0]
    d_str = dt.strftime("%Y-%m-%d")
    epoch = resolve_nav_epoch(nav_epochs, d_str)
    if epoch is None:
        return None
    r = calc_nav(row, epoch)
    if r is None:
        return None
    bd = epoch["components"].get("unlisted_breakdown", {})
    return {
        "label":         label,
        "date":          d_str,
        "arm_tn":        r["arm_val_tn"],
        "sbkk_tn":       r["sbkk_val_tn"],
        "tmus_tn":       r["tmus_val_tn"],
        **bd,
        "net_debt_tn":   epoch["components"]["net_debt_tn_jpy"],
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

_base_epoch = next(e for e in nav_epochs if e["valid_from"] == BASE_DATE)
_base_b     = epoch_bases[BASE_DATE]

data["timeseries"]              = series
data["snapshots"]               = snapshots
data["timeseries_generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
data["timeseries_base"]         = {
    "as_of":          BASE_DATE,
    "arm_price_usd":  _base_b["ARM"],
    "sbkk_price_jpy": _base_b["9434.T"],
    "tmus_price_usd": _base_b["TMUS"],
    "usdjpy":         _base_b["USDJPY=X"],
    "arm_value_tn":   _base_epoch["components"]["listed"]["arm"]["value_tn_jpy"],
    "sbkk_value_tn":  _base_epoch["components"]["listed"]["sbkk"]["value_tn_jpy"],
    "tmus_value_tn":  _base_epoch["components"]["listed"]["tmus"]["value_tn_jpy"],
    "unlisted_tn":    _base_epoch["components"]["unlisted_total_tn_jpy"],
    "net_debt_tn":    _base_epoch["components"]["net_debt_tn_jpy"],
    "shares":         _base_epoch["shares_outstanding"],
}

with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)


# ── 確認表示 ──────────────────────────────────────────────────────────────────
print(f"\n✓ {len(series)} 営業日分 → {OUTPUT_PATH} に保存")


def show_rows(label: str, rows: list):
    print(f"\n▼ {label}")
    for r in rows:
        d  = r.get("discount_pct")
        ep = r.get("nav_epoch", "?")
        print(f"  {r['date']}  epoch:{ep}  NAV {r['nav_tn']}兆  "
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
