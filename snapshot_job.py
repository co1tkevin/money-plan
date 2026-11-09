"""独立每日快照任务：读主数据 → 拉基金净值 → 生成快照 → 写入存储。

本模块不依赖 Streamlit，可被 Vercel Cron / GitHub Actions 直接调用：
    from snapshot_job import generate_daily_snapshot, run_supabase
    generate_daily_snapshot(SupabaseAdapter(client, user_id))        # 单个用户
    run_supabase(user_ids)                                          # 批量用户

命令行用法：
    python snapshot_job.py --mode local --data-file data/portfolio_data.json
    python snapshot_job.py --mode cloud --user-id <uuid> [--user-id <uuid> ...]
    python snapshot_job.py --mode cloud --all-users      # 需 service_role key

云端模式所需环境变量：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY
命令行模式不需要 Streamlit，也不会触碰任何页面状态。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import portfolio_core as core
from storage_adapter import JsonFileAdapter, StorageAdapter, StorageError, SupabaseAdapter

BASE_DIR = Path(__file__).parent
DEFAULT_DATA_FILE = BASE_DIR / "data" / "portfolio_data.json"
DEFAULT_SNAPSHOT_FILE = BASE_DIR / "data" / "snapshots.json"


def akshare_ready() -> tuple[bool, str]:
    try:
        import akshare  # noqa: F401
    except Exception as exc:
        return False, str(exc)
    return True, ""


def generate_daily_snapshot(
    store: StorageAdapter,
    day: date | str | None = None,
    use_network: bool = True,
    fetcher=None,
) -> dict:
    """生成并保存一份快照；返回 {"snapshot", "errors", "stored_count"}。

    这是定时任务的唯一入口，前端页面不调用它，保证任务与 UI 解耦。
    """
    data = core.normalize(store.load_data())

    nav_map: dict = {}
    errors: list[str] = []
    if use_network:
        ready, reason = akshare_ready()
        if ready:
            nav_map, errors = core.collect_nav(data, fetcher)
        else:
            errors.append(f"未安装 akshare，跳过净值拉取：{reason}")

    snapshot = core.build_snapshot(data, nav_map, day)
    stored = store.save_snapshot(snapshot)
    return {"snapshot": snapshot, "errors": errors, "stored_count": len(stored)}


def supabase_service_client():
    """用 service_role key 创建客户端，供定时任务跨用户写入（绕过 RLS）。"""
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not (url and key):
        raise StorageError("云端模式需要环境变量 SUPABASE_URL 与 SUPABASE_SERVICE_ROLE_KEY")
    try:
        from supabase import create_client
    except Exception as exc:
        raise StorageError(f"未安装 supabase 包：{exc}") from exc
    return create_client(url, key)


def list_user_ids(client) -> list[str]:
    try:
        result = client.auth.admin.list_users()
    except Exception as exc:
        raise StorageError(f"读取用户列表失败：{exc}") from exc
    users = getattr(result, "users", result) or []
    ids: list[str] = []
    for user in users:
        uid = getattr(user, "id", None)
        if uid is None and isinstance(user, dict):
            uid = user.get("id")
        if uid:
            ids.append(str(uid))
    return ids


def run_supabase(user_ids: list[str], day=None, use_network: bool = True) -> list[dict]:
    """批量给多个用户生成快照；单个用户失败不影响其它用户。"""
    client = supabase_service_client()
    results: list[dict] = []
    for uid in user_ids:
        try:
            outcome = generate_daily_snapshot(SupabaseAdapter(client, uid), day, use_network)
            results.append({"user_id": uid, **outcome})
        except Exception as exc:
            results.append({"user_id": uid, "error": str(exc)})
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="永久组合 · 每日资产快照任务")
    parser.add_argument("--mode", choices=["local", "cloud"], default="local", help="local=本地 JSON；cloud=Supabase")
    parser.add_argument("--data-file", default=str(DEFAULT_DATA_FILE), help="主数据 JSON 路径（local 模式）")
    parser.add_argument("--snapshot-file", default=str(DEFAULT_SNAPSHOT_FILE), help="快照 JSON 路径（local 模式）")
    parser.add_argument("--date", default=date.today().isoformat(), help="快照日期，默认今天（YYYY-MM-DD）")
    parser.add_argument("--offline", action="store_true", help="不联网拉取净值，单日涨跌按 0 记录")
    parser.add_argument("--user-id", action="append", default=[], help="cloud 模式：指定用户 UUID，可重复")
    parser.add_argument("--all-users", action="store_true", help="cloud 模式：为该 Supabase 项目的全部用户生成")
    parser.add_argument("--quiet", action="store_true", help="只输出错误与单行结果")
    return parser


def _print_local(result: dict, snapshot_file: str, quiet: bool) -> None:
    snapshot = result["snapshot"]
    if quiet:
        print(f"{snapshot['date']} total={snapshot['total_asset']:.2f} daily={snapshot['daily_return']:+.2f}%")
    else:
        print(f"快照日期：{snapshot['date']}")
        print(f"总资产：{snapshot['total_asset']:,.2f} 元")
        print(f"组合当日收益率：{snapshot['daily_return']:+.2f}%")
        print(f"净值覆盖：{'是' if snapshot['nav_ok'] else '否（单日涨跌按 0 记录）'}")
        print(f"快照文件：{Path(snapshot_file).resolve()}（累计 {result['stored_count']} 条）")
        for category in core.CATEGORIES:
            info = snapshot["asset_detail"][category]
            if info["market_value"] or info["weight_pct"]:
                print(
                    f"  - {category}：{info['market_value']:,.2f} 元 / 仓位 {info['weight_pct']:.2f}% / "
                    f"单日 {info['change_pct']:+.2f}%"
                )
    if result["errors"]:
        print("[提示] 净值拉取失败（单日涨跌按 0 计）：" + "；".join(result["errors"]), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    use_network = not args.offline

    if args.mode == "local":
        adapter = JsonFileAdapter(args.data_file, args.snapshot_file)
        try:
            result = generate_daily_snapshot(adapter, args.date, use_network)
        except StorageError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"[错误] 文件读写失败：{exc}", file=sys.stderr)
            return 1
        _print_local(result, args.snapshot_file, args.quiet)
        return 0

    user_ids = list(args.user_id)
    if args.all_users:
        try:
            user_ids = list_user_ids(supabase_service_client())
        except StorageError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 1
    if not user_ids:
        print("[错误] cloud 模式需要 --user-id 或 --all-users", file=sys.stderr)
        return 1

    try:
        results = run_supabase(user_ids, args.date, use_network)
    except StorageError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    failed = 0
    for item in results:
        if "error" in item:
            failed += 1
            print(f"[失败] {item['user_id']}：{item['error']}", file=sys.stderr)
        elif not args.quiet:
            snap = item["snapshot"]
            print(f"[完成] {item['user_id']} {snap['date']} 总资产 {snap['total_asset']:,.2f} 当日 {snap['daily_return']:+.2f}%")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
