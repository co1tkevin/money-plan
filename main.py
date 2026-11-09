"""永久组合 · 场外基金配置系统（Streamlit 网页端）

仅处理场外公募基金，不含股票、不做盯盘、不自动调仓、不提供任何买卖建议。

数据读写全部经过 storage_adapter 的适配器接口，本文件不直接操作 localStorage；
所有业务计算来自 portfolio_core，与命令行脚本 snapshot_task.py 复用同一套函数。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import portfolio_core as core
from storage_adapter import (
    DATA_KEY,
    SNAPSHOT_KEY,
    HybridAdapter,
    LocalStorageAdapter,
    MemoryAdapter,
    StorageError,
    StorageNotReady,
    build_export_bundle,
    split_import_payload,
)
from supabase_backend import (
    SUPABASE_AVAILABLE,
    build_store,
    friendly_error,
    is_configured,
    login as cloud_login,
    logout as cloud_logout,
    register as cloud_register,
)

st.set_page_config(page_title="永久组合 · 场外基金配置", layout="wide")

SAVE_SLOT = st.empty()
COMPONENT_DIR = Path(__file__).parent / "ls_component"
PENDING = object()


# ---------------------------------------------------------------- 浏览器存储桥
@st.cache_resource
def _storage_component():
    try:
        return components.declare_component("pp_local_storage", path=str(COMPONENT_DIR))
    except Exception:
        return None


class BrowserStorageBridge:
    """把 Streamlit 自定义组件的 iframe 读写，包装成同步风格的 read / write。

    只负责“怎么读到浏览器的 localStorage”，不承载任何业务规则；
    LocalStorageAdapter 通过注入这两个动作实现对上层完全透明。
    """

    def __init__(self) -> None:
        self._comp = _storage_component()

    @property
    def available(self) -> bool:
        return self._comp is not None

    def read(self, purpose: str, key: str):
        cache_key = f"ls_cache_{purpose}"
        if cache_key in st.session_state:
            return st.session_state[cache_key]
        if self._comp is None:
            return None
        nonce = st.session_state.setdefault(f"ls_nonce_{purpose}", uuid.uuid4().hex[:8])
        result = self._comp(action="get", skey=key, nonce=nonce, key=f"ppls_get_{purpose}", default=None)
        if isinstance(result, dict) and result.get("nonce") == nonce:
            value = result.get("value")
            st.session_state[cache_key] = value
            return value
        return PENDING

    def write(self, purpose: str, key: str, value: str) -> bool:
        if self._comp is None:
            return False
        seq = st.session_state.get("ls_write_seq", 0) + 1
        st.session_state["ls_write_seq"] = seq
        nonce = hashlib.md5(f"{key}|{value}|{seq}".encode("utf-8")).hexdigest()[:10]
        with SAVE_SLOT.container():
            result = self._comp(
                action="set",
                skey=key,
                value=value,
                nonce=nonce,
                key=f"ppls_set_{purpose}_{nonce}",
                default=None,
            )
        if isinstance(result, dict):
            ok = bool(result.get("ok"))
        else:
            ok = True  # 组件尚未回传结果，视为已提交（下次运行会拿到确认值）
        if ok:
            # 同步会话内缓存，避免后续基于旧快照列表合并导致历史记录丢失
            st.session_state[f"ls_cache_{purpose}"] = value
        return ok


bridge = BrowserStorageBridge()


def _purpose_of(key: str) -> str:
    return "snap" if key == SNAPSHOT_KEY else "data"


def _read_storage_key(key: str):
    value = bridge.read(_purpose_of(key), key)
    if value is PENDING:
        raise StorageNotReady(key)
    return value


def _write_storage_key(key: str, value: str) -> bool:
    return bridge.write(_purpose_of(key), key, value)


if bridge.available:
    local_store = LocalStorageAdapter(_read_storage_key, _write_storage_key)
else:
    local_store = MemoryAdapter()

AUTH_KEY = "permanent_portfolio_auth_v1"


def _restore_auth():
    """登录态（token）存在浏览器本地，刷新页面后自动恢复；未登录返回 None。"""
    if "auth" in st.session_state:
        return st.session_state["auth"]
    if not bridge.available:
        st.session_state["auth"] = None
        return None
    raw = bridge.read("auth", AUTH_KEY)
    if raw is PENDING:
        st.info("正在恢复登录状态…")
        st.stop()
    auth = None
    if isinstance(raw, str) and raw.strip():
        try:
            auth = json.loads(raw)
        except (TypeError, ValueError):
            auth = None
    st.session_state["auth"] = auth
    return auth


def _persist_auth(auth: dict | None) -> None:
    if not bridge.available:
        return
    try:
        bridge.write("auth", AUTH_KEY, json.dumps(auth, ensure_ascii=False) if auth else "")
    except Exception:
        pass


def _reset_session_cache() -> None:
    """切换账号或拉取云端后，清空本次会话的业务缓存，强制重新读取。"""
    for key in ("data", "snapshots", "ls_cache_data", "ls_cache_snap", "nav_rows", "review", "storage_ok"):
        st.session_state.pop(key, None)


auth = _restore_auth()
cloud_errors: list[str] = []
store = build_store(local_store, auth, errors=cloud_errors)


# ---------------------------------------------------------------- 会话内数据
def save_data(data: dict) -> None:
    try:
        st.session_state["storage_ok"] = store.save_data(data)
    except StorageNotReady:
        st.session_state["storage_ok"] = False


def get_data() -> dict:
    if "data" in st.session_state:
        return st.session_state["data"]
    try:
        raw = store.load_data()
    except StorageNotReady:
        st.info("正在读取浏览器本地数据…（若长时间无响应，可点击下方按钮）")
        if st.button("读取不到？临时使用空配置（仅本次会话）"):
            st.session_state["data"] = core.default_data()
            st.session_state["storage_ok"] = False
            st.rerun()
        st.stop()
    data = core.normalize(raw)
    st.session_state["data"] = data
    if not raw.get("assets"):  # 首次使用：把默认结构写回存储
        save_data(data)
    return data


def get_snapshots():
    """返回快照列表；浏览器端尚未回传时返回 None。"""
    if "snapshots" in st.session_state:
        return st.session_state["snapshots"]
    try:
        snapshots = store.load_snapshots()
    except StorageNotReady:
        return None
    st.session_state["snapshots"] = snapshots
    return snapshots


# ---------------------------------------------------------------- 展示工具
def rows_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col in ("目标占比%", "实际占比%", "偏离pp", "当前金额", "目标金额", "缺口金额"):
        if col in df.columns:
            df[col] = df[col].map(float)
    return df


def num(value, fallback: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return fallback
        return float(core.q2(core.D(value)))
    except (TypeError, ValueError):
        return fallback


@st.cache_data(ttl=1800, show_spinner=False)
def cached_nav(code: str) -> dict:
    """网页端给共用 fetch_nav 加一层 Streamlit 缓存，不改变拉取逻辑。"""
    return core.fetch_nav(code)


# ---------------------------------------------------------------- 页面
st.title("永久组合 · 场外基金配置系统")
st.markdown(
    """
    <div style="border:1px solid #e6e6e6;background:#fafafa;border-radius:8px;padding:10px 14px;font-size:13px;color:#555;line-height:1.7;">
    <b>免责声明</b>：本工具仅用于个人资产配置的记录与规则自查，不构成任何投资建议，不含荐基、择时与涨跌预测，
    不执行任何自动调仓。所有金额均为手动录入的参考数据，投资决策及风险由使用者自行承担。
    </div>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------- 账号 / 云端
