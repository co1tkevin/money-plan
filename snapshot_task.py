"""命令行每日快照任务（无 UI，供操作系统定时调用）

实际逻辑都在 snapshot_job.generate_daily_snapshot()，本文件只是 local 模式的命令行入口，
保证和网页端、Vercel Cron / GitHub Actions 共用同一套计算。

用法示例：
    python snapshot_task.py
    python snapshot_task.py --data-file data/portfolio_data.json --snapshot-file data/snapshots.json
    python snapshot_task.py --offline          # 不联网，单日涨跌按 0 记录
    python snapshot_task.py --date 2026-11-08  # 指定快照日期（默认今天）

Windows 计划任务（每天 20:30 执行，工作目录设为项目根目录）：
    schtasks /create /tn "PermanentPortfolioSnapshot" /sc daily /st 20:30 ^
      /tr "cmd /c cd /d \"D:\\AI\\money plan\" && python snapshot_task.py"

主数据来源：网页端【⑤ 数据与快照】→【导出全量业务数据（JSON）】，
把导出的文件保存为 data/portfolio_data.json（或用 --data-file 指定路径）即可。
写入云端请改用：python snapshot_job.py --mode cloud --user-id <uuid>
"""

from __future__ import annotations

import sys

from snapshot_job import main as _main


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    return _main(["--mode", "local", *args])


if __name__ == "__main__":
    raise SystemExit(main())
