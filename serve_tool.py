# -*- coding: utf-8 -*-
"""本地网页工具：查看 Joy 因子入选股票，并点开单只看规则和走势。"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DOCS = ROOT / "docs"
CACHE = ROOT / "cache"
PORT = 8771

sys.path.insert(0, str(ROOT))
from joy_factor import joy_factor
from screen_market import scan_market

FLAGS = [
    ("tide", "大级别向上", "收盘站上 160 日线，并且 160 日线在抬头"),
    ("screen", "三重滤网", "近端回踩过 40 日线，MACD 金叉后当天重新站上，10 日线转上"),
    ("m520", "520", "5 日线上穿 20 日线，回踩 20 日不破，再站回 5 日线"),
    ("bottom3", "底部三步曲", "下跌后窄幅蓄力，放量突破，随后缩量回踩不破"),
    ("attack3", "攻击性三步曲", "20 日生命线向上，放量突破后缩量回踩"),
    ("dragon", "神龙出海", "站稳前高压力，缩量回踩，MACD 在零轴上方金叉"),
    ("buy3", "类三买", "离开近 20 日中枢上沿后，回抽没有跌回去"),
    ("rule123", "123法则", "下跌过程中回踩不创新低，再突破前高"),
    ("pillar", "一柱擎天", "波动收窄之后，一根放量长阳突破前压"),
    ("beichi", "背驰", "价格创新低，DIF 不创新低，并且这段回到过零轴"),
    ("wave2", "二波", "第一波拉升后没有跌破起点，再次金叉"),
    ("vol_break", "放量突破", "收敛之后，突破当天量能达到 20 日均量的 3 倍"),
    ("distribution", "后段爆量", "一段大涨之后放量，按量能课扣分，不做出场信号"),
]


_CAP_CACHE: dict[str, tuple[float | None, float | None]] = {}
_SCAN_LOCK = threading.Lock()
_SCAN = {
    "running": False,
    "finished": False,
    "phase": "",
    "done": 0,
    "total": 0,
    "hits": 0,
    "failed": 0,
    "matches": 0,
    "asof": "",
    "error": "",
}


def _yi_to_yuan(value: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number * 1e8 if number > 0 else None


def fetch_market_caps(codes: list[str]) -> None:
    missing = [code for code in codes if code and code not in _CAP_CACHE]
    if not missing:
        return
    symbols = ",".join(("sh" if code.startswith("6") else "sz") + code for code in missing)
    url = "https://qt.gtimg.cn/q=" + symbols
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        text = urlopen(req, timeout=20).read().decode("gbk", "replace")
    except Exception:
        return
    for line in text.split(";"):
        parts = line.split("~")
        if len(parts) < 46:
            continue
        code = parts[2].strip()
        if code:
            _CAP_CACHE[code] = (_yi_to_yuan(parts[45]), _yi_to_yuan(parts[44]))


def scan_status() -> dict:
    with _SCAN_LOCK:
        return dict(_SCAN)


def start_scan() -> bool:
    with _SCAN_LOCK:
        if _SCAN["running"]:
            return False
        _SCAN.update(
            running=True,
            finished=False,
            phase="universe",
            done=0,
            total=0,
            hits=0,
            failed=0,
            matches=0,
            asof="",
            error="",
        )
    threading.Thread(target=_run_scan, daemon=True).start()
    return True


def _run_scan() -> None:
    def on_progress(state: dict) -> None:
        with _SCAN_LOCK:
            _SCAN.update(state)
            _SCAN["running"] = True

    try:
        payload = scan_market(force=True, on_progress=on_progress)
    except Exception:
        print(traceback.format_exc(), flush=True)
        with _SCAN_LOCK:
            _SCAN["running"] = False
            _SCAN["finished"] = True
            _SCAN["error"] = "这次没有拉完，列表还是上一次的结果"
        return
    _CAP_CACHE.clear()
    sync_pages()
    meta = payload.get("meta") or {}
    with _SCAN_LOCK:
        _SCAN["running"] = False
        _SCAN["finished"] = True
        _SCAN["phase"] = "done"
        _SCAN["matches"] = len(payload.get("rows") or [])
        _SCAN["asof"] = str(meta.get("asof") or "")
        _SCAN["error"] = ""


def attach_market_caps(rows: list[dict]) -> None:
    fetch_market_caps([str(row.get("code") or "") for row in rows])
    for row in rows:
        total, circulating = _CAP_CACHE.get(str(row.get("code") or ""), (None, None))
        row["total_mv"] = total
        row["float_mv"] = circulating


def load_board() -> dict:
    path = WEB / "results.json"
    if path.exists():
        return _without_star(json.loads(path.read_text(encoding="utf-8")))
    html = (WEB / "index.html").read_text(encoding="utf-8")
    marker = '<script id="payload" type="application/json">'
    start = html.find(marker)
    if start < 0:
        return {"meta": {}, "rows": []}
    start += len(marker)
    end = html.find("</script>", start)
    data = json.loads(html[start:end])
    return _without_star(data)


def _without_star(data: dict) -> dict:
    """688 开头是科创板，不放进列表，扫描数字里也不再计入。"""
    rows = [row for row in data.get("rows") or [] if not str(row.get("code") or "").startswith("688")]
    meta = dict(data.get("meta") or {})
    counts = dict(meta.get("counts") or {})
    star_n = sum(1 for _ in CACHE.glob("688*.csv"))
    if star_n and counts.get("6"):
        counts["6"] = max(0, int(counts["6"]) - star_n)
        if meta.get("scanned"):
            meta["scanned"] = max(0, int(meta["scanned"]) - star_n)
        meta["counts"] = counts
    return {"meta": meta, "rows": rows}


def load_history(code: str, fallback: pd.DataFrame) -> pd.DataFrame:
    """日K图用上市以来的不复权日线。筛选缓存仍是近端，避免整场重下。"""
    path = CACHE / f"{code}.hist.csv"
    last_day = str(fallback["date"].iloc[-1])[:10]
    if path.exists():
        hist = pd.read_csv(path).drop_duplicates("date").sort_values("date")
        if len(hist) >= len(fallback) and str(hist["date"].iloc[-1])[:10] >= last_day:
            return hist
    symbol = ("sh" if code.startswith("6") else "sz") + code
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen=10000"
    )
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        text = urlopen(req, timeout=20).read().decode("utf-8", "replace").strip()
        klines = json.loads(text) if text and text != "null" else []
        rows = [
            {
                "date": item["day"],
                "open": float(item["open"]),
                "close": float(item["close"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "volume": float(item["volume"]),
            }
            for item in klines
        ]
        if len(rows) >= len(fallback):
            hist = pd.DataFrame(rows).drop_duplicates("date").sort_values("date")
            hist.to_csv(path, index=False)
            return hist
    except Exception:
        pass
    return fallback


def _num(value, digits: int = 2):
    if pd.isna(value):
        return None
    return round(float(value), digits)


def chart_bars(frame: pd.DataFrame) -> list[dict]:
    close_s = pd.to_numeric(frame["close"], errors="coerce")
    prev_s = close_s.shift(1)
    ma_s = {n: close_s.rolling(n).mean() for n in (5, 10, 20, 40, 160)}
    dif_s = close_s.ewm(span=12, adjust=False).mean() - close_s.ewm(span=26, adjust=False).mean()
    dea_s = dif_s.ewm(span=9, adjust=False).mean()
    bars = []
    for i in range(len(frame)):
        row = frame.iloc[i]
        close = float(row.close)
        volume = float(row.volume)
        bars.append(
            {
                "date": str(row.date)[:10],
                "open": _num(row.open),
                "high": _num(row.high),
                "low": _num(row.low),
                "close": _num(close),
                "prev": _num(prev_s.iloc[i]),
                "volume": round(volume),
                "amount": round(volume * close),
                "ma5": _num(ma_s[5].iloc[i]),
                "ma10": _num(ma_s[10].iloc[i]),
                "ma20": _num(ma_s[20].iloc[i]),
                "ma40": _num(ma_s[40].iloc[i]),
                "ma160": _num(ma_s[160].iloc[i]),
                "dif": _num(dif_s.iloc[i], 3),
                "dea": _num(dea_s.iloc[i], 3),
            }
        )
    return bars


def kline_frame(code: str) -> pd.DataFrame | None:
    """公开页用本地已有日线。有更长的历史文件就用历史文件。"""
    paths = [CACHE / f"{code}.csv", CACHE / f"{code}.hist.csv"]
    chosen: pd.DataFrame | None = None
    for path in paths:
        if not path.exists():
            continue
        frame = pd.read_csv(path).drop_duplicates("date").sort_values("date")
        if chosen is None or len(frame) > len(chosen):
            chosen = frame
    return chosen


def stock_detail(code: str) -> dict:
    code = "".join(ch for ch in code if ch.isdigit())[:6]
    if code.startswith("688"):
        return {"ok": False, "code": code, "error": "688 开头的科创板不在列表里"}
    path = CACHE / f"{code}.csv"
    if not path.exists() or len(code) != 6:
        return {"ok": False, "code": code, "error": "本地没有这只股票的日线"}
    frame = load_history(code, pd.read_csv(path).sort_values("date"))
    scored = joy_factor(frame)
    last = scored.iloc[-1]
    prev = float(scored["close"].iloc[-2]) if len(scored) > 1 else float(last["close"])
    price = float(last["close"])
    bars = chart_bars(scored)
    flags = []
    for key, label, text in FLAGS:
        flags.append({"key": key, "label": label, "text": text, "on": bool(last[key])})
    return {
        "ok": True,
        "code": code,
        "date": str(frame["date"].iloc[-1]),
        "price": round(price, 2),
        "pct": round((price / prev - 1) * 100, 2) if prev else 0,
        "score": round(float(last["joy_score"]), 1),
        "entry": bool(last["joy_entry"]),
        "stop": None if pd.isna(last["stop_ref"]) else round(float(last["stop_ref"]), 2),
        "target": None if pd.isna(last["target_2r"]) else round(float(last["target_2r"]), 2),
        "flags": flags,
        "bars": bars,
        "total_mv": _CAP_CACHE.get(code, (None, None))[0],
        "float_mv": _CAP_CACHE.get(code, (None, None))[1],
    }


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>小葵的Ai</title>
<style>
  :root {
    color-scheme: light;
    --bg:#f4f2ee; --card:#fff; --ink:#1c1917; --muted:#78716c; --line:#e7e5e4;
    --up:#dc2626; --down:#15803d; --soft:#faf9f7;
  }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 "Segoe UI","Microsoft YaHei UI","PingFang SC",sans-serif; background:var(--bg); color:var(--ink); }
  header { position:sticky; top:0; z-index:5; background:rgba(244,242,238,.94); backdrop-filter:blur(12px); border-bottom:1px solid var(--line); padding:14px 22px 12px; }
  main { padding:14px 22px 36px; }
  h1 { margin:0; font-size:22px; font-weight:680; letter-spacing:-0.03em; }
  .brand { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  .refresh {
    background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:6px 12px; cursor:pointer; font-weight:650;
  }
  .refresh:disabled { opacity:.55; cursor:default; }
  #refreshMsg:empty { display:none; }
  #refreshMsg { margin:6px 0 0; font-size:12px; }
  .sub { color:var(--muted); margin:4px 0 0; font-size:13px; }
  .toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:0 0 14px; }
  button { font:inherit; color:var(--ink); }
  .seg { display:flex; flex-wrap:wrap; gap:2px; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:3px; }
  .seg button, .hotbtn {
    background:transparent; border:0; border-radius:9px; padding:7px 12px; cursor:pointer;
  }
  .seg button.active, .hotbtn.active { background:var(--ink); color:#fff; }
  .hotbtn { background:var(--card); border:1px solid var(--line); font-weight:650; }
  .hotbtn.active { border-color:var(--ink); }
  button:focus-visible { outline:2px solid #d6d3d1; outline-offset:1px; }
  .layout { display:grid; grid-template-columns:1fr; gap:14px; }
  @media (max-width:979px) {
    .layout.picked { display:flex; flex-direction:column; }
    .layout.picked .detail { order:-1; scroll-margin-top:120px; }
    svg.kline { width:100%; max-width:100%; height:240px; aspect-ratio:auto; }
  }
  @media (min-width:980px) {
    .layout { grid-template-columns: minmax(300px, 380px) minmax(0, 1fr); align-items:start; }
    .layout > section { max-height:calc(100vh - 168px); overflow:auto; padding-right:6px; }
    .detail { position:sticky; top:12px; max-height:calc(100vh - 24px); overflow:auto; }
  }
  #count { font-size:12px; margin:0 2px 8px; }
  .list { display:flex; flex-direction:column; gap:8px; }
  .card {
    display:block; text-align:left; width:100%; background:var(--card); border:1px solid var(--line);
    border-radius:12px; padding:12px 14px; cursor:pointer;
    box-shadow:0 1px 2px rgba(28,25,23,.04);
  }
  .card:hover { border-color:#d6d3d1; }
  .card.selected { border-color:var(--ink); box-shadow:0 0 0 1px var(--ink); }
  .row1, .row2, .id { display:flex; align-items:center; }
  .row1, .row2 { justify-content:space-between; gap:12px; }
  .id { gap:8px; min-width:0; max-width:100%; }
  .name { flex:0 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .code { font-weight:700; font-variant-numeric:tabular-nums; letter-spacing:-0.02em; }
  .score {
    flex:none; min-width:46px; text-align:center; font-variant-numeric:tabular-nums; font-weight:700;
    font-size:13px; padding:3px 8px; border-radius:8px; background:#f5f5f4; color:#57534e;
  }
  .score.hi { background:#fee2e2; color:#b91c1c; }
  .score.mid { background:#ffedd5; color:#c2410c; }
  .score.lo { background:#f5f5f4; color:#57534e; }
  .muted { color:var(--muted); }
  .up { color:var(--up); } .down { color:var(--down); }
  .tags { margin-top:8px; }
  .tag { display:inline-block; margin:0 6px 4px 0; padding:2px 8px; border-radius:999px; background:#faf7f2; border:1px solid #efe8dc; font-size:12px; color:#44403c; }
  .soe-note { margin-top:6px; font-size:12px; line-height:1.45; }
  .capline { margin-top:4px; font-size:12px; }
  .st { color:#9a3412; background:#fff7ed; border-radius:999px; padding:1px 7px; font-size:12px; }
  .detail { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:16px 16px 8px; min-height:220px; box-shadow:0 1px 2px rgba(28,25,23,.04); }
  .placeholder { padding:36px 12px; }
  .ph-title { margin:0 0 6px; font-size:18px; font-weight:680; }
  .detail-head { display:flex; gap:12px; align-items:flex-start; }
  .detail-title { display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-size:20px; font-weight:680; }
  .pill { display:inline-block; margin-left:6px; padding:1px 8px; border-radius:999px; font-size:12px; }
  .pill.yes { background:#ecfdf3; color:#166534; }
  .pill.no { background:#f5f5f4; color:#78716c; }
  .metrics { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:8px; margin:12px 0 4px; }
  .metrics div { background:var(--soft); border:1px solid var(--line); border-radius:10px; padding:8px 10px; }
  .metrics span { display:block; color:var(--muted); font-size:12px; }
  .metrics b { font-size:16px; font-weight:680; font-variant-numeric:tabular-nums; }
  .block-title { margin:16px 0 2px; font-size:12px; font-weight:650; letter-spacing:.06em; color:var(--muted); }
  .kline-wrap { position:relative; margin-top:8px; background:var(--soft); border:1px solid var(--line); border-radius:12px; padding:8px 4px 2px; min-height:180px; overflow:hidden; scroll-margin-top:96px; touch-action:none; }
  svg.kline { width:100%; max-width:100%; height:auto; aspect-ratio:640 / 232; display:block; cursor:grab; touch-action:none; }
  .kzoom { position:absolute; top:6px; right:6px; z-index:4; display:flex; flex-direction:column; gap:4px; }
  .kzoom button { width:34px; height:34px; padding:0; border-radius:8px; border:1px solid var(--line); background:rgba(255,255,255,.94); font-size:20px; line-height:1; cursor:pointer; }
  svg.kline.dragging { cursor:grabbing; }
  .kline-cap { margin:6px 2px 0; font-size:12px; }
  .kline-tip { position:absolute; z-index:3; width:max-content; max-width:calc(100% - 8px); padding:8px 10px; background:rgba(255,255,255,.98); border:1px solid var(--line); border-radius:10px; font-size:12px; line-height:1.4; pointer-events:none; box-shadow:0 10px 28px rgba(28,25,23,.12); }
  .kline-tip .kdate { font-weight:700; margin-bottom:4px; }
  .kline-cols { display:flex; gap:16px; }
  .kline-row { display:flex; justify-content:space-between; gap:12px; font-variant-numeric:tabular-nums; }
  .flag { padding:10px 0; border-top:1px solid var(--line); }
  .flag-h { display:flex; justify-content:space-between; align-items:center; gap:8px; }
  .flag b { font-weight:650; }
  .flag.off b, .flag.off .muted { color:#a8a29e; }
  .state { flex:none; font-size:12px; border-radius:999px; padding:1px 8px; }
  .flag.on .state { background:#ecfdf3; color:#166534; }
  .flag.off .state { background:#f5f5f4; color:#a8a29e; }
  .empty { color:var(--muted); padding:20px 4px; }
  @media (max-width:700px) {
    header, main { padding-left:14px; padding-right:14px; }
    .metrics { grid-template-columns:repeat(2, minmax(0,1fr)); }
  }
  @media (max-width:979px) {
    .kzoom { left:6px; right:auto; flex-direction:row; }
    .kline-tip { left:4px; top:46px; max-width:calc(100% - 8px); padding:4px 6px; font-size:11px; line-height:1.25; }
    .kline-tip .kline-cols { gap:8px; }
    .kline-tip .kline-row { gap:6px; }
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1>小葵的Ai</h1>
    <button class="refresh" id="refreshBtn" type="button">重新拉取</button>
  </div>
  <p class="sub" id="meta">正在读取筛选结果…</p>
  <p class="muted" id="refreshMsg"></p>
</header>
<main>
  <div class="toolbar">
    <div class="seg">
      <button class="tab active" data-prefix="all">全部</button>
      <button class="tab" data-prefix="6">沪A</button>
      <button class="tab" data-prefix="3">创业板</button>
      <button class="tab" data-prefix="0">深A主板</button>
    </div>
    <button class="hotbtn" id="hotbtn" type="button">高度吻合</button>
    <button class="hotbtn" id="soebtn" type="button">注入重组国资</button>
  </div>
  <div class="layout">
    <section>
      <div class="muted" id="count"></div>
      <div class="list" id="list"></div>
    </section>
    <aside class="detail" id="detail"><div class="placeholder"><p class="ph-title">选择一只股票</p><p class="muted">点左侧列表，这里显示命中规则和最近日K。</p></div></aside>
  </div>
</main>
<script>
let board = {meta:{}, rows:[]};
let prefix = "all";
let hotOnly = false;
let soeOnly = false;
const soeDeals = {
  "600156": "国资，重组审核中。控股股东兴湘集团，实控人湖南省国资委。拟购买易信科技 97.40%。",
  "002827": "国资。实控人西藏自治区国资委，控股股东所持股份拟无偿划转。",
  "300895": "国资，股权重组推进中。北京市国资委安排的股权优化，实控人仍在北京国资体系。",
  "300862": "重组预案。拟购买苏州岚创科技控股权，尽调审计评估尚未完成。大股东为自然人。",
  "600539": "重组待注册。上交所已审核通过购买利珀科技，还差证监会注册。大股东为自然人。",
  "601598": "国资。控股股东中国外运长航，实控人国务院国资委。",
  "603268": "重组已落地。2025 年已置入恒力重工，不是还在预期里的方案。"
};
let selected = "";
let chartBars = [];
let chartStart = 0;
let chartSpan = 110;
let localTool = false;
const CHART_SPAN_MIN = 16;
const CHART_SPAN_MAX = 900;

const listEl = document.getElementById("list");
const detailEl = document.getElementById("detail");
const countEl = document.getElementById("count");

function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function shown() {
  return board.rows.filter(r => {
    if (soeOnly) return !!soeDeals[r.code];
    if (hotOnly) return coreTags(r).length >= 2;
    return prefix === "all" || r.prefix === prefix;
  }).slice().sort((a, b) => b.score - a.score || a.code.localeCompare(b.code));
}
function coreTags(r) {
  return (r.tags || []).filter(t => t !== "大级别向上");
}
function renderList() {
  const rows = shown();
  const scope = soeOnly ? "注入重组国资" : hotOnly ? "高度吻合" : "当前";
  countEl.textContent = `${scope} ${rows.length} 只`;
  listEl.innerHTML = rows.map(r => {
    const pct = Number(r.pct);
    const cls = pct > 0 ? "up" : pct < 0 ? "down" : "";
    const sign = pct > 0 ? "+" : "";
    const tags = (r.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join("");
    const soeNote = soeDeals[r.code] ? `<div class="muted soe-note">${esc(soeDeals[r.code])}</div>` : "";
    const st = r.st ? `<span class="st">风险</span>` : "";
    const scoreCls = Number(r.score) >= 30 ? "hi" : Number(r.score) >= 22 ? "mid" : "lo";
    return `<button class="card ${r.code === selected ? "selected" : ""}" data-code="${r.code}">
      <div class="row1"><span class="id"><span class="code">${r.code}</span><span class="name">${esc(r.name)}</span><span class="score ${scoreCls}">${Number(r.score).toFixed(1)}</span>${st}</span></div>
      ${(r.total_mv || r.float_mv) ? `<div class="muted capline">总市值 ${capText(r.total_mv)} · 流通 ${capText(r.float_mv)}</div>` : ""}
      <div class="row2"><span class="muted">${esc(r.board)} · ${Number(r.price).toFixed(2)}</span><span class="${cls}">${sign}${pct.toFixed(2)}%</span></div>
      <div class="tags">${tags}</div>
      ${soeNote}
    </button>`;
  }).join("") || `<p class="empty">${soeOnly ? "这 70 只里没有注入、重组或国资大股东。" : hotOnly ? "没有高度吻合的股票。" : "这一栏没有符合入场条件的股票。"}</p>`;
  listEl.querySelectorAll(".card").forEach(btn => btn.onclick = () => {
    const row = board.rows.find(r => r.code === btn.dataset.code);
    openDetail(btn.dataset.code, row ? row.name : btn.dataset.code);
  });
}
function capText(n) {
  n = Number(n);
  if (!n) return "—";
  if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
  if (n >= 1e4) return (n / 1e4).toFixed(2) + "万";
  return String(Math.round(n));
}
function money(n) {
  n = Number(n) || 0;
  if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + "万";
  return String(Math.round(n));
}
function hands(shares) {
  const lots = (Number(shares) || 0) / 100;
  if (lots >= 10000) return (lots / 10000).toFixed(2) + "万手";
  return Math.round(lots) + "手";
}
function px2(v) { return v == null || Number.isNaN(Number(v)) ? "—" : Number(v).toFixed(2); }
function signed(v, digits) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  return (n > 0 ? "+" : "") + n.toFixed(digits);
}
function visibleSpan() {
  return Math.max(1, Math.min(chartSpan, chartBars.length));
}
function clampChartStart() {
  const count = visibleSpan();
  chartStart = Math.max(0, Math.min(chartStart, Math.max(0, chartBars.length - count)));
}
function zoomChart(nextSpan, anchorIndex) {
  const limit = Math.min(CHART_SPAN_MAX, chartBars.length);
  const span = Math.max(CHART_SPAN_MIN, Math.min(Math.round(nextSpan), Math.max(CHART_SPAN_MIN, limit)));
  if (span === chartSpan) return;
  const count = Math.min(span, chartBars.length);
  chartSpan = span;
  chartStart = Math.round(anchorIndex - count / 2);
  clampChartStart();
  paintChart();
}
function paintChart() {
  const svg = document.getElementById("kline-svg");
  const cap = document.getElementById("kline-cap");
  if (!svg || !chartBars.length) return;
  const count = visibleSpan();
  clampChartStart();
  const bars = chartBars.slice(chartStart, chartStart + count);
  const narrow = window.innerWidth < 980;
  const w = 640, priceH = 148, volH = narrow ? 112 : 56, gap = 14, padX = 6, padY = 8;
  const h = padY + priceH + gap + volH + 6;
  const slot = (w - padX * 2) / bars.length;
  const bw = Math.max(1.2, slot * 0.62);
  const min = Math.min(...bars.map(b => Number(b.low)));
  const max = Math.max(...bars.map(b => Number(b.high)));
  const span = (max - min) || 1;
  const maxAmt = Math.max(...bars.map(b => Number(b.amount) || 0), 1);
  const yPrice = v => padY + (1 - (v - min) / span) * priceH;
  const volTop = padY + priceH + gap;
  const parts = bars.map((b, i) => {
    const o = Number(b.open), c = Number(b.close), hi = Number(b.high), lo = Number(b.low);
    const up = c >= o;
    const color = up ? "#dc2626" : "#15803d";
    const x = padX + i * slot + slot / 2;
    const top = Math.min(yPrice(o), yPrice(c));
    const bh = Math.max(1, Math.abs(yPrice(c) - yPrice(o)));
    const amt = Number(b.amount) || 0;
    const vh = Math.max(amt > 0 ? 1 : 0, (amt / maxAmt) * volH);
    return `<line x1="${x.toFixed(1)}" y1="${yPrice(hi).toFixed(1)}" x2="${x.toFixed(1)}" y2="${yPrice(lo).toFixed(1)}" stroke="${color}" stroke-width="1"/><rect x="${(x - bw / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" fill="${color}"/><rect x="${(x - bw / 2).toFixed(1)}" y="${(volTop + volH - vh).toFixed(1)}" width="${bw.toFixed(1)}" height="${vh.toFixed(1)}" fill="${color}" opacity="0.8"/>`;
  }).join("");
  const split = padY + priceH + gap / 2;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("width", String(w));
  svg.setAttribute("height", String(h));
  if (window.innerWidth < 980) {
    svg.style.aspectRatio = "auto";
    svg.style.width = "100%";
    svg.style.height = "240px";
  } else {
    svg.style.aspectRatio = w + " / " + h;
    svg.style.width = "100%";
    svg.style.height = "auto";
  }
  svg.dataset.w = w;
  svg.dataset.h = h;
  svg.dataset.padx = padX;
  svg.dataset.pady = padY;
  svg.dataset.priceh = priceH;
  svg.dataset.min = min;
  svg.dataset.span = span;
  svg.dataset.start = chartStart;
  svg.dataset.count = bars.length;
  svg.innerHTML = `<line x1="${padX}" y1="${split.toFixed(1)}" x2="${w - padX}" y2="${split.toFixed(1)}" stroke="#e7e5e4" stroke-width="1"/>${parts}<line id="kline-v" stroke="#1c1917" stroke-width="1" stroke-dasharray="3 3" visibility="hidden"/><line id="kline-h" stroke="#1c1917" stroke-width="1" stroke-dasharray="3 3" visibility="hidden"/>`;
  const first = chartBars[0].date;
  const last = chartBars[chartBars.length - 1].date;
  cap.textContent = chartBars.length > count
    ? `${first} → ${last} · 当前 ${bars[0].date} → ${bars[bars.length - 1].date} · 双指缩放，单指拖动`
    : `${first} → ${last} · 已是全部日K · 双指可放大`;
}
function tipRow(label, value, cls) {
  return `<div class="kline-row"><span class="muted">${label}</span><span class="${cls || ""}">${value}</span></div>`;
}
function showBar(index) {
  const svg = document.querySelector("svg.kline");
  const tip = document.getElementById("kline-tip");
  if (!svg || !tip || !chartBars.length) return;
  const i = Math.max(0, Math.min(chartBars.length - 1, index));
  const b = chartBars[i];
  const w = Number(svg.dataset.w), h = Number(svg.dataset.h), padX = Number(svg.dataset.padx), padY = Number(svg.dataset.pady), priceH = Number(svg.dataset.priceh);
  const min = Number(svg.dataset.min), span = Number(svg.dataset.span) || 1;
  const start = Number(svg.dataset.start) || 0;
  const count = Number(svg.dataset.count) || 1;
  const local = Math.max(0, Math.min(count - 1, i - start));
  const slot = (w - padX * 2) / count;
  const x = padX + local * slot + slot / 2;
  const y = padY + (1 - (Number(b.close) - min) / span) * priceH;
  const vline = document.getElementById("kline-v");
  const hline = document.getElementById("kline-h");
  vline.setAttribute("x1", x.toFixed(1)); vline.setAttribute("x2", x.toFixed(1));
  vline.setAttribute("y1", String(padY)); vline.setAttribute("y2", String(h - 4));
  vline.setAttribute("visibility", "visible");
  hline.setAttribute("y1", y.toFixed(1)); hline.setAttribute("y2", y.toFixed(1));
  hline.setAttribute("x1", String(padX)); hline.setAttribute("x2", String(w - padX));
  hline.setAttribute("visibility", "visible");
  const prev = b.prev == null ? null : Number(b.prev);
  const chg = prev ? Number(b.close) - prev : null;
  const pct = prev ? (Number(b.close) / prev - 1) * 100 : null;
  const amp = prev ? (Number(b.high) - Number(b.low)) / prev * 100 : null;
  const cls = chg == null ? "" : chg > 0 ? "up" : chg < 0 ? "down" : "";
  tip.innerHTML = `<div class="kdate">${esc(b.date)}</div><div class="kline-cols"><div>`
    + tipRow("开盘", px2(b.open))
    + tipRow("最高", px2(b.high), "up")
    + tipRow("最低", px2(b.low), "down")
    + tipRow("收盘", px2(b.close), cls)
    + tipRow("涨跌", pct == null ? "—" : signed(pct, 2) + "%", cls)
    + tipRow("振幅", amp == null ? "—" : amp.toFixed(2) + "%")
    + tipRow("成交量", hands(b.volume))
    + tipRow("成交额", money(b.amount))
    + `</div><div>`
    + tipRow("MA5", px2(b.ma5))
    + tipRow("MA10", px2(b.ma10))
    + tipRow("MA20", px2(b.ma20))
    + tipRow("MA40", px2(b.ma40))
    + tipRow("MA160", px2(b.ma160))
    + tipRow("DIF", b.dif == null ? "—" : Number(b.dif).toFixed(3))
    + tipRow("DEA", b.dea == null ? "—" : Number(b.dea).toFixed(3))
    + `</div></div>`;
  tip.hidden = false;
  if (window.innerWidth < 980) {
    tip.style.left = "4px";
    tip.style.top = "46px";
    return;
  }
  const wrap = svg.parentElement.getBoundingClientRect();
  const svgBox = svg.getBoundingClientRect();
  const tipW = tip.offsetWidth || 180;
  let left = (svgBox.left - wrap.left) + (x / w) * svgBox.width + 12;
  if (left + tipW > wrap.width - 4) left = (svgBox.left - wrap.left) + (x / w) * svgBox.width - tipW - 12;
  tip.style.left = Math.max(4, left) + "px";
  tip.style.top = "4px";
}
function hideBar() {
  const tip = document.getElementById("kline-tip");
  const vline = document.getElementById("kline-v");
  const hline = document.getElementById("kline-h");
  if (tip) tip.hidden = true;
  if (vline) vline.setAttribute("visibility", "hidden");
  if (hline) hline.setAttribute("visibility", "hidden");
}
function barIndexAt(svg, clientX) {
  const rect = svg.getBoundingClientRect();
  const w = Number(svg.dataset.w), padX = Number(svg.dataset.padx);
  const count = Number(svg.dataset.count) || 1;
  const x = (clientX - rect.left) / rect.width * w;
  const slot = (w - padX * 2) / count;
  return (Number(svg.dataset.start) || 0) + Math.floor((x - padX) / slot);
}
function bindKline() {
  const svg = document.getElementById("kline-svg");
  if (!svg || !chartBars.length || svg.dataset.bound) return;
  svg.dataset.bound = "1";
  const pts = new Map();
  let drag = null;
  let pinch = null;
  let tipHold = false;
  const hideTip = () => { tipHold = false; hideBar(); };
  const fingerDist = () => {
    const a = [...pts.values()];
    if (a.length < 2) return 0;
    return Math.hypot(a[0].x - a[1].x, a[0].y - a[1].y);
  };
  const fingerMidX = () => {
    const a = [...pts.values()];
    return (a[0].x + a[1].x) / 2;
  };
  svg.addEventListener("pointerdown", ev => {
    if (ev.pointerType === "mouse" && ev.button !== 0) return;
    pts.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    try { svg.setPointerCapture(ev.pointerId); } catch (e) {}
    if (pts.size >= 2) {
      drag = null;
      svg.classList.remove("dragging");
      hideTip();
      pinch = { dist: Math.max(fingerDist(), 1), span: chartSpan, anchor: barIndexAt(svg, fingerMidX()) };
      return;
    }
    drag = { x: ev.clientX, start: chartStart, moved: false, touch: ev.pointerType === "touch" };
    if (drag.touch) {
      tipHold = true;
      showBar(barIndexAt(svg, ev.clientX));
    } else {
      tipHold = false;
      svg.classList.add("dragging");
    }
  });
  svg.addEventListener("pointermove", ev => {
    if (pts.has(ev.pointerId)) pts.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    if (pinch && pts.size >= 2) {
      const dist = fingerDist();
      if (dist > 12) zoomChart(pinch.span * (pinch.dist / dist), pinch.anchor);
      return;
    }
    if (!drag) {
      if (ev.pointerType !== "touch") showBar(barIndexAt(svg, ev.clientX));
      return;
    }
    const dx = ev.clientX - drag.x;
    if (Math.abs(dx) > (drag.touch ? 14 : 4)) drag.moved = true;
    if (!drag.moved) {
      if (drag.touch) showBar(barIndexAt(svg, ev.clientX));
      return;
    }
    svg.classList.add("dragging");
    hideTip();
    const rect = svg.getBoundingClientRect();
    const count = Number(svg.dataset.count) || 1;
    const next = drag.start - Math.round(dx / (rect.width / count));
    const countFit = visibleSpan();
    const clamped = Math.max(0, Math.min(next, chartBars.length - countFit));
    if (clamped !== chartStart) {
      chartStart = clamped;
      paintChart();
    }
  });
  const endPointer = ev => {
    const wasPinch = !!pinch;
    const touched = drag && drag.touch;
    pts.delete(ev.pointerId);
    if (pts.size < 2) pinch = null;
    if (wasPinch || touched || ev.type === "pointercancel") hideTip();
    else tipHold = false;
    drag = null;
    svg.classList.remove("dragging");
  };
  svg.addEventListener("pointerup", endPointer);
  svg.addEventListener("pointercancel", endPointer);
  svg.addEventListener("pointerleave", () => { if (!drag && !pinch && !tipHold) hideBar(); });
  svg.addEventListener("wheel", ev => {
    ev.preventDefault();
    if (ev.ctrlKey || ev.metaKey) {
      zoomChart(chartSpan * (ev.deltaY > 0 ? 1.25 : 0.8), barIndexAt(svg, ev.clientX));
      return;
    }
    const countFit = visibleSpan();
    const next = chartStart + (ev.deltaY > 0 ? -8 : 8);
    chartStart = Math.max(0, Math.min(next, chartBars.length - countFit));
    hideBar();
    paintChart();
  }, { passive: false });
  const zoomIn = document.getElementById("kzoom-in");
  const zoomOut = document.getElementById("kzoom-out");
  const onZoom = (factor, ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    zoomChart(chartSpan * factor, chartStart + visibleSpan() / 2);
  };
  if (zoomIn) zoomIn.addEventListener("click", ev => onZoom(0.72, ev));
  if (zoomOut) zoomOut.addEventListener("click", ev => onZoom(1.4, ev));
  hideBar();
}
function revealChart() {
  const layout = document.querySelector(".layout");
  if (layout) layout.classList.add("picked");
  if (window.innerWidth >= 980) return;
  const detail = document.getElementById("detail");
  if (detail) detail.scrollIntoView({behavior:"auto", block:"start"});
}
async function showPublishedDetail(row) {
  const pct = Number(row.pct);
  const cls = pct > 0 ? "up" : pct < 0 ? "down" : "";
  const sign = pct > 0 ? "+" : "";
  const scoreCls = Number(row.score) >= 30 ? "hi" : Number(row.score) >= 22 ? "mid" : "lo";
  const tags = (row.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join("");
  const soe = soeDeals[row.code] ? `<p class="muted soe-note">${esc(soeDeals[row.code])}</p>` : "";
  const cap = (row.total_mv || row.float_mv) ? `<p class="muted capline">总市值 ${capText(row.total_mv)} · 流通 ${capText(row.float_mv)}</p>` : "";
  detailEl.innerHTML = `<div class="detail-head"><div><div class="detail-title"><span class="code">${esc(row.code)}</span><span>${esc(row.name || "")}</span><span class="score ${scoreCls}">${Number(row.score).toFixed(1)}</span></div>${cap}<p class="muted">${esc(row.date)}<span class="pill yes">符合入场</span></p></div></div>
    <div class="metrics">
      <div><span>收盘</span><b>${Number(row.price).toFixed(2)}</b></div>
      <div><span>涨跌</span><b class="${cls}">${sign}${pct.toFixed(2)}%</b></div>
      <div><span>止损参考</span><b>${row.stop == null ? "—" : Number(row.stop).toFixed(2)}</b></div>
      <div><span>2:1 目标</span><b>${row.target == null ? "—" : Number(row.target).toFixed(2)}</b></div>
    </div>
    <div class="kline-wrap"><svg id="kline-svg" class="kline" role="img" aria-label="日K和成交额"></svg><div id="kline-tip" class="kline-tip" hidden></div><div class="kzoom"><button type="button" id="kzoom-in" aria-label="放大">+</button><button type="button" id="kzoom-out" aria-label="缩小">−</button></div></div>
    <div id="kline-cap" class="muted kline-cap">正在读取日K…</div>
    <div class="tags">${tags}</div>
    ${soe}`;
  revealChart();
  chartBars = [];
  try {
    const res = await fetch("bars/" + encodeURIComponent(row.code) + ".json");
    if (res.ok) chartBars = await res.json();
  } catch (e) {}
  if (!chartBars.length) {
    const capEl = document.getElementById("kline-cap");
    if (capEl) capEl.textContent = "这只股票的日K还没发布。";
    return;
  }
  chartSpan = 110;
  chartStart = Math.max(0, chartBars.length - chartSpan);
  paintChart();
  bindKline();
  revealChart();
}
async function openDetail(code, name) {
  selected = code;
  renderList();
  if (!localTool) {
    const row = board.rows.find(r => r.code === code);
    if (!row) { detailEl.innerHTML = `<p>没有数据</p>`; return; }
    await showPublishedDetail(row);
    return;
  }
  detailEl.innerHTML = `<p class="muted">正在计算 ${esc(code)} …</p>`;
  const res = await fetch("/api/stock?code=" + encodeURIComponent(code));
  const data = await res.json();
  if (!data.ok) { detailEl.innerHTML = `<p>${esc(data.error || "没有数据")}</p>`; return; }
  const pct = Number(data.pct);
  const cls = pct > 0 ? "up" : pct < 0 ? "down" : "";
  const sign = pct > 0 ? "+" : "";
  const scoreCls = Number(data.score) >= 30 ? "hi" : Number(data.score) >= 22 ? "mid" : "lo";
  const flags = (data.flags || []).slice().sort((a, b) => Number(b.on) - Number(a.on)).map(f => `<div class="flag ${f.on ? "on" : "off"}"><div class="flag-h"><b>${esc(f.label)}</b><span class="state">${f.on ? "命中" : "未中"}</span></div><div class="muted">${esc(f.text)}</div></div>`).join("");
  detailEl.innerHTML = `<div class="detail-head"><div><div class="detail-title"><span class="code">${esc(data.code)}</span><span>${esc(name || "")}</span><span class="score ${scoreCls}">${Number(data.score).toFixed(1)}</span></div><p class="muted capline">总市值 ${capText(data.total_mv)} · 流通 ${capText(data.float_mv)}</p><p class="muted">${esc(data.date)}<span class="pill ${data.entry ? "yes" : "no"}">${data.entry ? "符合入场" : "不符合入场"}</span></p></div></div>
    <div class="metrics">
      <div><span>收盘</span><b>${Number(data.price).toFixed(2)}</b></div>
      <div><span>涨跌</span><b class="${cls}">${sign}${pct.toFixed(2)}%</b></div>
      <div><span>止损参考</span><b>${data.stop == null ? "—" : Number(data.stop).toFixed(2)}</b></div>
      <div><span>2:1 目标</span><b>${data.target == null ? "—" : Number(data.target).toFixed(2)}</b></div>
    </div>
    <div class="kline-wrap"><svg id="kline-svg" class="kline" role="img" aria-label="日K和成交额"></svg><div id="kline-tip" class="kline-tip" hidden></div><div class="kzoom"><button type="button" id="kzoom-in" aria-label="放大">+</button><button type="button" id="kzoom-out" aria-label="缩小">−</button></div></div>
    <div id="kline-cap" class="muted kline-cap"></div>
    <h2 class="block-title">规则对照</h2>
    ${flags}`;
  chartBars = data.bars || [];
  chartSpan = 110;
  chartStart = Math.max(0, chartBars.length - chartSpan);
  paintChart();
  bindKline();
  revealChart();
}
function setBoard(nextPrefix) {
  prefix = nextPrefix;
  hotOnly = false;
  soeOnly = false;
  document.querySelectorAll(".tab, .hotbtn").forEach(b => b.classList.remove("active"));
  const picked = document.querySelector(`.tab[data-prefix="${nextPrefix}"]`);
  if (picked) picked.classList.add("active");
  renderList();
}
document.querySelectorAll(".tab").forEach(btn => btn.onclick = () => setBoard(btn.dataset.prefix));
document.getElementById("hotbtn").onclick = () => {
  hotOnly = true;
  soeOnly = false;
  document.querySelectorAll(".tab, .hotbtn").forEach(b => b.classList.remove("active"));
  document.getElementById("hotbtn").classList.add("active");
  renderList();
};
document.getElementById("soebtn").onclick = () => {
  soeOnly = true;
  hotOnly = false;
  document.querySelectorAll(".tab, .hotbtn").forEach(b => b.classList.remove("active"));
  document.getElementById("soebtn").classList.add("active");
  renderList();
};
function applyMeta(data) {
  const m = data.meta || {};
  const c = m.counts || {};
  document.getElementById("meta").textContent = `信号日 ${m.asof || "—"} · 扫描 ${m.scanned || 0} 只（沪A ${c["6"] || 0} / 创业板 ${c["3"] || 0} / 深A主板 ${c["0"] || 0}）· 入选 ${data.rows.length} 只`;
}
function refreshText(s) {
  if (s.error) return s.error;
  if (!s.total) return "正在获取股票列表…";
  return `正在拉取 ${s.done}/${s.total} · 符合 ${s.hits} · 失败 ${s.failed}`;
}
async function pollRefresh() {
  const btn = document.getElementById("refreshBtn");
  const msg = document.getElementById("refreshMsg");
  const res = await fetch("/api/refresh");
  const s = await res.json();
  if (s.running) {
    btn.disabled = true;
    btn.textContent = "拉取中";
    msg.textContent = refreshText(s);
    setTimeout(pollRefresh, 1000);
    return;
  }
  btn.disabled = false;
  btn.textContent = "重新拉取";
  if (s.error) { msg.textContent = s.error; return; }
  if (!s.finished) { msg.textContent = ""; return; }
  const data = await fetch("/api/list").then(r => r.json());
  board = data;
  applyMeta(data);
  renderList();
  msg.textContent = `已按因子更新，信号日 ${s.asof || "—"}，入选 ${data.rows.length} 只`;
}
document.getElementById("refreshBtn").onclick = async () => {
  const btn = document.getElementById("refreshBtn");
  const msg = document.getElementById("refreshMsg");
  btn.disabled = true;
  btn.textContent = "拉取中";
  msg.textContent = "正在获取股票列表…";
  const res = await fetch("/api/refresh", {method: "POST"});
  if (!res.ok) {
    btn.disabled = false;
    btn.textContent = "重新拉取";
    msg.textContent = "这次没有拉完，列表还是上一次的结果";
    return;
  }
  pollRefresh();
};
async function boot() {
  let data = null;
  try {
    const res = await fetch("/api/list");
    if (res.ok) {
      const body = await res.json();
      if (body && Array.isArray(body.rows)) {
        data = body;
        localTool = true;
      }
    }
  } catch (e) {}
  if (!data) {
    const res = await fetch("results.json");
    if (!res.ok) throw new Error("no results");
    data = await res.json();
    localTool = false;
    document.getElementById("refreshBtn").hidden = true;
    document.getElementById("refreshMsg").textContent = "这是已发布的结果。重新拉取请在自己电脑上打开工具，拉完后再发布一次。";
  }
  board = data;
  applyMeta(data);
  document.getElementById("hotbtn").textContent = "高度吻合";
  renderList();
  if (!localTool) return;
  const s = await fetch("/api/refresh").then(r => r.json()).catch(() => null);
  if (s && s.running) pollRefresh();
}
boot().catch(() => { document.getElementById("meta").textContent = "结果没有读出来"; });
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/list":
            board = load_board()
            attach_market_caps(board.get("rows") or [])
            body = json.dumps(board, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/refresh":
            body = json.dumps(scan_status(), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/stock":
            code = (parse_qs(parsed.query).get("code") or [""])[0]
            detail = stock_detail(code)
            if detail.get("ok"):
                attach_market_caps([detail])
            body = json.dumps(detail, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/refresh":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        started = start_scan()
        body = json.dumps({"ok": True, "started": started}, ensure_ascii=False).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


def export_published_bars(rows: list[dict]) -> None:
    folder = DOCS / "bars"
    folder.mkdir(exist_ok=True)
    keep: set[str] = set()
    for row in rows:
        code = "".join(ch for ch in str(row.get("code") or "") if ch.isdigit())[:6]
        frame = kline_frame(code) if len(code) == 6 else None
        if frame is None or len(frame) < 2:
            continue
        body = json.dumps(chart_bars(frame), ensure_ascii=False, separators=(",", ":"))
        (folder / f"{code}.json").write_text(body, encoding="utf-8")
        keep.add(code)
    for old in folder.glob("*.json"):
        if old.stem not in keep:
            old.unlink()


def sync_pages() -> None:
    """把当前页面、筛选结果和日K写到 docs，供公开网页读取。"""
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(PAGE, encoding="utf-8")
    src = WEB / "results.json"
    if src.exists():
        text = src.read_text(encoding="utf-8")
        (DOCS / "results.json").write_text(text, encoding="utf-8")
        export_published_bars(json.loads(text).get("rows") or [])


def main() -> None:
    sync_pages()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"小葵的Ai http://127.0.0.1:{PORT}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