with st.container(border=True):
    if not SUPABASE_AVAILABLE:
        st.caption("未安装 supabase 包：当前仅使用本地存储。执行 `pip install supabase` 后即可启用云端同步。")
    elif not is_configured():
        st.caption(
            "未配置云端：在环境变量或 `.streamlit/secrets.toml` 里填写 `SUPABASE_URL` 与 `SUPABASE_ANON_KEY` 后启用。"
            "当前仅使用本地存储。"
        )
    elif auth:
        info_col, btn_col = st.columns([5, 1])
        info_col.markdown(
            f"**已登录**：{auth.get('email', '')}　｜　云端同步已启用（多端互通），本地保留离线缓存。"
        )
        if btn_col.button("退出登录", key="btn_logout", width="stretch"):
            cloud_logout()
            _persist_auth(None)
            st.session_state["auth"] = None
            _reset_session_cache()
            st.rerun()
    else:
        st.markdown(
            "**登录账号**：登录后持仓、交易流水、每日快照会同步到 Supabase，手机和电脑共用同一份数据；"
            "未登录只能本地试用。"
        )
        with st.form("login_form"):
            cols = st.columns([2, 2, 1, 1])
            email_input = cols[0].text_input("邮箱", key="login_email", placeholder="you@example.com")
            password_input = cols[1].text_input("密码", type="password", key="login_pwd")
            do_login = cols[2].form_submit_button("登录")
            do_register = cols[3].form_submit_button("注册")

        if do_login or do_register:
            email_text = (email_input or "").strip()
            if not email_text or len(password_input or "") < 6:
                st.error("请填写邮箱，密码至少 6 位。")
            elif do_login:
                try:
                    new_auth = cloud_login(email_text, password_input)
                except Exception as exc:
                    st.error(f"登录失败：{friendly_error(exc)}")
                else:
                    st.session_state["auth"] = new_auth
                    _persist_auth(new_auth)
                    _reset_session_cache()
                    st.rerun()
            else:
                try:
                    st.success(cloud_register(email_text, password_input))
                except Exception as exc:
                    st.error(f"注册失败：{friendly_error(exc)}")

if cloud_errors:
    st.warning("云端初始化失败，已自动使用本地存储：" + "；".join(friendly_error(StorageError(e)) for e in cloud_errors))

data = get_data()

# 各 tab 共用的计算结果
rows, total = core.compute_rows(data)
frame = rows_frame(rows)
target_sum = sum((r["目标占比%"] for r in rows), core.ZERO)
breaks = [r for r in rows if r["状态"]]
plan = core.rebalance_plan(rows)
snapshots = get_snapshots()

tab_dash, tab_hold, tab_trade, tab_data = st.tabs(
    ["① 总览", "② 持仓与定投", "③ 流水与复盘", "④ 数据中心"]
)

