import argparse
import asyncio
import sys
from pathlib import Path

from niya.config import load_settings


def _ask(question: str) -> str:
    return input(question).strip() if sys.stdin.isatty() else ""


def run_model_command(settings, words: list[str]) -> int:
    import httpx

    from niya.db import Database
    from niya.llm import list_models
    from niya.model_choice import (
        OPENROUTER,
        ModelChoice,
        clear_choice,
        configured_choice,
        current_choice,
        describe_options,
        format_models,
        openrouter_problem,
        parse_choice,
        save_choice,
        search_models,
    )

    db = Database(settings.db_path)
    try:
        arg = " ".join(words).strip()
        if not arg:
            current, source = current_choice(db, settings)
            print(f"目前的模型：{current}（{source}）\n{describe_options().replace('/model', 'niya model')}")
            arg = _ask("輸入要切換的模型（直接 Enter 不變）：")
            if not arg:
                return 0
        if arg == "reset":
            clear_choice(db)
            print(f"已改回設定檔的模型：{configured_choice(settings)}")
            return 0
        try:
            models = asyncio.run(list_models(base_url=settings.base_url)) if arg.startswith("search") or "/" in arg else None
        except httpx.HTTPError:
            models = None
        if arg.startswith("search"):
            keyword = arg.removeprefix("search").strip()
            if models is None or not keyword:
                print("用法：niya model search <關鍵字>（需要連得上 OpenRouter）", file=sys.stderr)
                return 2
            found = search_models(models, keyword)
            if not found:
                print(f"OpenRouter 上沒有符合「{keyword}」又支援工具呼叫的模型。")
                return 1
            print(format_models(found))
            picked = _ask("輸入編號切換（直接 Enter 不變）：")
            if not (picked.isdigit() and 1 <= int(picked) <= len(found)):
                return 0
            new = ModelChoice(OPENROUTER, found[int(picked) - 1].id)
        else:
            try:
                new = parse_choice(arg)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 2
            if new.provider == OPENROUTER and models is not None and (problem := openrouter_problem(new.model, models)):
                print(problem.replace("/model", "niya model"), file=sys.stderr)
                return 2
        save_choice(db, new)
        print(f"已切換到 {new}。正在跑的 niya 要重新啟動才會套用；REPL 裡可以直接用 /model。")
        return 0
    finally:
        db.close()


def run_effort_command(settings, level: str | None) -> int:
    from niya.db import Database
    from niya.model_choice import EFFORT_HELP, EFFORTS, clear_effort, current_choice, current_effort, save_effort

    db = Database(settings.db_path)
    try:
        provider = current_choice(db, settings)[0].provider
        levels = EFFORTS[provider]
        if level is None:
            effort, source = current_effort(db, settings)
            print(f"目前的思考強度：{effort or '預設'}（{source}）\n{EFFORT_HELP}\n{provider} 可用：{'、'.join(levels)}；reset 改回設定檔")
            return 0
        if level == "reset":
            clear_effort(db)
            print(f"已改回設定檔：{settings.effort or '預設'}")
            return 0
        if level not in levels:
            print(f"{provider} 沒有「{level}」這個強度，可用：{'、'.join(levels)}", file=sys.stderr)
            return 2
        save_effort(db, level)
        print(f"思考強度改成 {level}。正在跑的 niya 要重新啟動才會套用；REPL 裡可以直接用 /effort。")
        return 0
    finally:
        db.close()


