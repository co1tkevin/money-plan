"""存储抽象层（多端互通的核心解耦点）

上层（main.py / snapshot_task.py）只允许调用这里定义的 StorageAdapter 四个方法：
    load_data()      -> dict            读取业务主数据
    save_data(data)  -> bool            保存业务主数据
    load_snapshots() -> list[dict]      读取时序快照列表
    save_snapshot(snapshot) -> list[dict] 保存单条快照（同日覆盖），返回保存后的完整列表

当前实现：
    LocalStorageAdapter  浏览器 localStorage（网页端，读写动作由调用方注入，不依赖 Streamlit）
    JsonFileAdapter      本地 JSON 文件（命令行快照脚本）
    MemoryAdapter        仅本次会话的内存实现（浏览器组件不可用时降级）

未来接入手机 / 电脑多端互通：新增一个 HttpApiAdapter 实现同样四个方法，
然后只需在 main.py / snapshot_task.py 的构造处替换适配器类型，
业务计算（portfolio_core.py）与页面代码完全不用改。
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# 主数据沿用旧的 localStorage key，保证已录入的数据不丢失
DATA_KEY = "permanent_portfolio_fund_v1"
SNAPSHOT_KEY = "permanent_portfolio_snapshots_v1"

ReadFn = Callable[[str], "str | None"]
WriteFn = Callable[[str, str], bool]


class StorageNotReady(RuntimeError):
    """浏览器端数据尚未回传（自定义组件首次渲染需要一次页面往返）。"""


class StorageError(RuntimeError):
    """存储读写失败。"""


# ---------------------------------------------------------------- 抽象接口
class StorageAdapter(ABC):
    label = "存储适配器"

    @abstractmethod
    def load_data(self) -> dict:
        """返回业务主数据（原样，不做业务规范化）。"""

    @abstractmethod
    def save_data(self, data: dict) -> bool:
        """整体覆盖保存业务主数据。"""

    @abstractmethod
    def load_snapshots(self) -> list[dict]:
        """返回全部时序快照，按日期升序。"""

    @abstractmethod
    def save_snapshot(self, snapshot: dict) -> list[dict]:
        """保存一条快照；同一天重复保存时覆盖旧记录。返回保存后的完整列表。"""


# ---------------------------------------------------------------- 工具
def _loads(raw, default):
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _merge_snapshot(snapshots: list[dict], snapshot: dict) -> list[dict]:
    day = str(snapshot.get("date", ""))
    merged = [s for s in snapshots if str(s.get("date", "")) != day]
    merged.append(dict(snapshot))
    merged.sort(key=lambda s: str(s.get("date", "")))
    return merged


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 浏览器 localStorage
class LocalStorageAdapter(StorageAdapter):
    """网页端适配器。真正的读写动作由 main.py 注入（封装 Streamlit 自定义组件）。"""

    label = "浏览器 localStorage"

    def __init__(self, read: ReadFn, write: WriteFn, data_key: str = DATA_KEY, snapshot_key: str = SNAPSHOT_KEY):
        self._read = read
        self._write = write
        self.data_key = data_key
        self.snapshot_key = snapshot_key

    def load_data(self) -> dict:
        raw = _loads(self._read(self.data_key), {})
        return raw if isinstance(raw, dict) else {}

    def save_data(self, data: dict) -> bool:
        return bool(self._write(self.data_key, _dumps(data)))

    def load_snapshots(self) -> list[dict]:
        raw = _loads(self._read(self.snapshot_key), [])
        if not isinstance(raw, list):
            return []
        return [s for s in raw if isinstance(s, dict)]

    def save_snapshot(self, snapshot: dict) -> list[dict]:
        merged = _merge_snapshot(self.load_snapshots(), snapshot)
        if not self._write(self.snapshot_key, json.dumps(merged, ensure_ascii=False)):
            raise StorageError("快照写入浏览器存储失败")
        return merged


# ---------------------------------------------------------------- 本地 JSON 文件
class JsonFileAdapter(StorageAdapter):
    """命令行脚本使用：主数据 + 快照分别落在本地 JSON 文件。"""

    label = "本地 JSON 文件"

    def __init__(self, data_path: str | Path, snapshot_path: str | Path):
        self.data_path = Path(data_path)
        self.snapshot_path = Path(snapshot_path)

    def load_data(self) -> dict:
        if not self.data_path.exists():
            raise StorageError(
                f"主数据文件不存在：{self.data_path}\n"
                "请先在网页端【⑤ 数据与快照】页导出 JSON 备份，保存到该路径后重试。"
            )
        raw = _loads(self.data_path.read_text(encoding="utf-8"), {})
        # 兼容直接使用网页端的全量导出文件
        if isinstance(raw, dict) and isinstance(raw.get("data"), dict) and "assets" not in raw:
            raw = raw["data"]
        if not isinstance(raw, dict):
            raise StorageError(f"主数据文件格式不正确：{self.data_path}")
        return raw

    def save_data(self, data: dict) -> bool:
        _atomic_write(self.data_path, json.dumps(data, ensure_ascii=False, indent=2))
        return True

    def load_snapshots(self) -> list[dict]:
        if not self.snapshot_path.exists():
            return []
        raw = _loads(self.snapshot_path.read_text(encoding="utf-8"), [])
        if not isinstance(raw, list):
            return []
        return [s for s in raw if isinstance(s, dict)]

    def save_snapshot(self, snapshot: dict) -> list[dict]:
        merged = _merge_snapshot(self.load_snapshots(), snapshot)
        _atomic_write(self.snapshot_path, json.dumps(merged, ensure_ascii=False, indent=2))
        return merged


# ---------------------------------------------------------------- 内存（降级）
class MemoryAdapter(StorageAdapter):
    """浏览器组件不可用时的临时实现：只在当前会话有效。"""

    label = "临时内存（仅本次会话）"

    def __init__(self, data: dict | None = None, snapshots: list[dict] | None = None):
        self._data = dict(data or {})
        self._snapshots = list(snapshots or [])

    def load_data(self) -> dict:
        return dict(self._data)

    def save_data(self, data: dict) -> bool:
        self._data = dict(data)
        return True

    def load_snapshots(self) -> list[dict]:
        return [dict(s) for s in self._snapshots]

    def save_snapshot(self, snapshot: dict) -> list[dict]:
        self._snapshots = _merge_snapshot(self._snapshots, snapshot)
        return [dict(s) for s in self._snapshots]


# ---------------------------------------------------------------- 工厂（未来扩展点）
def create_adapter(kind: str, **kwargs) -> StorageAdapter:
    """按类型构造适配器。

    未来新增 http-api 适配器时，只需在这里加一个分支，
    例如 kind="http" -> HttpApiAdapter(base_url=..., token=...)，其它调用处保持不变。
    """
    kind = (kind or "").strip().lower()
    if kind in ("localstorage", "local_storage", "browser"):
        return LocalStorageAdapter(kwargs["read"], kwargs["write"], **{k: v for k, v in kwargs.items() if k.endswith("_key")})
    if kind in ("jsonfile", "json_file", "file"):
        return JsonFileAdapter(kwargs["data_path"], kwargs["snapshot_path"])
    if kind == "memory":
        return MemoryAdapter(kwargs.get("data"), kwargs.get("snapshots"))
    if kind == "http":
        raise StorageError("http 适配器尚未实现：当前阶段只做抽象预留，不实现后端服务与同步接口。")
    raise StorageError(f"未知的存储适配器类型：{kind}")


# ---------------------------------------------------------------- 导出 / 导入
def build_export_bundle(data: dict, snapshots: list[dict]) -> dict:
    """全量备份包：主数据 + 全部快照，可用于换机迁移或喂给命令行脚本。"""
    return {
        "app": "permanent_portfolio",
        "version": 1,
        "exported_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "data": data,
        "snapshots": [s for s in snapshots if isinstance(s, dict)],
    }


def split_import_payload(raw) -> tuple[dict, list[dict]]:
    """解析导入内容：既支持全量备份包，也支持只有主数据的裸对象。"""
    if not isinstance(raw, dict):
        raise StorageError("导入内容必须是 JSON 对象")
    if isinstance(raw.get("data"), dict) and "assets" not in raw:
        snaps = raw.get("snapshots") or []
        return raw["data"], [s for s in snaps if isinstance(s, dict)]
    if "assets" not in raw:
        raise StorageError("导入内容缺少 assets 字段，无法识别为业务数据")
    return raw, []


# ---------------------------------------------------------------- Supabase 云端
TRADE_DB_FIELDS = {
    "基金名称": "fund_name",
    "日期": "trade_date",
    "方向": "direction",
    "金额": "amount",
    "资产类别": "category",
    "开仓逻辑": "open_logic",
    "出场逻辑": "close_logic",
    "赎回费备注": "fee_note",
}
TRADE_APP_FIELDS = {v: k for k, v in TRADE_DB_FIELDS.items()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SupabaseAdapter(StorageAdapter):
    """云端主存储，对应三张表（均已启用 user_id 行级权限 RLS）：

    user_portfolio        每个用户一行：assets jsonb + monthly_budget + age
    user_trades           每个用户多行：一笔交易一行
    user_daily_snapshot   每个用户多行：(user_id, snapshot_date) 唯一，一天一条快照

    客户端由上层注入（网页端用登录后的 anon 客户端，定时任务可用 service_role），
    本类只负责字段映射与读写，不关心网络来源。
    """

    label = "Supabase 云端"
    PORTFOLIO_TABLE = "user_portfolio"
    TRADES_TABLE = "user_trades"
    SNAPSHOT_TABLE = "user_daily_snapshot"

    def __init__(self, client, user_id: str):
        self.client = client
        self.user_id = str(user_id)

    # -- 内部 --------------------------------------------------
    def _select(self, table: str, order: str | None = None):
        query = self.client.table(table).select("*").eq("user_id", self.user_id)
        if order:
            query = query.order(order)
        return query.execute().data or []

    @staticmethod
    def _trade_to_row(trade: dict) -> dict:
        row = {"amount": float(trade.get("金额") or 0) or 0.0}
        for app_key, db_key in TRADE_DB_FIELDS.items():
            value = trade.get(app_key, "")
            if app_key == "金额":
                continue
            if db_key == "trade_date":
                text = str(value or "")[:10]
                row[db_key] = text or None
            else:
                row[db_key] = str(value or "")
        return row

    @staticmethod
    def _row_to_trade(row: dict) -> dict:
        trade = {}
        for db_key, app_key in TRADE_APP_FIELDS.items():
            value = row.get(db_key)
            if app_key == "金额":
                trade[app_key] = float(value or 0)
            elif app_key == "日期":
                trade[app_key] = str(value or "")[:10]
            else:
                trade[app_key] = str(value or "")
        return trade

    # -- 接口实现 ----------------------------------------------
    def load_data(self) -> dict:
        try:
            rows = self._select(self.PORTFOLIO_TABLE)
            if not rows:
                return {}
            row = rows[0]
            trades = self._select(self.TRADES_TABLE, order="trade_date")
        except Exception as exc:
            raise StorageError(f"读取云端数据失败：{exc}") from exc
        return {
            "assets": row.get("assets") or {},
            "monthly_budget": row.get("monthly_budget"),
            "age": row.get("age"),
            "trades": [self._row_to_trade(t) for t in trades],
        }

    def save_data(self, data: dict) -> bool:
        payload = {
            "user_id": self.user_id,
            "assets": data.get("assets") or {},
            "monthly_budget": float(data.get("monthly_budget") or 0),
            "age": int(data.get("age") or 0),
            "updated_at": _now_iso(),
        }
        rows = [dict(self._trade_to_row(t), user_id=self.user_id) for t in (data.get("trades") or []) if isinstance(t, dict)]
        try:
            self.client.table(self.PORTFOLIO_TABLE).upsert(payload, on_conflict="user_id").execute()
            self.client.table(self.TRADES_TABLE).delete().eq("user_id", self.user_id).execute()
            if rows:
                self.client.table(self.TRADES_TABLE).insert(rows).execute()
        except Exception as exc:
            raise StorageError(f"写入云端失败：{exc}") from exc
        return True

    def load_snapshots(self) -> list[dict]:
        try:
            rows = self._select(self.SNAPSHOT_TABLE, order="snapshot_date")
        except Exception as exc:
            raise StorageError(f"读取云端快照失败：{exc}") from exc
        snapshots: list[dict] = []
        for row in rows:
            snapshots.append(
                {
                    "date": str(row.get("snapshot_date") or "")[:10],
                    "total_asset": float(row.get("total_asset") or 0),
                    "daily_return": float(row.get("daily_return") or 0),
                    "asset_detail": row.get("asset_detail") or {},
                    "fund_nav": row.get("fund_nav") or {},
                    "nav_ok": bool(row.get("nav_ok")),
                }
            )
        return snapshots

    def save_snapshot(self, snapshot: dict) -> list[dict]:
        row = {
            "user_id": self.user_id,
            "snapshot_date": str(snapshot.get("date", ""))[:10],
            "total_asset": float(snapshot.get("total_asset") or 0),
            "daily_return": float(snapshot.get("daily_return") or 0),
            "asset_detail": snapshot.get("asset_detail") or {},
            "fund_nav": snapshot.get("fund_nav") or {},
            "nav_ok": bool(snapshot.get("nav_ok")),
            "updated_at": _now_iso(),
        }
        try:
            self.client.table(self.SNAPSHOT_TABLE).upsert(row, on_conflict="user_id,snapshot_date").execute()
        except Exception as exc:
            raise StorageError(f"写入云端快照失败：{exc}") from exc
        return self.load_snapshots()


# ---------------------------------------------------------------- 云端 + 本地缓存
class HybridAdapter(StorageAdapter):
    """联网优先读写 Supabase；任何云端异常自动降级到 localStorage 缓存。

    - 读：云端成功则顺带回写本地缓存；云端失败或无数据则读本地
    - 写：先写本地缓存，再写云端；云端失败只标记 pending，不阻断用户操作
    - 同步：push_to_cloud() / pull_from_cloud() 供【同步上传/拉取】按钮调用
    """

    label = "Supabase 云端 + 本地缓存"

    def __init__(self, remote: StorageAdapter, local: StorageAdapter):
        self.remote = remote
        self.local = local
        self.mode = "cloud"  # cloud / offline
        self.last_error = ""
        self.pending_upload = False

    @property
    def online(self) -> bool:
        return self.mode == "cloud"

    def _call_remote(self, func, *args, **kwargs):
        try:
            result = func(*args, **kwargs)
            self.mode = "cloud"
            self.last_error = ""
            return True, result
        except Exception as exc:
            self.mode = "offline"
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False, None

    def _cache_data(self, data: dict) -> None:
        try:
            self.local.save_data(data)
        except Exception:
            pass

    # -- 接口实现 ----------------------------------------------
    def load_data(self) -> dict:
        if self.online:
            ok, data = self._call_remote(self.remote.load_data)
            if ok and isinstance(data, dict) and data.get("assets"):
                self._cache_data(data)
                return data
            if ok:
                self.pending_upload = True  # 云端还没有数据，等用户上传本地
        return self.local.load_data()

    def save_data(self, data: dict, to_cloud: bool = True) -> bool:
        local_ok = True
        try:
            self.local.save_data(data)
        except Exception:
            local_ok = False
        if not to_cloud:
            self.pending_upload = True
            return local_ok
        ok, _ = self._call_remote(self.remote.save_data, data)
        if not ok:
            self.pending_upload = True
        return ok or local_ok

    def load_snapshots(self) -> list[dict]:
        if self.online:
            ok, snapshots = self._call_remote(self.remote.load_snapshots)
            if ok and snapshots:
                return snapshots
            if ok:
                local_snaps = self.local.load_snapshots()
                if local_snaps:
                    self.pending_upload = True
                return local_snaps
        return self.local.load_snapshots()

    def save_snapshot(self, snapshot: dict, to_cloud: bool = True) -> list[dict]:
        local_list = self.local.save_snapshot(snapshot)
        if not to_cloud:
            self.pending_upload = True
            return local_list
        ok, remote_list = self._call_remote(self.remote.save_snapshot, snapshot)
        if not ok:
            self.pending_upload = True
            return local_list
        return remote_list or local_list

    # -- 显式同步 ----------------------------------------------
    def push_to_cloud(self) -> dict:
        """本地 → 云端：主数据以本地为准，快照按日期合并（本地优先）。"""
        data = self.local.load_data()
        self.remote.save_data(data)

        local_snaps = self.local.load_snapshots()
        merged: dict[str, dict] = {str(s.get("date", "")): s for s in self.remote.load_snapshots()}
        for item in local_snaps:
            merged[str(item.get("date", ""))] = item
        for day in sorted(merged):
            self.remote.save_snapshot(merged[day])

        self.mode = "cloud"
        self.pending_upload = False
        return {"trades": len(data.get("trades") or []), "snapshots": len(merged), "local_snapshots": len(local_snaps)}

    def pull_from_cloud(self) -> dict:
        """云端 → 本地：主数据与快照整体覆盖本地缓存。"""
        data = self.remote.load_data()
        if isinstance(data, dict) and data.get("assets"):
            self.local.save_data(data)
        snapshots = self.remote.load_snapshots()
        for item in snapshots:
            self.local.save_snapshot(item)
        self.mode = "cloud"
        self.pending_upload = False
        return {"has_data": bool(isinstance(data, dict) and data.get("assets")), "snapshots": len(snapshots)}
