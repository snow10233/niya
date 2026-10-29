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
- **M4 鬧鐘**：
  - niya 用 `alarm` 工具自己設提醒，支援一次性和 cron 重複。時間到了會叫醒 niya 跑一回合，去提醒你或去做某件事。
  - niya 沒在運作時錯過的鬧鐘：提醒類 6 小時內、任務類 24 小時內補響，超過就讓 niya 告訴你錯過了。
  - REPL 裡用 `/alarms` 查看，`/alarms cancel <id>` 取消。
- **M5 記憶與時間**：
  - **遺忘曲線**：每條記憶有自己的穩定度 S，R 隨時間下降。重要度只決定初始 S，之後只有真的用上才會變牢（睡眠時判斷，M6）。
  - **時間模糊**：資料庫存精確時間，但 niya 看到的時間標籤會隨 R 變粗：「昨天下午 2 點」→「昨天」→「10 月下旬」→「上個月」→「2025 年秋天」。
  - **記住**：`remember` 刻意記下偏好、事實、計畫。白天記的是「還不牢」的狀態，要等睡覺整理（M6）。
  - **每回合自動回想**：用 embedding、全文搜尋和時間線索（「昨天」「上週二」）找候選，記憶越淡就要越強的線索才想得起來。差一點想起來的會用「話到嘴邊」提示 niya 去用力想。
  - **用力回想**：`deep_recall`（包含已封存的記憶和以前的對話，附精確時間）、`recall_timeline`（翻日記）、`recall_related`（從一條記憶往下挖）。
  - **記憶區塊**：每次開新 session，system prompt 會附上「關於主人」「我知道的事」「近期計畫」（含鬧鐘）。
  - `niya memory list` / `niya memory show <id>` 看每條記憶的 S、R 和浮現紀錄。
  - embedding 用 `perplexity/pplx-embed-v1-4b`（1024 維）。挑選依據是一份 24 題的中文回想測驗：它的正解和無關記憶分得最開。
- **M6a 睡眠**：
  - **時間**：每天 03:00 夜間睡眠、13:00 午睡，整理完就醒，不睡滿。早上到午睡前都沒對話，就跳過午睡；niya 沒在運作而錯過太久的時段，不會事後補睡。
  - **NREM**：
    - 每個對話片段交給模型一次（不帶工具），切成事件、萃取事實、偏好和計畫，並判斷白天浮現的記憶哪些真的用上（用上的才強化）。
    - 新資訊推翻舊記憶時，舊的會標上有效期限。
    - 程式會驗證切分結果，不合法就退回「整段算一個事件」。
    - 每段在一個 transaction 裡寫完，中斷不會留下半套。
  - **白天記的記憶**：睡過一覺才轉成正式記憶，S 才拿到依重要度應得的值。
  - **夜間另外做的事**：日摘要（當天的事件都連到它）、資料庫備份（留 7 份）、夢境日誌（`/dream`）。
  - **修剪**：R 低於 0.30 的記憶封存，之後只有用力回想才找得到。
  - **起床**：整理完才清空 context、開新 session、重建記憶區塊，第一則訊息前會附一行「午睡醒來（整理了…）」。發現沒設鬧鐘的計畫，會提醒 niya 判斷要不要補。
  - **context 滿了**：說「有點累了，先睡一下」就提早去睡。午睡前提早睡算這天的午睡，午睡後提早睡算這晚的夜間睡眠，被頂替的時段不再睡。醒來時會帶上「睡前正在做」的進度。
  - **睡覺中**：訊息先排隊；`/wake` 或提醒類鬧鐘會叫醒 niya，但不清空 context，閒下來後（被 `/wake` 叫醒要 30 分鐘，被鬧鐘叫醒要 5 分鐘）再續睡。
  - **REPL 指令**：`/status`、`/sleep [nap|night]`、`/wake`、`/dream`。
  - **快轉時鐘**：`niya repl --fake-clock "2026-10-29 11:30"` 搭配 `/advance 90m`，不用真的等到 13:00 也能測試睡眠。
  - **睡眠用的模型**：預設和對話相同，可以用 `[sleep] model` 另外指定便宜的模型。