def run_memory_command(settings, args) -> int:
    from niya.clock import RealClock
    from niya.db import Database
    from niya.memory.curve import days_until
    from niya.memory.engine import KIND_LABELS, MemoryEngine
    from niya.memory.timefmt import exact_label

    db = Database(settings.db_path)
    engine = MemoryEngine(db, RealClock(), settings.tz)
    try:
        if args.memory_command == "list":
            where = "" if args.all else "WHERE state IN ('labile', 'active')"
            rows = db.query(f"SELECT id FROM memories {where} ORDER BY id")
            memories = engine.load(r["id"] for r in rows)
            if not memories:
                print("還沒有記憶。")
                return 0
            print(f"{'id':>5} {'狀態':<9}{'類型':<4}{'重要':>3} {'S(天)':>7} {'R':>5}  內容")
            for m in memories.values():
                print(f"{m.id:>5} {m.state:<10}{KIND_LABELS[m.kind]:<4}{m.importance:>4} {m.stability:>7.2f} {engine.r(m):>5.2f}  {m.content[:60]}")
            return 0
        memory = engine.get(args.id)
        if memory is None:
            print(f"找不到 m{args.id}", file=sys.stderr)
            return 1
        r = engine.r(memory)
        print(f"m{memory.id}（{KIND_LABELS[memory.kind]}，{memory.state}，重要度 {memory.importance}，來源 {memory.trust}/{memory.origin}）")
        print(memory.content)
        print(f"發生時間：{exact_label(memory.occurred_start, memory.occurred_end, settings.tz)}（精度 {memory.time_precision}）")
        print(f"S = {memory.stability:.2f} 天，R = {r:.3f}，上次複習 {exact_label(memory.last_review_at, None, settings.tz)}")
        print(f"再過 {max(days_until(0.30, memory.stability) - (engine.clock.now() - memory.last_review_at) / 86400, 0):.1f} 天會降到封存門檻")
        events = db.query("SELECT channel, COUNT(*) AS n FROM retrieval_events WHERE memory_id = ? GROUP BY channel", (memory.id,))
        if events:
            print("浮現次數：" + "、".join(f"{e['channel']} {e['n']}" for e in events))
        print(engine.related(f"m{memory.id}", session_id=None, turn_id=None).split("\n", 1)[1] if db.one("SELECT 1 FROM memory_links WHERE src_id = ? OR dst_id = ?", (memory.id, memory.id)) else "（沒有相關連結）")
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="niya", description="niya：會睡覺、會遺忘、有時間感的長期運行 agent")
    parser.add_argument("--config", type=Path, help="設定檔路徑，預設 $NIYA_CONFIG 或 /etc/niya/niya.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    repl = sub.add_parser("repl", help="在終端機跟 niya 對話（開發測試用）")
    repl.add_argument("--fake-clock", nargs="?", const="", metavar="'YYYY-MM-DD HH:MM'", help="用可以快轉的時鐘（/advance）測試睡眠和鬧鐘")
    sub.add_parser("doctor", help="檢查執行環境")
    model = sub.add_parser("model", help="查看或切換模型")
    model.add_argument("choice", nargs="*", help="例如 claude:sonnet、openrouter:<模型 ID>、search <關鍵字>、reset")
    effort = sub.add_parser("effort", help="查看或調整思考強度")
    effort.add_argument("level", nargs="?", help="low / medium / high / xhigh / max（OpenRouter 另有 none、minimal）；reset 改回設定檔")
    login = sub.add_parser("login", help="用 Claude 訂閱登入（透過官方 Claude Code 的登入流程）")
    login.add_argument("--token", action="store_true", help="改產生長效 token，寫進環境變數 CLAUDE_CODE_OAUTH_TOKEN")
    sub.add_parser("logout", help="登出 Claude")
    memory = sub.add_parser("memory", help="查看記憶")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    listing = memory_sub.add_parser("list", help="列出記憶與目前的 R")
    listing.add_argument("--all", action="store_true", help="連封存和被取代的也列出")
    show = memory_sub.add_parser("show", help="看一條記憶的細節")
    show.add_argument("id", type=int)
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config, pop_secrets=True)
    except (ValueError, OSError) as exc:
        print(f"設定錯誤：{exc}", file=sys.stderr)
        return 2

    if args.command == "repl":
        from niya.repl import run_repl

        try:
            return asyncio.run(run_repl(settings, fake_clock=args.fake_clock))
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
    if args.command == "effort":
        return run_effort_command(settings, args.level)
    if args.command == "memory":
        return run_memory_command(settings, args)
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
