# -*- coding: utf-8 -*-
"""用 Joy 因子扫描 6 / 3 / 0 开头的 A 股，并生成网页。

6 开头是沪 A（不含 688 科创板），3 开头是深市创业板，0 开头是深 A 主板。
行情用腾讯前复权日线。结果写到 joy_factor/web/index.html。
"""

from __future__ import annotations

import json
import sys
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from joy_factor import joy_factor

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
WEB = ROOT / "web"
OUT_HTML = WEB / "index.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}
TAGS = [
    ("screen", "三重滤网"),
    ("m520", "520"),
    ("bottom3", "底部三步曲"),
    ("attack3", "攻击性三步曲"),
    ("dragon", "神龙出海"),
    ("buy3", "类三买"),
    ("rule123", "123法则"),
    ("pillar", "一柱擎天"),
    ("beichi", "背驰"),
    ("wave2", "二波"),
    ("vol_break", "放量突破"),
    ("tide", "大级别向上"),
]
PREFIX_BOARD = {
    "6": "沪A",
    "3": "创业板",
    "0": "深A主板",
}


def _get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _sina_node(node: str) -> list[dict]:
    rows = []
    for page in range(1, 80):
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1&node={node}"
        )
        text = _get(url).decode("utf-8", errors="replace").strip()
        if not text or text == "null":
            break
        batch = json.loads(text)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
    return rows


def fetch_universe() -> list[dict]:
    """拉取沪深 A 股代码，只保留 6 / 3 / 0 开头。"""
    found: dict[str, dict] = {}
    # sh_a 沪市主板，sz_a 深市主板，cyb 创业板。688 科创板不扫。
    for node in ("sh_a", "sz_a", "cyb"):
        for item in _sina_node(node):
            code = str(item.get("code") or "").strip()
            name = str(item.get("name") or "").strip()
            if not code or code[0] not in PREFIX_BOARD or code.startswith("688"):
                continue
            found[code] = {
                "code": code,
                "name": name,
                "board": PREFIX_BOARD[code[0]],
                "prefix": code[0],
            }
    rows = list(found.values())
    rows.sort(key=lambda r: r["code"])
    return rows


def _symbol(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def fetch_bars(code: str, force: bool = False) -> pd.DataFrame | None:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{code}.csv"
    cached = None
    if path.exists():
        frame = pd.read_csv(path).sort_values("date").reset_index(drop=True)
        if len(frame) >= 60:
            cached = frame
            if not force and time.time() - path.stat().st_mtime < 18 * 3600:
                return cached
    symbol = _symbol(code)
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen=260"
    )
    last_error = None
    for attempt in range(4):
        try:
            time.sleep(0.15)
            text = _get(url).decode("utf-8", errors="replace").strip()
            if not text or text == "null":
                return cached
            klines = json.loads(text)
            rows = []
            for parts in klines:
                rows.append(
                    {
                        "date": parts["day"],
                        "open": float(parts["open"]),
                        "close": float(parts["close"]),
                        "high": float(parts["high"]),
                        "low": float(parts["low"]),
                        "volume": float(parts["volume"]),
                    }
                )
            if len(rows) < 60:
                return cached
            frame = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
            frame.to_csv(path, index=False)
            return frame
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(1.2 * (attempt + 1))
    if cached is not None:
        return cached
    raise RuntimeError(str(last_error))


def evaluate(stock: dict, force: bool = False) -> dict | None:
    frame = fetch_bars(stock["code"], force=force)
    if frame is None or frame.empty:
        return None
    scored = joy_factor(frame)
    last = scored.iloc[-1]
    prev_close = float(scored["close"].iloc[-2]) if len(scored) > 1 else float(last["close"])
    price = float(last["close"])
    pct = (price / prev_close - 1) * 100 if prev_close else 0.0
    tags = [label for key, label in TAGS if bool(last[key])]
    return {
        "code": stock["code"],
        "name": stock["name"],
        "board": stock["board"],
        "prefix": stock["prefix"],
        "date": str(frame["date"].iloc[-1]),
        "price": round(price, 2),
        "pct": round(pct, 2),
        "score": round(float(last["joy_score"]), 1),
        "entry": bool(last["joy_entry"]),
        "tags": tags,
        "stop": None if pd.isna(last["stop_ref"]) else round(float(last["stop_ref"]), 2),
        "target": None if pd.isna(last["target_2r"]) else round(float(last["target_2r"]), 2),
        "st": ("ST" in stock["name"].upper()) or ("退" in stock["name"]),
    }