- **M6b REM 與長期記憶**：
  - **週、月摘要**：夜間睡眠會檢查最近一個完整的週和月有沒有摘要，沒有就補做。日摘要屬於週摘要，週摘要屬於月摘要。越上層的摘要 S 越大（週 ×3、月 ×6），越不容易忘。
  - **擴散強化**：一個事件被真的用上時，它所屬的日、週、月摘要也會小幅變牢。
  - **REM**：
    - 新記憶會和相似的舊記憶連起來。
    - 相關的記憶聚成一群（至少三條）時，請模型判斷有沒有值得記下的規律，例如「網站 502 通常是 gunicorn 沒啟動」。有的話就記成「想法」。
    - 想法只是 niya 自己的推測，初始 S 只有一半，要之後真的用上才會留下來。
  - **預算**：每次睡眠花在摘要和 REM 的上限是 `[sleep] budget_usd`（預設 1 美元）。對話整理（NREM）一定會做完，不受這個限制。
- **M7 Skills（程序性記憶）**：
  - **存放**：技能是 `$NIYA_HOME/skills/<name>/SKILL.md`（YAML frontmatter 加上 markdown 步驟，格式和 Hermes／agentskills.io 相同），可以附 `references/`、`templates/`、`scripts/`、`assets/`；SKILL.md 裡的 `${SKILL_DIR}` 讀出來時會換成技能資料夾，附的腳本可以寫成 `${SKILL_DIR}/scripts/x.py`。
  - **工具**：`skills_list`、`skill_view`、`skill_manage`（create、patch、edit、write_file、archive、restore、accept）。刪除一律改成封存，不會真的刪掉。
  - **何時寫、何時用**：做完複雜、會再遇到的工作，niya 會自己寫成技能；之後遇到同樣的事，先讀技能再照著做。每次開新 session，system prompt 會列出可用的技能。
  - **安全**：
    - 技能目錄在受保護的 niya 資料夾裡，只有 `skill_manage` 能寫入，而且寫入前會用最嚴格的規則掃描注入內容。
    - 讀取時也會掃描，被竄改的技能會顯示 `[BLOCKED]`，不會被當成指令。
  - **生命週期**：30 天沒用就從 system prompt 的清單拿掉（stale），90 天沒用就封存。被用到就恢復。
  - **REM 提案**：同一種工作做了三次以上，睡覺時會整理成技能草稿，附上當時實際下過的指令。醒來後由 niya 決定要不要收下。
- **M8 子 agent 與 MCP**：
  - **子 agent**：
    - **用法**：`delegate_task` 把可以獨立完成的工作交給另一個 niya，它用自己的 context 做完，只交回報告。適合要讀很多網頁或檔案、可以同時進行的調查。
    - **限制**：只能派一層，同時最多 3 個。沒有人格設定、記憶、鬧鐘和技能管理，也不能再派子 agent。
    - **安全**：
      - 子 agent 的工具在父 session 名下執行，所以「讀過外部內容」的污染狀態雙向共享。
      - 子 agent 被攔下的外傳，你回「好」也能放行。
      - 讀過外部內容的子 agent 交回的報告，會包上 `<untrusted>`。
    - **紀錄**：子 agent 的對話會存檔，但不進睡眠整理（成果已經在父 agent 的工具結果裡）。
  - **MCP**：
    - **設定**：在設定檔的 `[mcp.servers.<名稱>]` 寫 `command`／`args`／`env`（本機 stdio）或 `url`（streamable HTTP），啟動時連線，工具名稱是 `mcp__<名稱>__<工具>`。
    - **安全**：
      - MCP 回傳的內容一律當作外部內容：包上 `<untrusted>`、標記污染、遮蔽密鑰。
      - `env` 裡用 `${VAR}` 引用的環境變數，會和 niya 自己的密鑰一樣處理：讀完就從環境變數移除、在輸出中遮蔽，不會出現在 terminal 裡。
    - **連不上時**：某個 server 連不上，只會略過那一個，不影響啟動。
