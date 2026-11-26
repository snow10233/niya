# AGENTS.md

給在這個 repo 裡工作的 agent 看的說明。

## 這是什麼

niya 是主人自己寫的長期運行 agent，目標是取代跑在 Hermes Agent 上的「澪夏」。會這樣做，是因為主人不滿意現有 agent 的記憶：想要有時間感、會遺忘、會睡覺整理的記憶。

## 現在的狀態（2026-11-26）

從頭重做中。這條 `main` 是全新的起點，沒有接續舊的歷史，目前還沒有任何程式。

舊版在 `v0` 分支，只當參考，不在上面繼續開發：

- 約 22,900 行 Python，M1–M8 都做完、測試也過，但做得太快，主人看不懂。
- 看舊版的檔案用 `git show v0:路徑`；舊版做了什麼、做到哪，看 `git show v0:AGENTS.md`。
- 原始設計與 M1–M8 里程碑在 `/home/snow/.claude/plans/agent-github-sprightly-hummingbird.md`。

## 重做的規矩

- 主人要看得懂每一步，而且能用自己的話解釋它在做什麼。慢沒關係。
- 每一步先講清楚要做什麼、為什麼，再動手。
- 不要整包從 `v0` 複製過來。
- 範圍是全部：每一行主人都要懂，工具層也一樣（2026-11-26 拍板）。
- 從最小的 agent 迴圈開始，一次往上加一小塊，每一塊做完都要能實際跑給主人看。
- 先用 API key 做自己的 agent 迴圈，之後才接 Claude Code（訂閱）。Claude Code 會自己跑迴圈，別家不一定會，所以自己的迴圈要留著給別家用（2026-11-26 拍板）。

## 怎麼跑

`uv run niya.py`。程式開頭的 `load_dotenv()` 會把 `.env` 裡的密鑰讀進環境變數。

## 其他規矩

- 每完成一段就 commit（訊息用繁體中文），不 push。
- 設計決策都是主人逐項拍板過的，要推翻之前先問。給選項時用「1A 2B」編號，附上推薦和代價。
- `~/niya-old-datas` 底下的 Hermes 資料，以及 `niya-home/niya.db`，都只讀不改；要實驗就先複製到 scratchpad 再做。
- Claude 訂閱只透過官方 Claude Code 使用，不取出 token。
- 密鑰只放在 env 檔裡，不印出、不 commit。