# ---------------------------------------------------------------- ① 总览
with tab_dash:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("总资产（元）", core.money_text(total))
    m2.metric("目标占比合计", f"{float(target_sum):.1f}%", delta=f"{float(target_sum) - 100:+.1f}pp 相对100%")
    m3.metric("越界观察类别", f"{len(breaks)} 类")
    equity_pct = sum((r["实际占比%"] for r in rows if r["资产类别"] in core.EQUITY), core.ZERO)
    m4.metric("权益实际占比", f"{float(equity_pct):.1f}%")

    if abs(target_sum - 100) > core.D("0.05"):
        st.warning(f"目标占比合计为 {float(target_sum):.1f}%，不等于 100%，请到 ② 页核对录入。")

    # 调仓提醒：全系统唯一提醒位，越过 ±5pp 才出现
    if plan["actions"]:
        _pieces = [
            f"{a['资产类别']} {float(a['偏离pp']):+.1f}pp → {a['动作']}约 {abs(float(a['建议金额'])):,.0f} 元"
            for a in plan["actions"]
        ]
        st.warning(
            "**需要再平衡**（越过 ±5pp）：" + "；".join(_pieces)
            + f"。合计减仓 {abs(float(plan['sell_total'])):,.0f} 元 / 加仓 {float(plan['buy_total']):,.0f} 元。"
            "金额按目标占比倒推，仅供参考，系统不执行任何交易。"
        )
    else:
        st.success("各类别均在 ±5pp 以内，暂不需要再平衡。")

    dash_left, dash_right = st.columns(2)
    with dash_left:
        st.markdown("**累计收益曲线**")
        if snapshots:
            history = core.cumulative_returns(snapshots)
            mini_df = pd.DataFrame(
                [{"日期": h["日期"], "累计收益率%": float(h["累计收益率%"])} for h in history]
            ).set_index("日期")
            st.line_chart(mini_df)
        else:
            st.caption("暂无快照；每日任务自动写入后，这里会展示组合累计收益走势。")
    with dash_right:
        st.markdown("**目标 vs 实际配置（%）**")
        cfg_df = pd.DataFrame(
            {
                "资产类别": core.CATEGORIES,
                "目标占比%": [float(r["目标占比%"]) for r in rows],
                "实际占比%": [float(r["实际占比%"]) for r in rows],
            }
        ).set_index("资产类别")
        st.bar_chart(cfg_df)

