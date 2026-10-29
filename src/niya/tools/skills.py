from typing import Any

from niya.skills.store import SkillError, SkillStore
from niya.tools.registry import Tool, ToolContext, ToolError

LIST_DESCRIPTION = "列出所有技能（名稱和一句說明），包括很久沒用、沒出現在 system prompt 清單裡的。要讀內容用 skill_view。"
VIEW_DESCRIPTION = "讀出一個技能的完整內容（SKILL.md）再照著做。file_path 可以讀它的附檔（references/、templates/、scripts/、assets/）。"
MANAGE_DESCRIPTION = (
    "管理你的技能（程序性記憶：重複會遇到的工作怎麼做）。"
    "什麼時候寫：完成一件複雜的工作（5 個以上工具呼叫）、踩過坑才成功、主人糾正過做法，而且之後可能再遇到。一次性的小事不用寫。"
    "什麼時候改：照技能做卻發現步驟過時、少了步驟或有沒寫到的坑，馬上用 patch 修。"
    "好的技能：什麼時候用、編號步驟和確切的指令、常見的坑、怎麼確認成功。"
    "action：create（content 是完整的 SKILL.md）、patch（old_string → new_string，小改用這個）、edit（整份重寫）、"
    "write_file（附檔）、archive（不再需要；不會真的刪掉）、restore（從封存拿回來）、accept（收下睡覺時整理出的草稿）。"
    "SKILL.md 格式：開頭是 ---\\nname: 技能名稱\\ndescription: 什麼時候用、做什麼\\n---，接著是 markdown 本文。"
)
LIST_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}}
VIEW_PARAMETERS = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "file_path": {"type": "string", "description": "附檔路徑，例如 references/notes.md"}},
    "required": ["name"],
}
MANAGE_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["create", "patch", "edit", "write_file", "archive", "restore", "accept"]},
        "name": {"type": "string", "description": "小寫英數字和 - _ .，例如 deploy-nginx"},
        "content": {"type": "string", "description": "create / edit：完整的 SKILL.md"},
        "old_string": {"type": "string", "description": "patch：要找的文字"},
        "new_string": {"type": "string", "description": "patch：換成的文字"},
        "replace_all": {"type": "boolean", "default": False},
        "file_path": {"type": "string", "description": "patch / write_file：附檔路徑"},
        "file_content": {"type": "string", "description": "write_file：附檔內容"},
    },
    "required": ["action", "name"],
}


def skill_tools(store: SkillStore) -> list[Tool]:
    async def skills_list(args: dict[str, Any], ctx: ToolContext) -> str:
        skills = store.all(("active", "stale", "proposed"))
        if not skills:
            return "目前還沒有技能。完成複雜、會重複遇到的工作後，可以用 skill_manage 寫下來。"
        labels = {"stale": "（很久沒用）", "proposed": "（草稿，等你收下）"}
        return "\n".join(f"- {s.name}{labels.get(s.status, '')}：{s.description}" for s in skills)

    async def skill_view(args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            return store.view(str(args.get("name") or ""), args.get("file_path") or None, session_id=ctx.session_id)
        except SkillError as exc:
            raise ToolError(str(exc)) from None

    async def skill_manage(args: dict[str, Any], ctx: ToolContext) -> str:
        action, name = args.get("action"), str(args.get("name") or "").strip()
        try:
            if action == "create":
                store.create(name, str(args.get("content") or ""), session_id=ctx.session_id)
                return f"已建立技能「{name}」，下次開新 session 會出現在技能清單裡。"
            if action == "edit":
                store.edit(name, str(args.get("content") or ""), session_id=ctx.session_id)
                return f"已更新技能「{name}」。"
            if action == "patch":
                old, new = args.get("old_string"), args.get("new_string")
                if not isinstance(old, str) or not isinstance(new, str):
                    raise SkillError("patch 需要 old_string 和 new_string。")
                return store.patch(name, old, new, file_path=args.get("file_path") or None, replace_all=bool(args.get("replace_all")), session_id=ctx.session_id)
            if action == "write_file":
                if not args.get("file_path"):
                    raise SkillError("write_file 需要 file_path。")
                return store.write_file(name, str(args["file_path"]), str(args.get("file_content") or ""), session_id=ctx.session_id)
            if action == "archive":
                store.archive(name, session_id=ctx.session_id)
                return f"已封存技能「{name}」，需要時可以 restore。"
            if action == "restore":
                store.restore(name, session_id=ctx.session_id)
                return f"已把技能「{name}」拿回來。"
            if action == "accept":
                store.accept(name, session_id=ctx.session_id)
                return f"已收下技能草稿「{name}」。"
        except SkillError as exc:
            raise ToolError(str(exc)) from None
        raise ToolError(f"未知的 action：{action}")

    return [
        Tool(name="skills_list", description=LIST_DESCRIPTION, parameters=LIST_PARAMETERS, handler=skills_list, toolset="skills", parallel_safe=True, timeout_s=15),
        Tool(name="skill_view", description=VIEW_DESCRIPTION, parameters=VIEW_PARAMETERS, handler=skill_view, toolset="skills", parallel_safe=True, timeout_s=15, max_result_chars=60_000),
        Tool(name="skill_manage", description=MANAGE_DESCRIPTION, parameters=MANAGE_PARAMETERS, handler=skill_manage, toolset="skills", timeout_s=30),
    ]
