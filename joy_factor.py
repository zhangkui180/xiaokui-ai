# -*- coding: utf-8 -*-
"""Joy讲交易公开视频规则编译成的日线因子。

账号：抖音号 56847674589（Joy讲交易 / 趋势浪子）。
对照了主页约 420 条公开作品标题，以及三重滤网、神龙系、520、
123法则、底部三步曲、攻击性三步曲、背驰、二波、成交量、区间套、
MACD分型等讲解视频的公开摘要。会员专属视频没有写进公式。

视频是逐根K线的盘感训练，没有可对齐的「股票-日期」标签，
所以这里不是机器学习拟合，而是把他反复使用的规则写成可计算分数。
画线、主力意图、小级别口播里的临场取舍，日线OHLCV只能近似。

不构成投资建议。他本人也把模拟盘当作实盘前的训练。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_ALIASES = {
    "open": ("open", "开盘", "开盘价"),
    "high": ("high", "最高", "最高价"),
    "low": ("low", "最低", "最低价"),
    "close": ("close", "收盘", "收盘价"),
    "volume": ("volume", "vol", "成交量", "成交额"),
}


def _pick(df: pd.DataFrame, name: str) -> pd.Series:
    lookup = {str(c).strip().lower(): c for c in df.columns}
    for alias in _ALIASES[name]:
        col = lookup.get(alias.lower())
        if col is not None:
            return pd.to_numeric(df[col], errors="coerce")
    raise KeyError(f"缺少列 {name}，可用列名：{_ALIASES[name]}")


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    dif = _ema(close, fast) - _ema(close, slow)
    dea = _ema(dif, signal)
    return dif, dea


def _cross_up(fast: pd.Series, slow: pd.Series) -> pd.Series:
    return (fast > slow) & (fast.shift(1) <= slow.shift(1))


def _flag(cond: pd.Series) -> pd.Series:
    return cond.fillna(False).astype(bool)


def _carry(level: pd.Series, trigger: pd.Series, limit: int) -> pd.Series:
    """触发日锁定价位，之后最多延续 limit 根，供回踩确认使用。"""
    return level.where(_flag(trigger)).ffill(limit=limit)


def joy_factor(
    df: pd.DataFrame,
    *,
    ma_tide: int = 160,
    ma_mid: int = 40,
    ma_short: int = 10,
    ma_fast: int = 5,
    ma_life: int = 20,
) -> pd.DataFrame:
    """计算 Joy 复合因子。

    参数默认值来自《三重滤网01》：160 / 40 / 10 日均线。
    520 战法使用 5 日与 20 日。攻击性三步曲的「核心生命线」取 20 日。

    返回原表的拷贝，并附加各分项布尔列、``joy_score``（0-100）、
    ``joy_entry``、参考止损 ``stop_ref`` 和 2:1 目标 ``target_2r``。
    """
    out = df.copy()
    o = _pick(df, "open")
    h = _pick(df, "high")
    l = _pick(df, "low")
    c = _pick(df, "close")
    v = _pick(df, "volume").clip(lower=0)
    idx = c.index

    ma160 = c.rolling(ma_tide).mean()
    ma40 = c.rolling(ma_mid).mean()
    ma10 = c.rolling(ma_short).mean()
    ma5 = c.rolling(ma_fast).mean()
    ma20 = c.rolling(ma_life).mean()
    vol_ma = v.rolling(20).mean().replace(0, np.nan)
    dif, dea = _macd(c)
    gold = _flag(_cross_up(dif, dea))

    # 三重滤网：大级别在 160 日上方且均线抬升。
    # 动手日要同时满足：近端回踩过 40 日、5 日内出现 MACD 金叉、
    # 当天重新站上 40 日、10 日线转上。站上之后不再天天重复给信号。
    tide = _flag((c > ma160) & (ma160.diff(5) > 0))
    near_mid = _flag(l.shift(1).rolling(8).min() <= ma40 * 1.02)
    short_up = _flag((c > ma10) & (ma10.diff(1) > 0))
    reclaim_mid = _flag((c > ma40) & (c.shift(1) <= ma40))
    screen = tide & near_mid & _flag(gold.rolling(5).max()) & reclaim_mid & short_up

    # 520：5 日金叉 20 日之后，回踩 20 日不破，再重新站上 5 日。20 日须向上。
    cross_520 = _flag(_cross_up(ma5, ma20))
    touch20 = _flag((l <= ma20 * 1.015) & (c >= ma20 * 0.995) & (ma5 > ma20))
    m520 = (
        _flag(cross_520.rolling(12).max())
        & _flag(touch20.rolling(8).max())
        & _flag(_cross_up(c, ma5))
        & _flag(ma20.diff(3) > 0)
        & (c > ma20)
    )

    # 123法则：先处在下跌里，回踩不创新低，再突破前高。三步齐了才算。
    down = _flag(c.shift(10) < c.shift(25))
    higher_low = _flag(l.rolling(6).min() > l.rolling(10).min().shift(8))
    rule123 = down & higher_low & _flag(c > h.rolling(8).max().shift(1)) & (c > o)

    # 底部三步曲：先有一段明显下跌，再进入窄幅蓄力，放量突破后缩量回踩不破。
    peak_before = h.rolling(60).max().shift(12)
    washed = _flag(l.rolling(12).min() <= peak_before * 0.90)
    box_rng = (h.rolling(12).max() - l.rolling(12).min()) / c.replace(0, np.nan)
    base = _flag(box_rng < 0.12)
    box_high = h.rolling(12).max().shift(1)
    breakout = _flag((c > box_high) & (v > 1.5 * vol_ma) & base.shift(1) & washed)
    base_level = _carry(box_high, breakout, 6)
    bottom3 = (
        base_level.notna()
        & _flag(l >= base_level * 0.98)
        & _flag(v < vol_ma)
        & _flag(c > c.shift(1))
        & ~breakout
    )

    # 攻击性三步曲：生命线向上，放量突破，缩量回踩不破。
    atk_level_src = h.rolling(10).max().shift(1)
    atk_break = _flag((c > atk_level_src) & (v > 1.5 * vol_ma) & (ma20.diff(3) > 0) & (c > ma20))
    atk_level = _carry(atk_level_src, atk_break, 6)
    attack3 = (
        atk_level.notna()
        & _flag(l >= atk_level * 0.98)
        & _flag(v < vol_ma)
        & _flag(c > ma5)
        & ~atk_break
    )

    # 神龙出海：站稳前高压力，缩量回踩不破，MACD 水上金叉。
    resist = h.rolling(20).max().shift(1)
    stood = _flag((c > resist) & (v > vol_ma))
    sea_level = _carry(resist, stood, 8)
    water_gold = _flag(gold & (dif > 0))
    dragon = (
        sea_level.notna()
        & _flag(l >= sea_level * 0.99)
        & _flag(v < v.rolling(5).max())
        & _flag(water_gold.rolling(5).max())
        & ~stood
    )

    # 类三买：离开近端中枢上沿后，回抽不跌回中枢，并重新转强。
    hub_high = h.rolling(20).max().shift(1)
    left_hub = _flag((c > hub_high) & (v > vol_ma))
    hub_level = _carry(hub_high, left_hub, 8)
    buy3 = hub_level.notna() & _flag(l > hub_level) & (c > o) & _flag(dif > dea) & ~left_hub

    # 一柱擎天：收敛之后，放量长阳突破前压。
    body = (c - o) / o.replace(0, np.nan)
    compressed = _flag(((h.rolling(10).max() - l.rolling(10).min()) / c.replace(0, np.nan)).shift(1) < 0.10)
    pillar = _flag((body > 0.05) & (v > 2 * vol_ma) & compressed & (c > h.rolling(20).max().shift(1)))

    # 背驰：价格创新低，DIF 不创新低，且这段里 DIF 回到过零轴附近。
    # 他明确说过背驰只是动能衰竭，不一定反转，所以权重低。
    price_new_low = _flag(l <= l.rolling(15).min().shift(1))
    dif_hold = _flag(dif > dif.rolling(15).min().shift(1))
    back_to_zero = _flag(dif.rolling(20).max() >= 0)
    beichi = price_new_low & dif_hold & back_to_zero & _flag(c < ma40)

    # 二波：第一波攻击之后，回撤不破转势起点，再等金叉。盈亏比参照 2:1。
    impulse = _flag(c.shift(5) / c.shift(15) - 1 > 0.08)
    hold_turn = _flag(l.rolling(5).min() > c.shift(15) * 0.98)
    wave2 = impulse & hold_turn & gold & _flag(v > vol_ma)

    # 真突破：收敛三角形一类结构，突破当日量能达到均量 3 倍以上。
    tight = _flag(((h.rolling(15).max() - l.rolling(15).min()) / c.replace(0, np.nan)).shift(1) < 0.08)
    vol_break = _flag((v > 3 * vol_ma) & (c > h.rolling(15).max().shift(1)) & tight)

    # 上涨后段爆量，按他的量能课视为诱多/出货，扣分。
    rally = _flag(c / c.shift(60) - 1 > 0.35)
    distribution = rally & _flag(v > 2.5 * vol_ma) & _flag(c >= c.rolling(60).quantile(0.8))

    parts = {
        "tide": (tide, 12),
        "screen": (screen, 18),
        "m520": (m520, 10),
        "rule123": (rule123, 8),
        "bottom3": (bottom3, 12),
        "attack3": (attack3, 10),
        "dragon": (dragon, 10),
        "buy3": (buy3, 8),
        "pillar": (pillar, 6),
        "beichi": (beichi, 6),
        "wave2": (wave2, 6),
        "vol_break": (vol_break, 4),
    }
    score = pd.Series(0.0, index=idx)
    for name, (cond, weight) in parts.items():
        flag = _flag(cond)
        out[name] = flag
        score = score + flag.astype(float) * weight
    out["distribution"] = _flag(distribution)
    score = score - out["distribution"].astype(float) * 20
    out["joy_score"] = score.clip(lower=0, upper=100)

    # 顺势模型必须大级别向上。底部三步曲是他单独练的抄底结构，不要求 160 日已经拐头。
    # 单纯背驰不加进场，他说过背驰只说明动能衰竭，不一定反转。
    trend_entry = tide & (screen | m520 | attack3 | dragon | buy3 | rule123)
    out["joy_entry"] = (trend_entry | bottom3) & ~out["distribution"]

    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    out["atr14"] = tr.rolling(14).mean()
    out["stop_ref"] = l.rolling(10).min()
    risk = (c - out["stop_ref"]).clip(lower=0)
    out["target_2r"] = c + 2 * risk
    # 移动止损参照：站上中周期后，用 10 日线台阶上移。
    out["trail_ref"] = np.where(c > ma40, ma10, out["stop_ref"])
    return out


def latest_scores(frames: dict[str, pd.DataFrame], **kwargs) -> pd.DataFrame:
    """多标的各自取最后一根K线，按 joy_score 从高到低排列。"""
    rows = []
    for symbol, frame in frames.items():
        scored = joy_factor(frame, **kwargs)
        if scored.empty:
            continue
        last = scored.iloc[-1]
        rows.append(
            {
                "symbol": symbol,
                "joy_score": float(last["joy_score"]),
                "joy_entry": bool(last["joy_entry"]),
                "screen": bool(last["screen"]),
                "m520": bool(last["m520"]),
                "bottom3": bool(last["bottom3"]),
                "attack3": bool(last["attack3"]),
                "dragon": bool(last["dragon"]),
                "buy3": bool(last["buy3"]),
                "beichi": bool(last["beichi"]),
                "distribution": bool(last["distribution"]),
            }
        )
    table = pd.DataFrame(rows)
    if table.empty:
        return table
    return table.sort_values(["joy_entry", "joy_score"], ascending=[False, False]).reset_index(drop=True)


def _demo_frame() -> pd.DataFrame:
    """构造一段「下跌—横盘—放量突破—缩量回踩」以便自检。"""
    fall = np.linspace(40, 18, 80)
    base = np.full(15, 18.0)
    event = np.array([20.0, 19.4, 19.2, 19.6])
    rest = np.linspace(19.7, 22, 21)
    close = np.concatenate([fall, base, event, rest])
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.05
    low = np.minimum(open_, close) - 0.05
    volume = np.full(close.size, 1_000_000.0)
    volume[95] = 5_000_000.0
    volume[96:99] = 400_000.0
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


if __name__ == "__main__":
    scored = joy_factor(_demo_frame())
    cols = ["joy_score", "joy_entry", "bottom3", "tide", "screen", "m520", "dragon", "buy3"]
    hit = scored.loc[scored["bottom3"] | scored["joy_entry"], cols]
    print(hit.tail(12).to_string())
    print("max_score", float(scored["joy_score"].max()))
    assert scored["joy_score"].between(0, 100).all()
    assert bool(scored["bottom3"].any()), "示例走势应触发底部三步曲"
    assert bool((scored["bottom3"] & scored["joy_entry"]).any()), "底部三步曲应给出入场"