# ---------------------------------------------------------------- ② 持仓与定投
with tab_hold:
    st.subheader("持仓录入")
    st.caption("每类资产填写目标占比、当前金额、基金名称与代码；系统自动计算占比、偏离与缺口。")

    base_rows = [
        {
            "资产类别": c,
            "基金名称": data["assets"][c]["name"],
            "基金代码": data["assets"][c]["code"],
            "目标占比(%)": float(core.D(data["assets"][c]["target"])),
            "当前金额(元)": float(core.D(data["assets"][c]["amount"])),
        }
        for c in core.CATEGORIES
    ]
    edited = st.data_editor(
        pd.DataFrame(base_rows),
        hide_index=True,
        width="stretch",
        num_rows="fixed",
        key="assets_editor_v2",
        column_config={
            "资产类别": st.column_config.TextColumn(disabled=True, width="small"),
            "基金名称": st.column_config.TextColumn(help="例如：天弘沪深300ETF联接A"),
            "基金代码": st.column_config.TextColumn(help="6 位场外基金代码，例如 000961", max_chars=6),
            "目标占比(%)": st.column_config.NumberColumn(min_value=0.0, max_value=100.0, step=0.1, format="%.2f"),
            "当前金额(元)": st.column_config.NumberColumn(min_value=0.0, step=0.01, format="%.2f"),
        },
    )

    new_assets = {}
    for _, row in edited.iterrows():
        c = str(row["资产类别"])
        code = re.sub(r"\D", "", str(row["基金代码"] or ""))[:6]
        new_assets[c] = {
            "target": num(row["目标占比(%)"]),
            "amount": num(row["当前金额(元)"]),
            "name": str(row["基金名称"] or "").strip(),
            "code": code,
        }

    if json.dumps(new_assets, sort_keys=True) != json.dumps(data["assets"], sort_keys=True):
        data["assets"] = new_assets
        save_data(data)

    with st.expander("分类明细 · 再平衡计算 · 三大类汇总", expanded=False):
        st.markdown("**分类明细**")
        st.dataframe(
            frame,
            hide_index=True,
            width="stretch",
            column_config={
                "基金": st.column_config.TextColumn(width="medium"),
                "目标占比%": st.column_config.NumberColumn(format="%.2f"),
                "实际占比%": st.column_config.NumberColumn(format="%.2f"),
                "偏离pp": st.column_config.NumberColumn(format="%+.2f"),
                "当前金额": st.column_config.NumberColumn(format="%.2f"),
                "目标金额": st.column_config.NumberColumn(format="%.2f"),
                "缺口金额": st.column_config.NumberColumn(format="%.2f", help="目标金额 − 当前金额，负数表示超出目标"),
                "状态": st.column_config.TextColumn(width="small"),
            },
        )
        st.caption("偏离规则：实际占比 − 目标占比 ＞ +5pp 或 ＜ −5pp 标为【越界观察】，仅标记，系统不做任何调仓。")

        if plan["actions"]:
            st.markdown("**再平衡建议**")
            plan_df = pd.DataFrame(plan["actions"])
            for col in ("实际占比%", "目标占比%", "偏离pp", "当前金额", "目标金额", "建议金额"):
                plan_df[col] = plan_df[col].map(float)
            st.dataframe(
                plan_df,
                hide_index=True,
                width="stretch",
                column_config={
                    "基金": st.column_config.TextColumn(width="medium"),
                    "实际占比%": st.column_config.NumberColumn(format="%.2f"),
                    "目标占比%": st.column_config.NumberColumn(format="%.2f"),
                    "偏离pp": st.column_config.NumberColumn(format="%+.2f"),
                    "当前金额": st.column_config.NumberColumn(format="%.2f"),
                    "目标金额": st.column_config.NumberColumn(format="%.2f"),
                    "建议金额": st.column_config.NumberColumn(
                        format="%+.2f", help="正数=加仓，负数=减仓；按目标占比倒推，不含手续费与税费"
                    ),
                },
            )

        st.markdown("**三大类汇总**")
        summary = core.group_summary(rows, total)
        summary_df = pd.DataFrame(summary)
        for col in ("目标占比%", "实际占比%", "差额pp"):
            summary_df[col] = summary_df[col].map(float)
        st.dataframe(
            summary_df,
            hide_index=True,
            width="stretch",
            column_config={
                "目标占比%": st.column_config.NumberColumn(format="%.1f"),
                "实际占比%": st.column_config.NumberColumn(format="%.1f"),
                "差额pp": st.column_config.NumberColumn(format="%+.1f"),
            },
        )

    # 行情（本地可选）
    with st.expander("行情（可选）：本地拉取基金净值"):
        st.caption("本机可通过 akshare 拉取净值，仅供查看，不参与金额计算；持仓金额仍以手动录入为准。")
        if core.IS_CLOUD:
            st.info("云端模式：已关闭网络行情，全部金额手动录入。")
        else:
            use_online = st.toggle("使用网络行情（本机）", value=True, key="use_online")
            nav_codes = st.text_input(
                "基金代码（多个用逗号或空格分隔；留空则读取持仓里已填的代码）",
                key="nav_codes",
                placeholder="例如：000961, 110020",
            )
            if st.button("拉取最新净值", disabled=not use_online):
                try:
                    import akshare as ak  # noqa: F401
                except Exception:
                    st.error("本机未安装 akshare，无法拉取净值。可执行 pip install akshare 后再试。")
                else:
                    if nav_codes.strip():
                        codes = list(dict.fromkeys(re.findall(r"\d{6}", nav_codes)))
                    else:
                        codes = list(dict.fromkeys(data["assets"][c]["code"] for c in core.CATEGORIES if data["assets"][c]["code"]))
                    if not codes:
                        st.warning("没有可拉取的代码：请在上面输入基金代码，或先在持仓录入里填写代码。")
                    else:
                        name_map = {data["assets"][c]["code"]: data["assets"][c]["name"] for c in core.CATEGORIES}
                        nav_rows = []
                        with st.spinner("正在拉取净值…"):
                            for code in codes:
                                try:
                                    info = cached_nav(code)
                                    nav_rows.append(
                                        {
                                            "基金代码": code,
                                            "基金名称": name_map.get(code, ""),
                                            "指标": info["indicator"],
                                            "数值": info["value"],
                                            "日期": info["date"],
                                            "备注": "",
                                        }
                                    )
                                except Exception:
                                    nav_rows.append(
                                        {
                                            "基金代码": code,
                                            "基金名称": name_map.get(code, ""),
                                            "指标": "",
                                            "数值": None,
                                            "日期": "",
                                            "备注": "无单位净值（货币基金或代码有误），请核对代码",
                                        }
                                    )
                        st.session_state["nav_rows"] = nav_rows
            if "nav_rows" in st.session_state:
                st.dataframe(
                    pd.DataFrame(st.session_state["nav_rows"]),
                    hide_index=True,
                    width="stretch",
                    column_config={"数值": st.column_config.NumberColumn(format="%.4f")},
                )
            if not use_online:
                st.caption("已关闭网络行情，仅手动录入。")

    st.divider()
    st.subheader("生命周期规则")
    st.caption(f"基准年龄 {core.BASE_AGE} 岁：权益每年 −1pp，纯债每年 +1pp。此处仅展示理论目标，不修改任何持仓。")

    c1, c2, c3 = st.columns(3)
    age = c1.number_input("当前年龄", min_value=18, max_value=90, value=int(data["age"]), step=1, key="age_input")
    if int(age) != int(data["age"]):
        data["age"] = int(age)
        save_data(data)

    life = core.lifecycle(data)
    c2.metric("距基准年龄", f"{life['years']} 年")
    c3.metric(
        "理论权益目标占比",
        f"{float(life['equity_theoretical']):.1f}%",
        delta=f"{float(life['equity_theoretical'] - life['equity_target']):+.1f}pp 相对当前设定",
    )

    life_df = pd.DataFrame(
        [
            {"项目": "权益设定目标（四类合计）", "占比%": float(life["equity_target"])},
            {"项目": "权益理论目标（按年龄）", "占比%": float(life["equity_theoretical"])},
            {"项目": "纯债设定目标", "占比%": float(life["bond_target"])},
            {"项目": "纯债理论目标（按年龄）", "占比%": float(life["bond_theoretical"])},
        ]
    )
    st.dataframe(life_df, hide_index=True, width="stretch", column_config={"占比%": st.column_config.NumberColumn(format="%.1f")})
    st.caption("理论值 = 设定目标 − (当前年龄 − 23) × 1pp；仅作规则对照，是否调整由使用者自行决定。")

    st.divider()
    st.subheader("定投计划")
    st.caption(
        "「我的计划」保存后保持不变，有变化时改一次即可；建议金额按权益四类 + 黄金的缺口加权分配，"
        "每日 = 每月 ÷ 当月工作日数。不生成订单、不自动执行。"
    )

    budget = st.number_input(
        "每月定投预算（元）",
        min_value=0.0,
        max_value=1_000_000.0,
        value=float(core.D(data["monthly_budget"])),
        step=100.0,
        key="budget_input",
    )
    if float(budget) != float(core.D(data["monthly_budget"])):
        data["monthly_budget"] = float(core.q2(core.D(budget)))
        save_data(data)

    bdays = core.business_days_in_month()
    dca, gap_total = core.dca_rows(rows, budget)
    suggested_daily = {r["资产类别"]: float(core.q2(core.D(r["每月参考"]) / bdays)) for r in dca}

    if st.button("把建议金额填入我的计划", key="dca_fill"):
        data["dca_plan"] = {c: suggested_daily.get(c, 0.0) for c in core.EQUITY + core.GOLD}
        save_data(data)
        st.session_state.pop("dca_editor", None)
        st.rerun()

    current_plan = {c: float(core.q2(core.D((data.get("dca_plan") or {}).get(c, 0)))) for c in core.EQUITY + core.GOLD}
    plan_rows = [
        {
            "资产类别": c,
            "建议每日(元)": suggested_daily.get(c, 0.0),
            "我的计划·每日(元)": current_plan[c],
        }
        for c in core.EQUITY + core.GOLD
    ]
    edited_plan = st.data_editor(
        pd.DataFrame(plan_rows),
        hide_index=True,
        width="stretch",
        num_rows="fixed",
        key="dca_editor",
        column_config={
            "资产类别": st.column_config.TextColumn(disabled=True, width="small"),
            "建议每日(元)": st.column_config.NumberColumn(format="%.2f", disabled=True, help="按缺口加权：每月参考 ÷ 本月工作日数"),
            "我的计划·每日(元)": st.column_config.NumberColumn(min_value=0.0, step=1.0, format="%.2f"),
        },
    )

    new_plan = {}
    for _, prow in edited_plan.iterrows():
        new_plan[str(prow["资产类别"])] = float(core.q2(core.D(prow["我的计划·每日(元)"])))
    if json.dumps(new_plan, sort_keys=True) != json.dumps(current_plan, sort_keys=True):
        data["dca_plan"] = new_plan
        save_data(data)

    daily_total = sum(new_plan.values())
    if gap_total > 0:
        st.caption(
            f"可分配缺口合计 {core.money_text(gap_total)} 元；月预算 {core.money_text(budget)} 元，本月 {bdays} 个工作日。"
            f"我的计划合计：每日 {daily_total:,.2f} 元 ≈ 每月 {daily_total * bdays:,.2f} 元。"
        )
    else:
        st.info("权益子类当前无正缺口（或未录入金额），建议金额不分配；我的计划仍可按自定义金额保存。")

    # 一键登记：把今日计划金额计入持仓 + 写买入流水；同一天只允许登记一次
    today_str = str(date.today())
    already_logged = any(
        str(t.get("日期", "")) == today_str and str(t.get("开仓逻辑", "")) == "每日定投"
        for t in data.get("trades", [])
    )
    if st.button("一键登记今日定投（计入持仓 + 流水）", key="btn_dca_apply", width="stretch"):
        entries = {c: core.q2(core.D(new_plan.get(c, 0))) for c in core.EQUITY + core.GOLD}
        entries = {c: amt for c, amt in entries.items() if amt > 0}
        if not entries:
            st.warning("我的计划里还没有金额，先在上方表格里填每日金额。")
        elif already_logged:
            st.warning("今天已经登记过定投了，不能重复计入。")
        else:
            for cat, amt in entries.items():
                current = core.q2(core.D(data["assets"][cat]["amount"]))
                data["assets"][cat]["amount"] = float(current + amt)
                data["trades"].append(
                    {
                        "基金名称": core.fund_label(data, cat),
                        "日期": today_str,
                        "方向": "买入",
                        "金额": float(amt),
                        "资产类别": cat,
                        "开仓逻辑": "每日定投",
                        "出场逻辑": "",
                        "赎回费备注": "",
                    }
                )
            save_data(data)
            st.success(f"已登记今日定投：{sum(entries.values()):,.2f} 元，共 {len(entries)} 笔。")
            st.rerun()
    if already_logged:
        st.caption("今日定投已登记。")

    with st.expander("建议分配明细（缺口 / 权重 / 每月参考）", expanded=False):
        dca_df = pd.DataFrame(dca)
        for col in ("当前金额", "目标金额", "缺口金额", "权重%", "每月参考", "每日参考"):
            dca_df[col] = dca_df[col].map(float)
        st.dataframe(
            dca_df,
            hide_index=True,
            width="stretch",
            column_config={
                "当前金额": st.column_config.NumberColumn(format="%.2f"),
                "目标金额": st.column_config.NumberColumn(format="%.2f"),
                "缺口金额": st.column_config.NumberColumn(format="%.2f"),
                "权重%": st.column_config.NumberColumn(format="%.1f"),
                "每月参考": st.column_config.NumberColumn(format="%.2f"),
                "每日参考": st.column_config.NumberColumn(format="%.2f"),
            },
        )

