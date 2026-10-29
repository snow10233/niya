import argparse
import asyncio
import sys
from pathlib import Path

from niya.config import load_settings


def run_model_command(settings, choice: str | None) -> int:
    from niya.db import Database
    from niya.model_choice import clear_choice, configured_choice, current_choice, describe_options, parse_choice, save_choice

    db = Database(settings.db_path)
    try:
        if choice is None:
            current, source = current_choice(db, settings)
            print(f"目前的模型：{current}（{source}）\n{describe_options()}")
            if not sys.stdin.isatty():
                return 0
            choice = input("輸入要切換的模型（直接 Enter 不變）：").strip()
            if not choice:
                return 0
        if choice == "reset":
            clear_choice(db)
            print(f"已改回設定檔的模型：{configured_choice(settings)}")
            return 0
        try:
            new = parse_choice(choice)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        save_choice(db, new)
        print(f"已切換到 {new}。正在跑的 niya 要重新啟動才會套用；REPL 裡可以直接用 /model。")
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="niya", description="niya：會睡覺、會遺忘、有時間感的長期運行 agent")
    parser.add_argument("--config", type=Path, help="設定檔路徑，預設 $NIYA_CONFIG 或 /etc/niya/niya.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("repl", help="在終端機跟 niya 對話（開發測試用）")
    sub.add_parser("doctor", help="檢查執行環境")
    model = sub.add_parser("model", help="查看或切換模型")
    model.add_argument("choice", nargs="?", help="例如 claude:opus、claude:sonnet、openrouter:<模型 ID>；reset 改回設定檔")
    login = sub.add_parser("login", help="用 Claude 訂閱登入（透過官方 Claude Code 的登入流程）")
    login.add_argument("--token", action="store_true", help="改產生長效 token，寫進環境變數 CLAUDE_CODE_OAUTH_TOKEN")
    sub.add_parser("logout", help="登出 Claude")
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
    if args.command == "model":
        return run_model_command(settings, args.choice)
    if args.command in ("login", "logout"):
        from niya.claude_cli import run_cli

        if args.command == "logout":
            return run_cli(settings, "auth", "logout")
        if args.token:
            print("登入後 Claude Code 會印出一組 token，把它存成環境變數 CLAUDE_CODE_OAUTH_TOKEN（VPS 上放在 /etc/niya/niya.env）。")
            return run_cli(settings, "setup-token")
        return run_cli(settings, "auth", "login", "--claudeai")
    parser.error(f"未知的指令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
