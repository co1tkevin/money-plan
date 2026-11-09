"""永久组合 · 共用业务计算模块

网页端（main.py）与命令行脚本（snapshot_task.py）复用本模块的同一套计算逻辑，
因此这里不依赖 Streamlit，也不直接读写任何存储。

数值约定：
- 对外（localStorage / JSON / DataFrame）仍是普通数字，保持旧数据结构不变；
- 所有内部加减乘除统一使用 Decimal，接口边界用 D() 转换，避免 float 浮点误差。
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# ---------------------------------------------------------------- 固定分类
CATEGORIES = ["货币", "纯债", "黄金", "红利低波", "沪深300", "中证500", "创业板指", "博弈层", "待清理"]
EQUITY = ["红利低波", "沪深300", "中证500", "创业板指"]
STABLE = ["货币", "纯债"]
GOLD = ["黄金"]
OUT_OF_CONFIG = ["博弈层", "待清理"]
GROUPS = {"权益": EQUITY, "稳健": STABLE, "黄金": GOLD}

DEFAULT_TARGETS = {
    "货币": 10.0,
    "纯债": 20.0,
    "黄金": 15.0,
    "红利低波": 15.0,
    "沪深300": 15.0,
    "中证500": 15.0,
    "创业板指": 10.0,
    "博弈层": 0.0,
    "待清理": 0.0,
}
BASE_AGE = 23
BAND = 5.0  # 偏离阈值（pp）

TRADE_FIELDS = ["基金名称", "日期", "方向", "金额", "资产类别", "开仓逻辑", "出场逻辑", "赎回费备注"]

# Vercel / 云端环境：关闭网络行情，全部金额手动录入
IS_CLOUD = bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV") or os.getenv("FUND_APP_OFFLINE"))

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
BAND_D = Decimal(str(BAND))


# ---------------------------------------------------------------- 数值工具
def D(value, default: str = "0") -> Decimal:
    """把任意输入安全转成 Decimal（float 先转字符串，避免二进制尾数误差）。"""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal(default)
    if isinstance(value, bool):
        return Decimal(int(value))
    try:
        text = str(value).strip()
        if not text:
            return Decimal(default)
        return Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def q2(value) -> Decimal:
    """金额 / 一般百分比：保留 2 位小数，四舍五入。"""
    return D(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def q4(value) -> Decimal:
    """中间比例：保留 4 位小数，减少累计误差。"""
    return D(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def money_text(value) -> str:
    return f"{D(value):,.0f}"


def _as_int(value, default: int) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 数据规范化
def default_data() -> dict:
    return {
        "assets": {c: {"target": DEFAULT_TARGETS[c], "amount": 0.0, "name": "", "code": ""} for c in CATEGORIES},
        "monthly_budget": 2000.0,
        "age": BASE_AGE,
        "trades": [],
        "dca_plan": {},
    }


def normalize(raw: dict | None) -> dict:
    """把任意来源的原始数据整理成完整的业务结构，缺失字段补默认值。"""
    base = default_data()
    if not isinstance(raw, dict):
        return base
    assets = raw.get("assets") if isinstance(raw.get("assets"), dict) else {}
    for c in CATEGORIES:
        row = assets.get(c) if isinstance(assets.get(c), dict) else {}
        target = D(row.get("target", DEFAULT_TARGETS[c]), str(DEFAULT_TARGETS[c]))
        amount = D(row.get("amount", 0))
        # 兼容旧版本：旧数据把“名称+代码”合并在 fund 字段里
        name = str(row.get("name", "") or "").strip()
        code = re.sub(r"\D", "", str(row.get("code", "") or ""))[:6]
        legacy = str(row.get("fund", "") or "").strip()
        if legacy:
            if not code:
                matched = re.search(r"(\d{6})", legacy)
                code = matched.group(1) if matched else ""
            if not name and legacy != code:
                name = re.sub(r"\s*\d{6}\s*", " ", legacy).strip()
        base["assets"][c] = {
            "target": float(target),
            "amount": float(amount),
            "name": name,
            "code": code,
        }
    base["monthly_budget"] = float(D(raw.get("monthly_budget", 2000)))
    base["age"] = _as_int(raw.get("age", BASE_AGE), BASE_AGE) or BASE_AGE
    trades = raw.get("trades")
    base["trades"] = [t for t in trades if isinstance(t, dict)] if isinstance(trades, list) else []
    plan = raw.get("dca_plan") if isinstance(raw.get("dca_plan"), dict) else {}
    base["dca_plan"] = {c: float(q2(D(plan.get(c, 0)))) for c in EQUITY}
    return base


def fund_label(data: dict, category: str) -> str:
    item = data["assets"][category]
    return f"{item['name']} {item['code']}".strip()


# ---------------------------------------------------------------- 持仓计算
def compute_rows(data: dict) -> tuple[list[dict], Decimal]:
    """按类别计算目标金额、实际占比、偏离 pp、缺口金额。"""
    assets = data["assets"]
    total = sum((D(assets[c].get("amount")) for c in CATEGORIES), ZERO)
    rows: list[dict] = []
    for c in CATEGORIES:
        item = assets[c]
        target = q2(D(item.get("target")))
        amount = q2(D(item.get("amount")))
        target_amount = q2(total * target / HUNDRED)
        cur_pct = q4(amount / total * HUNDRED) if total > 0 else ZERO
        dev = q4(cur_pct - target)
        rows.append(
            {
                "资产类别": c,
                "基金": fund_label(data, c),
                "目标占比%": target,
                "实际占比%": cur_pct,
                "偏离pp": dev,
                "当前金额": amount,
                "目标金额": target_amount,
                "缺口金额": q2(target_amount - amount),
                "状态": "【越界观察】" if abs(dev) > BAND_D else "",
            }
        )
    return rows, q2(total)


def group_summary(rows: list[dict], total: Decimal) -> list[dict]:
    summary: list[dict] = []
    for name, members in GROUPS.items():
        sub = [r for r in rows if r["资产类别"] in members]
        target = sum((r["目标占比%"] for r in sub), ZERO)
        amount = sum((r["当前金额"] for r in sub), ZERO)
        cur = q4(amount / total * HUNDRED) if total > 0 else ZERO
        summary.append({"大类": name, "成员": "、".join(members), "目标占比%": target, "实际占比%": cur, "差额pp": q4(cur - target)})
    sub = [r for r in rows if r["资产类别"] in OUT_OF_CONFIG]
    target = sum((r["目标占比%"] for r in sub), ZERO)
    amount = sum((r["当前金额"] for r in sub), ZERO)
    cur = q4(amount / total * HUNDRED) if total > 0 else ZERO
    summary.append({"大类": "另计（配置外）", "成员": "、".join(OUT_OF_CONFIG), "目标占比%": target, "实际占比%": cur, "差额pp": q4(cur - target)})
    return summary


def rebalance_plan(rows: list[dict]) -> dict:
    """按 ±BAND 越界情况给出再平衡参考金额（只计算，不涉及任何交易）。

    返回 {"actions": [...], "sell_total", "buy_total"}；
    每一项的“建议金额”为正表示加仓、为负表示减仓，等于 目标金额 − 当前金额。
    """
    actions: list[dict] = []
    for row in rows:
        dev = q2(row["偏离pp"])
        if abs(dev) <= BAND_D:
            continue
        gap = q2(row["缺口金额"])
        if gap == ZERO:
            continue
        actions.append(
            {
                "资产类别": row["资产类别"],
                "基金": row["基金"],
                "实际占比%": row["实际占比%"],
                "目标占比%": row["目标占比%"],
                "偏离pp": dev,
                "当前金额": row["当前金额"],
                "目标金额": row["目标金额"],
                "建议金额": gap,
                "动作": "加仓" if gap > ZERO else "减仓",
            }
        )
    actions.sort(key=lambda item: abs(item["偏离pp"]), reverse=True)
    sell_total = q2(sum((a["建议金额"] for a in actions if a["建议金额"] < ZERO), ZERO))
    buy_total = q2(sum((a["建议金额"] for a in actions if a["建议金额"] > ZERO), ZERO))
    return {"actions": actions, "sell_total": sell_total, "buy_total": buy_total}


# ---------------------------------------------------------------- 生命周期 / 定投
def lifecycle(data: dict) -> dict:
    """基准年龄 23 岁：权益每年 −1pp，纯债每年 +1pp。"""
    age = _as_int(data.get("age", BASE_AGE), BASE_AGE) or BASE_AGE
    years = max(0, age - BASE_AGE)
    equity_target = sum((q2(D(data["assets"][c].get("target"))) for c in EQUITY), ZERO)
    bond_target = q2(D(data["assets"]["纯债"].get("target")))
    return {
        "age": age,
        "years": years,
        "equity_target": equity_target,
        "equity_theoretical": max(ZERO, q2(equity_target - years)),
        "bond_target": bond_target,
        "bond_theoretical": min(HUNDRED, q2(bond_target + years)),
    }


def business_days_in_month(day=None) -> int:
    """当月工作日（周一至周五）天数，用于把月预算折算成每个交易日的定投金额。"""
    import calendar
    from datetime import date as _date

    day = day or _date.today()
    _, days = calendar.monthrange(day.year, day.month)
    return sum(1 for d in range(1, days + 1) if _date(day.year, day.month, d).weekday() < 5)


def dca_rows(rows: list[dict], budget) -> tuple[list[dict], Decimal]:
    """缺口加权定投：按权益四子类的正缺口占比分配预算。"""
    budget_d = q2(D(budget))
    bdays = max(business_days_in_month(), 1)
    equity_rows = [r for r in rows if r["资产类别"] in EQUITY]
    gaps = [max(ZERO, r["缺口金额"]) for r in equity_rows]
    gap_total = sum(gaps, ZERO)
    plan: list[dict] = []
    for row, gap in zip(equity_rows, gaps):
        weight = q4(gap / gap_total * HUNDRED) if gap_total > 0 else ZERO
        monthly = q2(weight / HUNDRED * budget_d)
        plan.append(
            {
                "资产类别": row["资产类别"],
                "当前金额": row["当前金额"],
                "目标金额": row["目标金额"],
                "缺口金额": row["缺口金额"],
                "权重%": weight,
                "每月参考": monthly,
                "每日参考": q2(monthly / Decimal(bdays)),
            }
        )
    return plan, q2(gap_total)


# ---------------------------------------------------------------- 行情（复用 akshare）
def fetch_nav(code: str, with_previous: bool = True) -> dict:
    """取最新净值：优先单位净值，其次累计净值。货币基金没有单位净值，直接返回失败。

    完全沿用原有 akshare 调用，只额外读取倒数第二行用于计算单日涨跌。
    """
    import akshare as ak

    last_error = None
    for indicator, value_col in (("单位净值走势", "单位净值"), ("累计净值走势", "累计净值")):
        try:
            table = ak.fund_open_fund_info_em(symbol=code, indicator=indicator)
            if table is not None and not table.empty:
                last = table.iloc[-1]
                value = q4(D(last[value_col]))
                result = {"indicator": value_col, "value": float(value), "date": str(last.iloc[0]), "change_pct": 0.0}
                if with_previous and len(table) >= 2:
                    prev = D(table.iloc[-2][value_col])
                    if prev != 0:
                        result["change_pct"] = float(q4((value - prev) / prev * HUNDRED))
                return result
        except Exception as exc:
            last_error = exc
    raise RuntimeError(str(last_error or "no nav"))


def collect_nav(data: dict, fetcher=None) -> tuple[dict, list[str]]:
    """按持仓里已填写的基金代码批量拉净值；fetcher 可注入网页端的缓存包装。"""
    fetcher = fetcher or fetch_nav
    codes: list[str] = []
    for c in CATEGORIES:
        code = str(data["assets"][c].get("code") or "")
        if code and code not in codes:
            codes.append(code)
    nav_map: dict[str, dict] = {}
    errors: list[str] = []
    for code in codes:
        try:
            nav_map[code] = fetcher(code)
        except Exception as exc:
            errors.append(f"{code}: {exc}")
    return nav_map, errors


# ---------------------------------------------------------------- 时序快照
def build_snapshot(data: dict, nav_map: dict | None = None, day: date | str | None = None) -> dict:
    """生成单条快照：总资产、组合当日收益率、各类资产市值/仓位/涨跌、每只基金净值与涨跌。

    涨跌幅来自所填基金代码的净值变化（复用 fetch_nav 的结果），仅作参考，不参与金额计算。
    """
    nav_map = nav_map or {}
    if isinstance(day, date):
        day_text = day.isoformat()
    else:
        day_text = str(day)[:10] if day else date.today().isoformat()

    rows, total = compute_rows(data)
    detail: dict[str, dict] = {}
    fund_nav: dict[str, dict] = {}
    daily = ZERO
    nav_ok = False

    for row in rows:
        c = row["资产类别"]
        item = data["assets"][c]
        code = str(item.get("code") or "")
        change = ZERO
        info = nav_map.get(code) if code else None
        if info and info.get("value") is not None:
            nav_ok = True
            change = q4(D(info.get("change_pct", 0)))
            fund_nav[code] = {
                "name": str(item.get("name") or ""),
                "nav": float(q4(D(info["value"]))),
                "change_pct": float(change),
            }
            daily += row["实际占比%"] / HUNDRED * change
        detail[c] = {
            "market_value": float(q2(row["当前金额"])),
            "weight_pct": float(q4(row["实际占比%"])),
            "change_pct": float(change),
        }

    return {
        "date": day_text,
        "total_asset": float(q2(total)),
        "daily_return": float(q4(daily)),
        "asset_detail": detail,
        "fund_nav": fund_nav,
        "nav_ok": nav_ok,
    }


def cumulative_returns(snapshots: list[dict]) -> list[dict]:
    """按日期顺序累乘得到组合累计收益率（百分数）。"""
    ordered = sorted([s for s in snapshots if isinstance(s, dict)], key=lambda s: str(s.get("date", "")))
    factor = ONE
    rows: list[dict] = []
    for s in ordered:
        daily = q4(D(s.get("daily_return")))
        factor *= ONE + daily / HUNDRED
        rows.append(
            {
                "日期": str(s.get("date", "")),
                "总资产": q2(D(s.get("total_asset"))),
                "当日收益率%": daily,
                "累计收益率%": q4((factor - ONE) * HUNDRED),
            }
        )
    return rows


# ---------------------------------------------------------------- 复盘（规则化）
def _parse_day(value) -> date | None:
    text = str(value or "")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def build_review(data: dict, rows: list[dict], today: date | None = None) -> tuple[str, int]:
    today = today or date.today()
    trades = sorted(
        [t for t in data.get("trades", []) if isinstance(t, dict)],
        key=lambda t: str(t.get("日期", "")),
    )
    if not trades:
        return "", 0

    findings = 0
    buy = [t for t in trades if t.get("方向") == "买入"]
    sell = [t for t in trades if t.get("方向") == "卖出"]
    buy_amt = sum((D(t.get("金额")) for t in buy), ZERO)
    sell_amt = sum((D(t.get("金额")) for t in sell), ZERO)
    cats = sorted({str(t.get("资产类别", "")) for t in trades})
    days = [d for d in (_parse_day(t.get("日期")) for t in trades) if d]
    span = f"{min(days).isoformat()} → {max(days).isoformat()}" if days else "日期缺失"

    dev_map = {r["资产类别"]: r["偏离pp"] for r in rows}

    lines = ["**一、交易概览**"]
    lines.append(f"- 记录 {len(trades)} 笔（买入 {len(buy)} / 卖出 {len(sell)}），买入合计 {money_text(buy_amt)} 元，卖出合计 {money_text(sell_amt)} 元。")
    lines.append(f"- 涉及类别 {len(cats)} 个；日期跨度 {span}。")

    # 配置遵守
    out_cfg = [t for t in trades if t.get("资产类别") in OUT_OF_CONFIG]
    lines.append("")
    lines.append("**二、配置遵守**")
    lines.append(
        f"- 配置内类别（权益/稳健/黄金）交易 {len(trades) - len(out_cfg)} 笔；"
        f"配置外类别（博弈层/待清理）交易 {len(out_cfg)} 笔，占比 {len(out_cfg) / len(trades) * 100:.1f}%。"
    )
    if out_cfg:
        findings += 1
        detail = "、".join(f"{t.get('日期', '')} {t.get('基金名称', '')}" for t in out_cfg[:5])
        lines.append(f"- 配置外交易明细（最多列 5 条）：{detail}。")

    # 再平衡纪律
    buy_over = [t for t in buy if dev_map.get(t.get("资产类别"), ZERO) > BAND_D]
    sell_under = [t for t in sell if dev_map.get(t.get("资产类别"), ZERO) < -BAND_D]
    lines.append("")
    lines.append("**三、再平衡纪律（按当前持仓快照近似）**")
    lines.append(f"- 买入落在偏离 ＞ +5pp 类别的记录 {len(buy_over)} 笔；卖出落在偏离 ＜ −5pp 类别的记录 {len(sell_under)} 笔。")
    if buy_over or sell_under:
        findings += 1
        if buy_over:
            lines.append("- 越界买入明细：" + "、".join(f"{t.get('日期', '')} {t.get('资产类别', '')} {money_text(D(t.get('金额')))} 元" for t in buy_over[:5]) + "。")
        if sell_under:
            lines.append("- 低配卖出明细：" + "、".join(f"{t.get('日期', '')} {t.get('资产类别', '')} {money_text(D(t.get('金额')))} 元" for t in sell_under[:5]) + "。")

    # 记录完整性
    no_open = [t for t in buy if not str(t.get("开仓逻辑", "")).strip()]
    no_close = [t for t in sell if not str(t.get("出场逻辑", "")).strip()]
    no_fee = [t for t in sell if not str(t.get("赎回费备注", "")).strip()]
    lines.append("")
    lines.append("**四、记录完整性**")
    lines.append(f"- 买入缺“开仓逻辑” {len(no_open)} 笔；卖出缺“出场逻辑” {len(no_close)} 笔；卖出缺“赎回费备注” {len(no_fee)} 笔。")
    if no_open or no_close or no_fee:
        findings += 1

    # 频率
    recent_cut = today - timedelta(days=30)
    recent = sum(1 for d in days if d >= recent_cut)
    burst = 0
    for cat in cats:
        cat_days = sorted(
            d
            for d in (
                _parse_day(t.get("日期"))
                for t in trades
                if t.get("资产类别") == cat and t.get("方向") == "买入"
            )
            if d
        )
        for prev, cur in zip(cat_days, cat_days[1:]):
            if (cur - prev).days <= 7:
                burst += 1
    lines.append("")
    lines.append("**五、交易频率**")
    lines.append(f"- 近 30 天交易 {recent} 笔；同一类别 7 天内连续买入 {burst} 组。")

    conclusion = f"共标记 {findings} 项与既定规则不一致的记录，详见上文。" if findings else "未发现与既定规则不一致的记录。"
    lines.append("")
    lines.append("**复盘结论**：" + conclusion + "本段为事后行为回顾，不含买卖建议、择时判断或涨跌预测。")
    return "\n".join(lines), findings

# ---------------------------------------------------------------- 展示统计（数据中心图表用）
def asset_vs_invested(snapshots: list[dict], trades: list[dict]) -> list[dict]:
    """按快照日期对齐：总资产 vs 累计净投入（买入 − 卖出）。仅用于图表展示，返回 float。"""
    day_invest: dict[str, float] = {}
    for t in trades:
        day = str(t.get("日期", ""))[:10]
        if not day:
            continue
        amt = float(D(t.get("金额")))
        day_invest[day] = day_invest.get(day, 0.0) + (amt if t.get("方向") == "买入" else -amt)
    out: list[dict] = []
    run = 0.0
    for s in sorted(snapshots, key=lambda x: str(x.get("date", ""))):
        day = str(s.get("date", ""))[:10]
        run += day_invest.get(day, 0.0)
        out.append({"日期": day, "总资产": float(D(s.get("total_asset"))), "累计净投入": round(run, 2)})
    return out


def perf_stats(snapshots: list[dict]) -> dict:
    """展示用统计：累计收益率%、年化波动率%、最大回撤%。快照不足时对应字段为 None。"""
    import math
    import statistics

    rets = [float(D(s.get("daily_return"))) / 100.0 for s in sorted(snapshots, key=lambda x: str(x.get("date", "")))]
    if not rets:
        return {"累计收益率%": None, "年化波动率%": None, "最大回撤%": None}
    idx = 1.0
    curve = []
    for r in rets:
        idx *= 1.0 + r
        curve.append(idx)
    cum = (curve[-1] - 1.0) * 100.0
    vol = statistics.pstdev(rets) * math.sqrt(252) * 100.0 if len(rets) > 1 else None
    peak = curve[0]
    mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, (v / peak - 1.0) * 100.0)
    return {
        "累计收益率%": round(cum, 2),
        "年化波动率%": (round(vol, 2) if vol is not None else None),
        "最大回撤%": round(mdd, 2),
    }