def render(matches: list[dict], meta: dict) -> None:
    WEB.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"meta": meta, "rows": matches}, ensure_ascii=False)
    (WEB / "results.json").write_text(payload, encoding="utf-8")
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Joy 因子选股</title>
<style>
  :root { color-scheme: light; --bg:#f4f1ea; --card:#fffdf8; --ink:#1c1915; --muted:#6d655c; --line:#e4dccf; --up:#c5382f; --down:#1f7a45; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background:var(--bg); color:var(--ink); }
  header { padding:28px 28px 8px; max-width:1180px; margin:0 auto; }
  h1 { font-size:28px; font-weight:650; margin:0 0 6px; letter-spacing:-0.02em; }
  .sub { color:var(--muted); margin:0; }
  .warn { margin:14px 0 0; color:#7a4b12; background:#fff4df; border:1px solid #f0ddb4; padding:10px 12px; border-radius:8px; }
  main { max-width:1180px; margin:0 auto; padding:16px 28px 48px; }
  .bar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:8px 0 14px; }
  button.tab, input { border:1px solid var(--line); background:var(--card); color:var(--ink); border-radius:999px; padding:7px 14px; font:inherit; }
  button.tab.active { background:var(--ink); color:#fff; border-color:var(--ink); }
  input { border-radius:8px; min-width:220px; }
  .stats { color:var(--muted); margin-left:auto; }
  table { width:max-content; min-width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:12px; }
  th, td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; word-break:keep-all; }
  main { overflow-x:auto; }
  th { font-size:12px; color:var(--muted); font-weight:600; cursor:pointer; white-space:nowrap; }
  tr:last-child td { border-bottom:0; }
  .code { font-variant-numeric:tabular-nums; font-weight:650; }
  .up { color:var(--up); } .down { color:var(--down); }
  .tag { display:inline-block; margin:0 6px 4px 0; padding:1px 7px; border-radius:999px; background:#f3ecdf; font-size:12px; }
  .st { color:#9a3412; font-size:12px; margin-left:6px; }
  .empty { padding:28px; color:var(--muted); }
  @media (max-width:800px) {
    header, main { padding-left:14px; padding-right:14px; }
    .stats { margin-left:0; width:100%; }
    table { display:block; overflow-x:auto; }
  }
</style>
</head>
<body>
<header>
  <h1>Joy 因子选股</h1>
  <p class="sub" id="meta"></p>
  <p class="warn">这是按公开视频规则算出的技术条件筛选，不是买卖建议。止损和 2:1 目标只是因子里的参考价位。</p>
</header>
<main>
  <div class="bar">
    <button class="tab active" data-prefix="all">全部</button>
    <button class="tab" data-prefix="6">6 开头 · 沪A（不含688）</button>
    <button class="tab" data-prefix="3">3 开头 · 创业板</button>
    <button class="tab" data-prefix="0">0 开头 · 深A主板</button>
    <input id="q" placeholder="搜代码或名称" />
    <div class="stats" id="stats"></div>
  </div>
  <table>
    <thead>
      <tr>
        <th data-key="code">代码</th>
        <th data-key="name">名称</th>
        <th data-key="board">板块</th>
        <th data-key="price">收盘</th>
        <th data-key="pct">涨跌幅</th>
        <th data-key="score">因子分</th>
        <th>命中结构</th>
        <th data-key="stop">止损参考</th>
        <th data-key="target">2:1 目标</th>
      </tr>
    </thead>
    <tbody id="body"></tbody>
  </table>
  <p class="empty" id="empty" hidden>这一栏没有符合入场条件的股票。</p>
</main>
<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
const data = JSON.parse(document.getElementById("payload").textContent);
const meta = data.meta;
document.getElementById("meta").textContent =
  `信号日 ${meta.asof} · 已扫描 ${meta.scanned} 只 · 失败 ${meta.failed} 只 · 符合入场 ${data.rows.length} 只`;
let prefix = "all";
let query = "";
let sortKey = "score";
let sortDir = -1;
const body = document.getElementById("body");
const empty = document.getElementById("empty");
const stats = document.getElementById("stats");
function num(v) { return v == null || v === "" ? -Infinity : Number(v); }
function shown() {
  return data.rows.filter(r => (prefix === "all" || r.prefix === prefix)
    && (r.code.includes(query) || r.name.toLowerCase().includes(query)));
}
function render() {
  const rows = shown().slice().sort((a, b) => {
    const av = sortKey === "name" || sortKey === "board" || sortKey === "code" ? String(a[sortKey]) : num(a[sortKey]);
    const bv = sortKey === "name" || sortKey === "board" || sortKey === "code" ? String(b[sortKey]) : num(b[sortKey]);
    if (av < bv) return -sortDir;
    if (av > bv) return sortDir;
    return a.code < b.code ? -1 : 1;
  });
  stats.textContent = `当前 ${rows.length} 只`;
  empty.hidden = rows.length !== 0;
  body.innerHTML = rows.map(r => {
    const pctCls = r.pct > 0 ? "up" : r.pct < 0 ? "down" : "";
    const sign = r.pct > 0 ? "+" : "";
    const tags = (r.tags || []).map(t => `<span class="tag">${t}</span>`).join("");
    const st = r.st ? `<span class="st">风险标注</span>` : "";
    return `<tr>
      <td class="code">${r.code}</td>
      <td>${r.name}${st}</td>
      <td>${r.board}</td>
      <td>${r.price.toFixed(2)}</td>
      <td class="${pctCls}">${sign}${r.pct.toFixed(2)}%</td>
      <td>${r.score.toFixed(1)}</td>
      <td>${tags}</td>
      <td>${r.stop == null ? "—" : r.stop.toFixed(2)}</td>
      <td>${r.target == null ? "—" : r.target.toFixed(2)}</td>
    </tr>`;
  }).join("");
}
document.querySelectorAll(".tab").forEach(btn => btn.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  prefix = btn.dataset.prefix;
  render();
}));
document.getElementById("q").addEventListener("input", e => { query = e.target.value.trim().toLowerCase(); render(); });
document.querySelectorAll("th[data-key]").forEach(th => th.addEventListener("click", () => {
  const key = th.dataset.key;
  if (sortKey === key) sortDir *= -1; else { sortKey = key; sortDir = key === "code" || key === "name" ? 1 : -1; }
  render();
}));
render();
</script>
</body>
</html>
"""
    html = html.replace("__PAYLOAD__", payload)
    OUT_HTML.write_text(html, encoding="utf-8")


def scan_market(force: bool = False, on_progress=None) -> dict:
    """按因子扫 6 / 3 / 0。force 时忽略 18 小时缓存，重新拉日线。"""
    started = time.time()
    if on_progress:
        on_progress({"phase": "universe", "done": 0, "total": 0, "hits": 0, "failed": 0})
    universe = fetch_universe()
    if not universe:
        raise RuntimeError("没有拉到股票列表")
    counts = {k: sum(1 for s in universe if s["prefix"] == k) for k in "630"}
    print(f"universe {len(universe)} 6={counts['6']} 3={counts['3']} 0={counts['0']}", flush=True)
    matches = []
    dates = []
    failed = 0
    done = 0
    if on_progress:
        on_progress({"phase": "scan", "done": 0, "total": len(universe), "hits": 0, "failed": 0})
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(evaluate, stock, force): stock for stock in universe}
        for fut in as_completed(futures):
            done += 1
            stock = futures[fut]
            try:
                row = fut.result()
            except Exception:
                failed += 1
                if failed <= 8:
                    print(f"fail {stock['code']}: {traceback.format_exc(limit=1).strip()}", flush=True)
                row = None
            if row:
                dates.append(row["date"])
            if row and row["entry"]:
                matches.append(row)
            if on_progress:
                on_progress({"phase": "scan", "done": done, "total": len(universe), "hits": len(matches), "failed": failed})
            if done % 100 == 0 or done == len(universe):
                print(f"progress {done}/{len(universe)} hits={len(matches)} failed={failed}", flush=True)
    asof = max(dates) if dates else datetime.now().strftime("%Y-%m-%d")
    matches = [row for row in matches if row["date"] == asof]
    matches.sort(key=lambda r: (-r["score"], r["code"]))
    if not matches:
        raise RuntimeError("这次没有筛出符合入场的股票")
    meta = {
        "asof": asof,
        "scanned": len(universe) - failed,
        "failed": failed,
        "counts": counts,
        "elapsed_sec": round(time.time() - started, 1),
    }
    WEB.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "rows": matches}
    (WEB / "results.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def main() -> None:
    payload = scan_market(force=False)
    render(payload["rows"], payload["meta"])
    print(f"wrote {OUT_HTML} matches={len(payload['rows'])} asof={payload['meta']['asof']}", flush=True)


if __name__ == "__main__":
    main()
