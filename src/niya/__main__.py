import argparse
import asyncio
import sys
from pathlib import Path

from niya.config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="niya", description="niya：會睡覺、會遺忘、有時間感的長期運行 agent")
    parser.add_argument("--config", type=Path, help="設定檔路徑，預設 $NIYA_CONFIG 或 /etc/niya/niya.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("repl", help="在終端機跟 niya 對話（開發測試用）")
    sub.add_parser("doctor", help="檢查執行環境")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config, pop_secrets=True)
    except (ValueError, OSError) as exc:
        print(f"設定錯誤：{exc}", file=sys.stderr)
        return 2

    if args.command == "repl":
        from niya.repl import run_repl

        try:
            return asyncio.run(run_repl(settings))
        except ValueError as exc:
            print(f"無法啟動：{exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130
    if args.command == "doctor":
        from niya.doctor import run_doctor

        return asyncio.run(run_doctor(settings))
    parser.error(f"未知的指令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