# ---------------------------------------------------------------- ③ 流水与复盘
with tab_trade:
    st.subheader("基金交易流水")

    with st.form("trade_form", clear_on_submit=True):
        f1, f2, f3, f4 = st.columns(4)
        name = f1.text_input("基金名称")
        tdate = f2.date_input("日期", value=date.today())
        direction = f3.selectbox("买卖方向", ["买入", "卖出"])
        amount = f4.number_input("金额（元）", min_value=0.0, step=0.01, value=0.0, format="%.2f")
        f5, f6 = st.columns(2)
        category = f5.selectbox("归属资产类别", core.CATEGORIES)
        fee_note = f6.text_input("赎回费备注", placeholder="如：持有 7 天内 1.5% / 无")
        open_logic = st.text_input("开仓逻辑", placeholder="如：缺口加权定投，权益低于目标")
        close_logic = st.text_input("出场逻辑", placeholder="如：偏离 +6pp，按再平衡规则减持")
        submitted = st.form_submit_button("记录这笔交易")

    if submitted:
        if not name.strip():
            st.error("请填写基金名称。")
        elif amount <= 0:
            st.error("金额需大于 0。")
        else:
            data["trades"].append(
                {
                    "基金名称": name.strip(),
                    "日期": str(tdate),
                    "方向": direction,
                    "金额": float(core.q2(core.D(amount))),
                    "资产类别": category,
                    "开仓逻辑": open_logic.strip(),
                    "出场逻辑": close_logic.strip(),
                    "赎回费备注": fee_note.strip(),
                }
            )
            save_data(data)
            st.rerun()

    trades = sorted(data["trades"], key=lambda t: str(t.get("日期", "")), reverse=True)
    if not trades:
        st.info("暂无交易记录。")
    else:
        tdf = pd.DataFrame([{k: t.get(k, "") for k in core.TRADE_FIELDS} for t in trades])
        tdf["日期"] = pd.to_datetime(tdf["日期"], errors="coerce")

        q1, q2, q3, q4 = st.columns([2, 1, 1, 1])
        cats = q1.multiselect("筛选：资产类别", core.CATEGORIES, default=core.CATEGORIES, key="filter_cat")
        dirs = q2.selectbox("筛选：方向", ["全部", "买入", "卖出"], key="filter_dir")
        valid_dates = tdf["日期"].dropna()
        dmin = valid_dates.min().date() if not valid_dates.empty else date.today() - timedelta(days=365)
        dmax = valid_dates.max().date() if not valid_dates.empty else date.today()
        start = q3.date_input("起始日期", value=dmin, key="filter_start")
        end = q4.date_input("结束日期", value=dmax, key="filter_end")

        mask = tdf["资产类别"].isin(cats)
        if dirs != "全部":
            mask &= tdf["方向"] == dirs
        mask &= tdf["日期"].dt.date.between(start, end)
        view = tdf[mask].copy()
        view["日期"] = view["日期"].dt.strftime("%Y-%m-%d")

        st.dataframe(view, hide_index=True, width="stretch", column_config={"金额": st.column_config.NumberColumn(format="%.2f")})
        st.caption(f"筛选结果 {len(view)} 条 / 全部 {len(tdf)} 条。")

        d1, d2 = st.columns([1, 3])
        csv_bytes = view.to_csv(index=False).encode("utf-8-sig")
        d1.download_button("导出 CSV", data=csv_bytes, file_name=f"基金交易流水_{date.today()}.csv", mime="text/csv", width="stretch")
        with d2.expander("记录管理"):
            if st.button("撤销最近一条录入"):
                data["trades"].pop()
                save_data(data)
                st.rerun()

    st.divider()
    st.subheader("AI复盘 · 事后行为回顾")
    st.caption("仅依据交易流水与当前持仓，核对是否遵守配置与再平衡规则；只做行为总结，不输出任何买卖建议、不预测涨跌。")

    if not data["trades"]:
        st.info("暂无交易记录，暂无可复盘内容。")
    else:
        if st.button("生成复盘"):
            report, findings = core.build_review(data, rows)
            st.session_state["review"] = (report, findings)
        if "review" in st.session_state:
            report, findings = st.session_state["review"]
            if findings:
                st.warning(f"本次复盘共标记 {findings} 项与既定规则不一致的记录。")
            else:
                st.success("本次复盘未发现与既定规则不一致的记录。")
            st.markdown(report)