- **terminal 的環境**：niya 的 terminal 不會帶到它自己的 virtualenv，所以 `python3`、`pip` 用的是系統的。
- **瀏覽器**：`browser_exec` 把 Python 程式交給 [Browser Use CLI](https://browser-use.com) 執行，照 Hermes 的做法移植，只用本機的瀏覽器。
  - **啟用**：VPS 上裝了 `browser-use`（`uv tool install browser-use`，再跑 `browser-use --doctor` 檢查）就會自動出現；也可以在 `[browser] command` 指定指令。
  - **截圖**：程式裡呼叫 `capture_screenshot()`，截圖會複製到 `media/screenshots/`，直接附給模型看。
  - **安全**：程式裡寫到的網址，開之前會先檢查，內網、本機位址和帶密鑰的網址都會擋下來。瀏覽器拿不到 niya 的密鑰，讀到的網頁一律當作外部內容。
- **生圖**：`image_generate` 畫圖，或照參考圖修改。畫好的圖存在 `media/generated/`，會附給 niya 看；滿意了，再用 `discord` 工具附檔給你。
  - **OpenRouter**（預設）：用 `[image] model` 指定的模型（預設 `openai/gpt-5.4-image-2`），費用記在 `llm_calls`。
  - **Codex 方案**（`[image] provider = "codex"`）：用 ChatGPT 方案的額度，透過官方 Codex CLI 的 `$imagegen` 畫圖，再從 `~/.codex/generated_images/` 取出成品。這和用 Claude 訂閱一樣，只透過官方 CLI，不取出登入 token 直接打 API。VPS 上要先 `codex login`。
- **語音**：`text_to_speech` 用 MiniMax（`t2a_v2`）把文字念成 mp3，存在 `media/tts/`，再用 `discord` 工具附檔給你。設定了 `MINIMAX_API_KEY` 才會啟用；模型、音色、區域在 `[tts]`。
- **圖片**：直接交給模型看（原生多模態），所以要選吃圖片的模型；不吃圖片的模型不另外處理。
  - **來源**：你在 Discord 傳的圖片、`read_file` 讀到的圖片、瀏覽器截圖、niya 自己生的圖。
  - **存放**：圖片和附檔存在 `$NIYA_HOME/media/`，訊息只記路徑，每次送出前才讀檔、縮到長邊 1568 px 以內。
  - **其他附檔**：你傳的非圖片檔也會下載到 `media/inbox/`，niya 拿到的是本機路徑（Discord 的附件連結會過期）。
- **Discord**：
  - **收訊息**：`niya run` 以 Discord bot 常駐，只回應主人。私訊之外，也可以設定幾個頻道。其他帳號、bot 一律忽略。
  - **送訊息**：先遮蔽密鑰，再依 code block 切成 2000 字以內，超過 6000 字改傳 `.md` 附檔。niya 處理中會顯示「正在輸入」。
  - **工具摘要**：niya 每呼叫一次工具，就顯示一行「emoji 工具名稱」，下面用 code block 放指令、路徑或查詢，最多 40 字，規則照 Hermes。
    - 連續呼叫同一個工具時共用一行標題；同樣的呼叫重複時記成 `(×2)`。
    - 這些摘要都在同一則訊息裡，有新的呼叫就編輯它。超過長度就接著開新的一則；niya 說完話之後的呼叫，也會開新的一則，放在那句話下面。
    - 子 agent 的呼叫，標題前面加 `↳`。
    - 不想看可以在 `niya.toml` 設 `[agent] tool_progress = false`。
  - **`discord` 工具**：有設定 `DISCORD_TOKEN` 就會啟用。它走 bot 的 REST API，所以 REPL 裡也能用。
    - **能做的事**：列出頻道和討論串、讀頻道歷史、發訊息（可以附檔，例如生的圖或語音）、刪自己發的訊息、開討論串、改名稱。
    - **發送的範圍**：只發到設定的伺服器（沒設定 `guild_id` 就是 bot 唯一所在的那個）和主人的私訊。不指定頻道時，發到主人最後一次說話的地方。
    - **讀到的訊息**：一律當作外部內容，因為頻道裡誰都能發言，包括新聞 feed。
    - **附檔**：`media/` 以外的本機檔案，只要 session 讀過外部內容，就要你同意才會送出。niya 自己的資料和設定一律不能附。
  - **睡覺中**：自動回「睡覺中」（30 分鐘最多一次），狀態顯示 💤。
  - **指令**：`/status`、`/wake`、`/stop`、`/sleep`、`/dream`、`/alarms`、`/egress`、`/model`、`/effort`、`/new`，只有主人能用；另外也可以打 `!wake`。

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
- **睡眠用的模型**：`[sleep] model` 和 `[sleep] effort` 可以另外指定。
  - 除了 claude、openrouter，也可以用 DeepSeek 官方 API：`model = "deepseek:deepseek-flash"`，金鑰放 `DEEPSEEK_API_KEY`。
  - DeepSeek 只能用在睡眠整理，不能拿來對話。
  - 它的思考強度只有 `low`、`high`、`max`：`medium` 算 `high`，`xhigh` 算 `max`。
  - DeepSeek 不回報費用，所以 `budget_usd` 對它沒有作用。

## 從 Hermes 搬家

把 Hermes 用過的 agent 帶過來：對話照時間重新「活」一遍，整理成 niya 的記憶；筆記拆開來分層；技能換成 niya 的工具再搬；人格從 SOUL.md 來。
資料來源是 Hermes 資料夾（`~/.hermes`）的副本，裡面要有 `state.db`、`memories/` 和 `skills/`。niya 只讀，不會改它。

```sh
uv run --env-file .env niya hermes import ~/hermes-copy      # 1. 匯入對話（只寫原始紀錄）
uv run --env-file .env niya hermes replay                    # 2. 回放：每回合自動回想，每天 03:00、13:00 睡覺整理
uv run --env-file .env niya hermes notes ~/hermes-copy       # 3. 拆筆記，產生審核清單和人格草稿
uv run --env-file .env niya hermes skills ~/hermes-copy      # 4. 讀技能：事實接在審核清單後面，技能列成搬家清單
uv run --env-file .env niya hermes apply $NIYA_HOME/hermes-notes.md   # 5. 看過、改過清單之後一起匯入
```

- **匯入**：
  - 每個 Hermes session 會在 03:00、13:00 切開。niya 在那兩個時間睡覺、清空 context，所以一次醒著就是一個 session。
  - Hermes 自己塞進對話的東西會拿掉：gateway 標頭、時間戳前綴、compaction 摘要、心跳提示。
  - 以 `[SILENT]` 結束的心跳整個不算。被主人撤回（rewind）的回合也不算。
  - cron 和子 agent 的 session 不匯入，它們的結果本來就會回到主對話。
  - 背景程序、委派完成這類通知只留前 300 字。
  - 所有內容都先遮蔽密鑰再存。
  - 可以重複匯入：同一份資料匯入第二次什麼都不會多；換成比較新的副本，只會補上新的回合。
- **回放**：
  - 用快轉的時鐘照時間走過整段歷史。每個 session 開頭先建記憶區塊，主人的每則訊息都跑一次自動回想，到了 03:00、13:00 就跑真正的睡眠整理。
  - 遺忘曲線、時間標籤和日、週、月摘要，都會像 niya 當時就在場一樣。
  - 同一次睡眠裡的片段預設同時整理 4 個（`--parallel`），重複的記憶在寫入時檢查，不會記兩次。
  - 中斷了再跑一次，會從最後一次睡完的地方接著做。`--until 'YYYY-MM-DD HH:MM'` 可以只跑一段，先看看結果。
  - 回放不會超過現在的時間。睡眠時段如果在未來，真的 niya 就會跳過那一晚。還沒睡到的片段，交給 niya 上線後的第一次睡眠整理。
- **筆記**：
  - USER.md、MEMORY.md 的每條筆記會拆成「一件事一條」，再分成四類：
    - `[persona]`：寫進人格設定，永遠記得。
    - `[owner]`：關於主人的記憶。
    - `[self]`：niya 自己知道的事。
    - `[drop]`：只對 Hermes 有用的，不帶過來。
  - 審核清單可以直接改標籤、內容、重要度，或刪掉整行；⚠️ 是敏感內容。
  - **apply**：
    - 回放時已經記得的筆記，算「又遇到一次」，會變牢。
    - 推翻舊記憶的筆記，會取代舊的那條。
    - 其他的新增成記憶。因為 Hermes 每個 session 都會看到這些筆記，新增時的穩定度是一般新記憶的 3 倍。
  - **人格**：
    - 從 Hermes 最後一份 system prompt 取出 SOUL.md，拿掉「運行在 Hermes 上」的句子，存成 `persona.hermes.md`。
    - apply 時把 `[persona]` 的筆記接在後面，寫到設定檔的 `persona` 路徑。
- **技能**：
  - Hermes 有很多事實只寫在技能裡（路徑、帳號、主人的習慣），回放看不到：睡眠整理只看得到工具名稱和參數。
  - Hermes 自己寫的、和內建但被改過的技能，模型會逐一讀完，做三件事：
    - 把事實照筆記的格式抽出來，接在 `hermes-notes.md` 最後面（每個技能一節），跟筆記一起審、一起 apply。
    - 判斷要不要搬：講 Hermes 本身的、niya 已經內建的（記憶整理、鬧鐘）建議 `[drop]`，其他 `[move]`。
    - 列出要改的地方，把 Hermes 的工具換成 niya 的（例如 `cronjob_manage` → `alarm`、`send_message` → `discord`、`vision_analyze` → `read_file` 看圖）。
  - 每個技能都會複製到 `$NIYA_HOME/hermes-skills/<名稱>/`，已經套用修改，技能自己資料夾的路徑換成 `${SKILL_DIR}`。檔案位置不動，附的腳本才找得到自己的檔案。
  - 搬家清單是 `hermes-skills.md`，改標籤決定搬不搬；要調整內容就直接改 `hermes-skills/` 裡的檔案。Hermes 內建、沒改過的技能只列出來，預設不搬。
  - 模型回的不是 JSON 會再試兩次。結果存在 `hermes-skills.json`，`--only 名稱,名稱` 可以只重讀幾個，其他沿用上次的結果。
  - apply 時把 `[move]` 的技能搬進 `$NIYA_HOME/skills/`，每個檔案都用最嚴格的規則掃描過；niya 已經有的同名技能不動，所以可以重跑。

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

- niya 呼叫工具時，REPL 會先印一行摘要（例如 `💻 terminal: uptime`），再顯示回覆。
- REPL 裡輸入 `/status` 看狀態、`/sleep` 去睡、`/wake` 叫醒、`/dream` 看夢境日誌、`/new` 開新 session、`/model` 切換模型、`/effort` 調整思考強度、`/alarms` 看鬧鐘、`/exit` 離開。
- 資料存在 `NIYA_HOME`（`.env.example` 預設是專案目錄下的 `.niya/`）的 `niya.db`。VPS 上預設是 `/var/lib/niya`。
- 設定檔可以用 `--config` 或 `NIYA_CONFIG` 指定，格式見 `niya.example.toml`。人格設定見 `persona.example.md`。
- 密鑰只從環境變數讀，讀完就從環境中移除，不要寫進設定檔或提交到 Git。

## Discord 與部署到 VPS

### 1. 建立 Discord bot

1. 到 [Discord Developer Portal](https://discord.com/developers/applications) 建立 Application，在 **Bot** 頁按 Reset Token 取得 token，並打開 **Message Content Intent**。
2. 在 **OAuth2 → URL Generator** 勾 `bot` 和 `applications.commands`，權限勾 Send Messages、Read Message History、Attach Files，用產生的網址把 bot 邀進你的伺服器。bot 要和你在同一個伺服器裡才能私訊你。
3. 在 Discord 打開開發者模式，對自己的頭像按右鍵，選「複製使用者 ID」。這就是 `owner_id`。

### 2. 安裝（在 VPS 上以 root 執行）

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh          # 安裝 uv
git clone <這個 repo> /opt/niya && cd /opt/niya
uv sync --frozen
mkdir -p /etc/niya && chmod 700 /etc/niya
cp niya.example.toml /etc/niya/niya.toml                  # 填 [discord] owner_id、[paths] 等
cp persona.example.md /etc/niya/persona.md                # 寫下你希望 niya 知道的關於你的事
install -m 600 .env.example /etc/niya/niya.env           # 複製成只有 root 能讀的密鑰檔，再填入
nano /etc/niya/niya.env
```

`/etc/niya/niya.env` 只放密鑰：

- **一定要填**：`DISCORD_TOKEN`，以及 `OPENROUTER_API_KEY`（記憶的 embedding 走 OpenRouter，用 Claude 訂閱對話也需要）。
- **主人 ID**：`NIYA_DISCORD_OWNER_ID`，或寫在 `niya.toml` 的 `[discord] owner_id`，擇一即可。
- **用 Claude 訂閱**：`CLAUDE_CODE_OAUTH_TOKEN`、`ANTHROPIC_API_KEY` 留空，下一步用 `niya login` 登入。
- **網路搜尋**：`TAVILY_API_KEY` 或 `BRAVE_SEARCH_API_KEY`，可以之後再補。
- **`NIYA_HOME=.niya` 那行要刪掉**：那是本機開發用的，留著資料會跑到 `/opt/niya/.niya`，不會放在 `/var/lib/niya`。
- 留空的項目等於沒設定，不用刪。

### 3. 登入 Claude、檢查、啟動

```sh
cd /opt/niya
set -a; . /etc/niya/niya.env; set +a
export NIYA_CONFIG=/etc/niya/niya.toml
.venv/bin/niya login        # 會印出網址，在自己的電腦打開並登入，再把代碼貼回來
.venv/bin/niya doctor
cp deploy/niya.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now niya
journalctl -u niya -f       # 看日誌
```

- 資料放在 `/var/lib/niya`：資料庫、技能、備份，以及 Claude Code 的登入資料。
- niya 改到自己的程式時，用 `git -C /opt/niya status` 和 `git -C /opt/niya diff` 就看得出改了什麼。
- systemd 設了 `TasksMax=512`、`MemoryMax=2G`，即使是 root 也跑不出 fork bomb，也不會把整台機器的記憶體吃光。
- niya 用 `nohup` 或 `&` 啟動的程序，在 niya 重啟時會一起被關掉。所以長期執行的服務，niya 會做成 systemd unit。

### 4. 更新

```sh
cd /opt/niya
git pull
uv sync --frozen
# service 檔有改才需要重新複製；沒改就什麼都不會做
cmp -s deploy/niya.service /etc/systemd/system/niya.service || { cp deploy/niya.service /etc/systemd/system/ && systemctl daemon-reload; }
systemctl restart niya
```

- 新程式要重啟才會生效。重啟前 niya 如果正在睡覺整理，下次醒著閒下來時會續睡，已整理完的片段不會重做。
- service 檔用複製的，不用 `systemctl link` 連到 repo：niya 能改 `/opt/niya`，用複製的話，repo 裡的 service 檔被改動，也不會在你不知情時改掉 systemd 的設定（例如記憶體上限）。

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
