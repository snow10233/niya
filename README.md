# niya

會睡覺、會遺忘、有時間感的長期運行 agent。跑在 VPS 上，透過 Discord 跟主人對話。

- 每則訊息都帶時間戳，niya 知道每件事是什麼時候發生的。
- 記憶照遺忘曲線衰退，真的用上才會變牢。
- 一天睡兩次，把醒著時的經歷切成事件、整理成記憶，再清空 context。

目前完成：

- **M1 骨架**：設定、資料庫、OpenRouter client、工具註冊、帶時間戳的訊息呈現、agent loop、REPL 和環境檢查。
- **M2 工具與安全防護**：terminal（含背景程序）、檔案工具、網路搜尋與擷取；黑名單、受保護路徑、密鑰遮蔽、偽造標記中和、SSRF 防護，以及「可疑外傳才問」。
- **模型選擇**：用 Claude 訂閱（透過官方 Claude Code）或 OpenRouter，可隨時用 `niya model` 或 `/model` 切換。
- **M3 情節紀錄**（Discord 和部署之外的部分）：
  - **對話分段**：閒置 30 分鐘或 session 結束就切一段；鬧鐘和背景程序觸發的回合各自一段。之後睡眠時會以段為單位整理。
  - **全文搜尋**：`deep_recall` 用關鍵字搜尋以前的對話，中文和英文都能找；`recall_related(msg#id)` 看前後文。
  - **持久化 inbox**：主人的訊息、背景程序結束的通知都先進資料庫，重啟不會遺失，一次處理一回合。
  - **context 用量門檻**：回合中超過 90% 就停下工具、交代進度；回合結束時超過 75% 就開新 session。睡眠做好以後，這裡會改成提早睡。

## 模型與登入

預設是 `claude:opus`：透過 `claude-agent-sdk` 內附的官方 Claude Code 執行，用量算在 Claude 訂閱裡。

```sh
uv run --env-file .env niya login        # 用 Claude 帳號登入（Claude Code 自己的登入流程）
uv run --env-file .env niya model        # 查看、切換模型
```

- **登入**：
  - 憑證由 Claude Code 自己存在 `$NIYA_HOME/claude`，跟你自己的 `~/.claude` 分開，niya 不會讀它。
  - VPS 沒有瀏覽器時，`niya login` 會印出網址，在自己的電腦打開，再把代碼貼回來。
  - 也可以用 `niya login --token` 產生長效 token，存成 `CLAUDE_CODE_OAUTH_TOKEN`。
- **切換模型**：
  - `niya model sonnet`，或在 REPL 裡打 `/model sonnet`、`/model openrouter:<模型 ID>`、`/model reset`（改回設定檔）。
  - 找 OpenRouter 的模型：`/model search <關鍵字>`（例如 `/model search gemini flash`）只列出支援工具呼叫的模型和價格，再用 `/model <編號>` 切換。直接打的 ID 也會先對照 OpenRouter 的清單，打錯會提醒。
  - 同一個 provider 換模型會直接套用；換 provider 會開新 session，舊的紀錄留在資料庫。
- **思考強度**：`/effort high`（或 `niya effort high`），`/effort reset` 改回設定檔。
  - Claude：`low`、`medium`、`high`、`xhigh`、`max`。Claude 不能完全不思考，設 `none`／`minimal` 會當成 `low`。
  - OpenRouter：另外還有 `none`（不思考）和 `minimal`，送出時是 `reasoning.effort`。
  - Claude 模式改強度時會重新連上 Claude Code，對話照樣接續。
- **為什麼不像 Hermes 那樣直接拿 token 呼叫 API**：Anthropic 規定訂閱登入只能用在官方的 Claude Code 和 Anthropic 自己的 App，第三方程式不能取出或代管登入憑證，從 2026 年 1 月起也會主動封鎖。所以 niya 改成在背後執行官方 Claude Code。
- **額度**：訂閱有 5 小時和每週的用量上限，用完時 niya 會告訴你幾點重置。設了 `ANTHROPIC_API_KEY` 就改走 API 計費。
- **Claude 模式跟 OpenRouter 模式的差別**：
  - 工具迴圈和 API context 由 Claude Code 管。niya 的工具以 MCP 的形式提供，模型看到的名稱是 `mcp__niya__terminal` 這類。
  - 所有工具一樣經過黑名單、外傳檢查和密鑰遮蔽。每則訊息也一樣存進 niya 的資料庫。
  - Claude Code 的自動壓縮已經關掉，context 滿了照計畫提早睡覺。
- 記憶用的 embedding（M5）還是走 OpenRouter。

## 安全機制

niya 在 VPS 上以 root 執行，終端機只擋黑名單，不需要主人逐一核准。程式層的防線：

- **黑名單**：`rm -rf /` 這類無法復原的指令、停掉或刪除 niya 自己、停 sshd、清空防火牆、刪 `authorized_keys`、鎖 root 帳號。
- **受保護路徑**：檔案工具不能改 niya 的資料與設定；`authorized_keys` 只能新增 key；`sshd_config` 寫入後跑 `sshd -t`，失敗就還原。
- **密鑰遮蔽**：所有工具輸出都會把 niya 的密鑰換成 `[REDACTED]`，子程序的環境變數裡也沒有密鑰。
- **外部內容**：網頁、搜尋結果、curl 抓下來的內容會包進 `<untrusted>`，內容裡偽造的 `[時間] user:` 行和標籤會被改寫。
- **可疑外傳**：只有「讀過外部內容」＋「目的地是陌生主機」＋「傳的是本機檔案或指令輸出」三者同時成立才會暫停。終端機會顯示詢問，回「好」就放行。已知主機用 `/egress list|allow|remove` 管理，主人訊息裡提過的網域會自動算已知。

## 本機執行

```sh
cp .env.example .env   # 用 Claude 訂閱的話不用填；用 OpenRouter 就填 OPENROUTER_API_KEY
uv sync
uv run --env-file .env niya login
uv run --env-file .env niya doctor
uv run --env-file .env niya repl
```

- REPL 裡輸入 `/new` 開新 session，`/model` 切換模型，`/effort` 調整思考強度，`/exit` 離開。
- 資料存在 `NIYA_HOME`（`.env.example` 預設是專案目錄下的 `.niya/`）的 `niya.db`。VPS 上預設是 `/var/lib/niya`。
- 設定檔可以用 `--config` 或 `NIYA_CONFIG` 指定，格式見 `niya.example.toml`。人格設定見 `persona.example.md`。
- 密鑰只從環境變數讀，讀完就從環境中移除，不要寫進設定檔或提交到 Git。

## 送給模型的格式

```
[2026-10-28 (三) 21:30] user: …
[2026-10-28 (三) 21:31] niya: …
```

資料庫只存原文和時間，前綴每次由固定規則產生，所以同一段歷史每次送出的文字完全一樣，前綴快取可以一路命中。

## 測試

```sh
uv run python -m unittest discover -s tests -v
```

測試使用模擬的 OpenRouter 和假時鐘，不需要密鑰，也不會連網。

## 授權

`LICENSES/` 底下放的是借用程式碼的原始授權。從 NousResearch/hermes-agent（MIT）改寫的檔案，開頭都會註明來源。