# ---------------------------------------------------------------- ④ 数据中心
with tab_data:
    st.subheader("数据中心")
    st.caption(f"每日快照由定时任务自动写入（约每天 22:30）；图表基于全部历史快照计算。当前存储：{store.label}。")

    flash = st.session_state.pop("snap_flash", None)
    if flash:
        st.success(flash)
    sync_flash = st.session_state.pop("sync_flash", None)
    if sync_flash:
        st.success(sync_flash)

    snap_ready = snapshots is not None

    with st.expander("手动补一条今日快照（一般由定时任务自动写入）", expanded=False):
        if not snap_ready:
            st.info("正在读取历史快照…")
        if core.IS_CLOUD:
            use_net = False
            st.caption("云端模式：已关闭网络行情，快照中的单日涨跌按 0 记录。")
        else:
            use_net = st.toggle("拉取最新净值计算单日涨跌", value=True, key="snap_use_net")
        if st.button("保存今日快照", type="primary", disabled=not snap_ready):
            nav_map: dict = {}
            errors: list[str] = []
            if use_net:
                with st.spinner("正在拉取净值…"):
                    nav_map, errors = core.collect_nav(data, cached_nav)
            snapshot = core.build_snapshot(data, nav_map)
            try:
                updated = store.save_snapshot(snapshot)
            except StorageNotReady:
                st.error("浏览器存储尚未就绪，请稍后重试。")
            else:
                st.session_state["snapshots"] = updated
                snapshots = updated
                message = (
                    f"已保存 {snapshot['date']} 快照：总资产 {core.money_text(snapshot['total_asset'])} 元，"
                    f"组合当日 {snapshot['daily_return']:+.2f}%。"
                )
                st.session_state["snap_flash"] = message
                st.success(message)
                if errors:
                    st.warning("以下基金净值拉取失败，单日涨跌按 0 计：" + "；".join(errors[:6]))

    if not snapshots:
        st.info("暂无快照记录。等待每日任务写入，或在上方手动补一条。")
    else:
        history = core.cumulative_returns(snapshots)
        hist_df = pd.DataFrame(
            [
                {
                    "日期": h["日期"],
                    "总资产（元）": float(h["总资产"]),
                    "当日收益率%": float(h["当日收益率%"]),
                    "累计收益率%": float(h["累计收益率%"]),
                }
                for h in history
            ]
        )
        stats = core.perf_stats(snapshots)
        s1, s2, s3 = st.columns(3)
        s1.metric("累计收益率", f"{stats['累计收益率%']:+.2f}%" if stats["累计收益率%"] is not None else "—")
        s2.metric("最大回撤", f"{stats['最大回撤%']:.2f}%" if stats["最大回撤%"] is not None else "—")
        s3.metric("年化波动率", f"{stats['年化波动率%']:.2f}%" if stats["年化波动率%"] is not None else "—")

        st.markdown("**总资产 vs 累计净投入**")
        avi_df = pd.DataFrame(core.asset_vs_invested(snapshots, data["trades"])).set_index("日期")
        st.line_chart(avi_df)
        st.caption("累计净投入 = 交易流水按日累加（买入 − 卖出）；两线之差即浮盈/浮亏，流水不全时仅供参考。")

        st.markdown("**每日涨跌（%）**")
        st.bar_chart(hist_df.set_index("日期")[["当日收益率%"]])

        st.markdown("**仓位漂移（堆叠面积，%）**")
        drift_rows = []
        for s in sorted(snapshots, key=lambda x: str(x.get("date", ""))):
            detail = s.get("asset_detail") if isinstance(s.get("asset_detail"), dict) else {}
            row = {"日期": str(s.get("date", ""))}
            for c in core.CATEGORIES:
                row[c] = float(core.D((detail.get(c) or {}).get("weight_pct")))
            drift_rows.append(row)
        drift_df = pd.DataFrame(drift_rows).set_index("日期")
        st.area_chart(drift_df)
        st.caption("单位为仓位占比（%），按快照日期排列；用于观察各类资产仓位随时间的变化。")

        st.markdown("**历史快照明细**")
        st.dataframe(
            hist_df.iloc[::-1],
            hide_index=True,
            width="stretch",
            column_config={
                "总资产（元）": st.column_config.NumberColumn(format="%.2f"),
                "当日收益率%": st.column_config.NumberColumn(format="%+.2f"),
                "累计收益率%": st.column_config.NumberColumn(format="%+.2f"),
            },
        )
        st.caption(f"共 {len(history)} 条快照；累计收益率按每日收益率连乘计算，仅反映已保存快照的区间表现。")

        picked = st.selectbox("查看某日仓位明细", list(drift_df.index[::-1]), key="snap_pick")
        picked_snap = next((s for s in snapshots if str(s.get("date", "")) == picked), None)
        if picked_snap:
            detail = picked_snap.get("asset_detail") if isinstance(picked_snap.get("asset_detail"), dict) else {}
            target_map = {c: core.D(data["assets"][c]["target"]) for c in core.CATEGORIES}
            detail_rows = []
            for c in core.CATEGORIES:
                info = detail.get(c) or {}
                weight = core.q4(core.D(info.get("weight_pct")))
                detail_rows.append(
                    {
                        "资产类别": c,
                        "市值（元）": float(core.q2(core.D(info.get("market_value")))),
                        "仓位占比%": float(weight),
                        "单日涨跌%": float(core.q4(core.D(info.get("change_pct")))),
                        "当前目标%": float(target_map[c]),
                        "相对目标偏离pp": float(core.q4(weight - target_map[c])),
                    }
                )
            st.dataframe(
                pd.DataFrame(detail_rows),
                hide_index=True,
                width="stretch",
                column_config={
                    "市值（元）": st.column_config.NumberColumn(format="%.2f"),
                    "仓位占比%": st.column_config.NumberColumn(format="%.2f"),
                    "单日涨跌%": st.column_config.NumberColumn(format="%+.2f"),
                    "当前目标%": st.column_config.NumberColumn(format="%.2f"),
                    "相对目标偏离pp": st.column_config.NumberColumn(format="%+.2f"),
                },
            )
            fund_nav = picked_snap.get("fund_nav") if isinstance(picked_snap.get("fund_nav"), dict) else {}
            if fund_nav:
                st.caption("当日基金净值：")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {"基金代码": code, "基金名称": v.get("name", ""), "当日净值": v.get("nav"), "单日涨跌%": v.get("change_pct")}
                            for code, v in fund_nav.items()
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "当日净值": st.column_config.NumberColumn(format="%.4f"),
                        "单日涨跌%": st.column_config.NumberColumn(format="%+.2f"),
                    },
                )

    st.divider()
    st.markdown("**云端同步**")
    if isinstance(store, HybridAdapter):
        if store.online:
            st.caption("状态：云端已连接 —— 读写以 Supabase 为准，同时保留本地离线缓存。")
        else:
            st.warning("状态：云端连接失败，已自动降级为本地缓存，修改不会丢失。恢复网络后可手动同步。")
            if store.last_error:
                st.caption(f"最近错误：{store.last_error[:200]}")
        sync1, sync2 = st.columns(2)
        if sync1.button("同步上传（本地 → 云端）", width="stretch", key="btn_push"):
            try:
                pushed = store.push_to_cloud()
            except Exception as exc:
                st.error(f"上传失败：{friendly_error(exc)}")
            else:
                st.success(f"已上传主数据（{pushed['trades']} 笔交易）与 {pushed['snapshots']} 条快照。")
        if sync2.button("从云端拉取（云端 → 本地）", width="stretch", key="btn_pull"):
            try:
                pulled = store.pull_from_cloud()
            except Exception as exc:
                st.error(f"拉取失败：{friendly_error(exc)}")
            else:
                st.session_state["sync_flash"] = f"已从云端拉取 {pulled['snapshots']} 条快照。"
                _reset_session_cache()
                st.rerun()
        if store.pending_upload:
            st.info("本地有改动尚未写入云端（离线期间或首次登录）。可点击【同步上传】把本地数据推到云端。")
    elif auth:
        st.caption("已登录，但云端客户端未就绪（未安装 supabase 包或缺少配置），当前仍用本地存储。")
    else:
        st.info("未登录：当前只使用本地缓存，数据不会上传。登录后可用【同步上传/拉取】实现多端互通。")

    st.divider()
    st.markdown("**数据备份与迁移**")
    bundle = build_export_bundle(data, snapshots or [])
    st.download_button(
        "导出全量业务数据（JSON）",
        data=json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name=f"永久组合_备份_{date.today().isoformat()}.json",
        mime="application/json",
    )
    st.caption("备份包含主数据与全部快照；也可直接作为 snapshot_task.py 的 --data-file 输入。")

    uploaded = st.file_uploader("导入 JSON 备份（覆盖主数据，快照按日期合并）", type=["json"], key="import_file")
    if uploaded is not None:
        try:
            payload = json.loads(uploaded.getvalue().decode("utf-8"))
            imported_data, imported_snaps = split_import_payload(payload)
        except (ValueError, StorageError) as exc:
            st.error(f"解析失败：{exc}")
        else:
            st.caption(f"待导入：{len(imported_data.get('assets', {}))} 类资产、{len(imported_snaps)} 条快照。")
            import_scope = st.radio(
                "导入方式",
                ["仅本地", "本地 + 同步写入云端"],
                horizontal=True,
                key="import_scope",
                help="选择“本地 + 云端”时会把导入结果同时写入 Supabase（需已登录）。",
            )
            if import_scope != "仅本地" and not isinstance(store, HybridAdapter):
                st.caption("当前未登录或云端不可用，本次导入只会写入本地。")
            if st.button("确认导入（覆盖当前主数据）"):
                merged_data = core.normalize(imported_data)
                to_cloud = import_scope != "仅本地" and isinstance(store, HybridAdapter)
                current = snapshots or []
                try:
                    if isinstance(store, HybridAdapter):
                        store.save_data(merged_data, to_cloud=to_cloud)
                        for item in imported_snaps:
                            current = store.save_snapshot(item, to_cloud=to_cloud)
                    else:
                        store.save_data(merged_data)
                        for item in imported_snaps:
                            current = store.save_snapshot(item)
                except StorageNotReady:
                    st.error("浏览器存储尚未就绪，请稍后重试。")
                except Exception as exc:
                    st.error(f"导入失败：{friendly_error(exc)}")
                else:
                    st.session_state["data"] = merged_data
                    st.session_state["snapshots"] = current
                    st.rerun()

    st.caption(f"存储适配器：{store.label}（本地缓存键 {DATA_KEY} / {SNAPSHOT_KEY}）。")

st.divider()
if not bridge.available:
    st.caption("浏览器存储组件不可用：当前数据仅保存在本次会话，刷新后会丢失。")
elif st.session_state.get("storage_ok") is False:
    st.caption("本地存储不可用：当前修改仅保留在本次会话，刷新后会丢失。")
else:
    st.caption(f"数据存储：{store.label}。")
