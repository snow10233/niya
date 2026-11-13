# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 這是什麼

niya 是主人自己寫的長期運行 agent，用來取代原本跑在 Hermes Agent 上的「澪夏」。會這樣做，是因為主人不滿意現有 agent 的記憶：想要有時間感、會遺忘、會睡覺整理的記憶。

- 跑在主人的 VPS 上（root），透過 Discord 對話。模型用 Claude 訂閱或 OpenRouter：Claude 訂閱經 claude-agent-sdk 跑官方 Claude Code；embedding 一律走 OpenRouter。
- 每則訊息都帶時間戳。對話閒置 30 分鐘就切成一個片段（`memory/episodic.py`）。每天 03:00、13:00 睡覺，由 `sleep/orchestrator.py` 的 `consolidate()` 依序跑 NREM（片段 → 事件和記憶）、日／週／月摘要、淡忘、REM、夢，睡醒清空 context。記憶照遺忘曲線衰退。
- 工具層很多是從 Hermes fork（`~/repos/forks/hermes-agent`，MIT，基準 commit 222465d84）搬來或改寫的，檔頭都有註明。agent 迴圈、記憶和睡眠是自己寫的。
- 功能說明在 README.md（寫給主人看）；原始設計與 M1–M8 里程碑在 `/home/snow/.claude/plans/agent-github-sprightly-hummingbird.md`。

## 測試

`uv run python -m unittest discover -s tests`，只跑一個檔案就加 `-p "test_x.py"`。直接執行測試檔會找不到 `tests/helpers.py`。

## 做到哪（2026-11-14）

- **10/29**：M1–M8 全部完成（骨架、工具與安全防護、模型選擇、分段與 deep_recall、鬧鐘、記憶、睡眠、REM、skills、子 agent 與 MCP），另外做了 Discord bot、systemd 部署檔和 Hermes 搬家工具（`niya hermes import/replay/notes`）。
- **10/30**：補上 Hermes 有、niya 原本沒有的工具（看圖、discord、browser_exec、image_generate、TTS），加了技能和排程的搬家工具（`hermes skills/cron`、`run-job`），記憶機制也改版了（又提起、曝光、喚醒、recall_more）。Hermes 9/8–10/29 的對話已經用 DeepSeek 回放，寫進 `~/niya-old-datas/niya-home/niya.db`。
- **10/31**：睡眠整理改用 Opus medium；system prompt 加了做事規則；context 滿了改成由 niya 自己寫交接摘要來壓縮；context 上限改成用模型本身的窗口。
- **之後沒有新 commit**，都是在 scratchpad 做實驗和討論：
  - harness 測試：9 題裡，Jev 判澪夏贏 5 題、niya 贏 2 題。主因是 niya 的測試環境沒有 skills，查得也少。
  - 用 Opus medium 重睡 10/26 那天：切得比 DeepSeek 粗（34 vs 44 個事件），並找到兩個 bug。
    - REM 把只有「...」的佔位答案存成了洞察（`sleep/rem.py` 沒有擋）。
    - 回放時，夢會混進前一晚的記憶。原因是回放的時鐘在睡覺時不會往前走，記憶的 created_at 剛好等於上次醒來的時間。
  - 連結分析：continues 只能接到最近 7 天裡最新的 15 個事件；REM 用 0.6 一路串起來的群太大（中位數 35 條，最多 774 條）。

## 還沒做

- **上線前**：
  - 主人審完 `hermes-notes.md` 後跑 `niya hermes apply`。
  - niya-home 的 Claude 登入，要主人自己來。
  - 搬 skills、轉 cron、部署到 VPS。
  - 切換前重新匯出 Hermes 的 state.db，把新的對話補進來。
- **等主人決定**：
  - 上面兩個 bug 要不要修。
  - continues 的候選要不要擴大、REM 分群要怎麼改。
  - 睡眠要不要先試 Sonnet／Haiku。
  - 要不要加查證規則、搬 skills，補上研究類任務的落差。
  - 記憶寫入改版：區分更新和補充，由 Jev 判斷兩條記憶的關係。
  - 繼續用 niya，正式跑兩週驗證；還是回 Hermes，把記憶做成外掛。

## 規矩

- 每完成一段、測試都過了就 commit（訊息用繁體中文），不 push。
- 設計決策都是主人逐項拍板過的，要推翻之前先問。給選項時用「1A 2B」編號，附上推薦和代價。
- `~/niya-old-datas` 底下的 Hermes 資料，以及 `niya-home/niya.db`，都只讀不改；要實驗就先複製到 scratchpad 再做。
- Claude 訂閱只透過官方 Claude Code 使用，不取出 token。
- 密鑰只放在 env 檔裡，不印出、不 commit。
