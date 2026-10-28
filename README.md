# niya

會睡覺、會遺忘、有時間感的長期運行 agent。跑在 VPS 上，透過 Discord 跟主人對話。

- 每則訊息都帶時間戳，niya 知道每件事是什麼時候發生的。
- 記憶照遺忘曲線衰退，真的用上才會變牢。
- 一天睡兩次，把醒著時的經歷切成事件、整理成記憶，再清空 context。

目前完成：

- **M1 骨架**：設定、資料庫、OpenRouter client、工具註冊、帶時間戳的訊息呈現、agent loop、REPL 和環境檢查。
- **M2 工具與安全防護**：terminal（含背景程序）、檔案工具、網路搜尋與擷取；黑名單、受保護路徑、密鑰遮蔽、偽造標記中和、SSRF 防護，以及「可疑外傳才問」。

## 安全機制

niya 在 VPS 上以 root 執行，終端機只擋黑名單，不需要主人逐一核准。程式層的防線：

- **黑名單**：`rm -rf /` 這類無法復原的指令、停掉或刪除 niya 自己、停 sshd、清空防火牆、刪 `authorized_keys`、鎖 root 帳號。
- **受保護路徑**：檔案工具不能改 niya 的資料與設定；`authorized_keys` 只能新增 key；`sshd_config` 寫入後跑 `sshd -t`，失敗就還原。
- **密鑰遮蔽**：所有工具輸出都會把 niya 的密鑰換成 `[REDACTED]`，子程序的環境變數裡也沒有密鑰。
- **外部內容**：網頁、搜尋結果、curl 抓下來的內容會包進 `<untrusted>`，內容裡偽造的 `[時間] user:` 行和標籤會被改寫。
- **可疑外傳**：只有「讀過外部內容」＋「目的地是陌生主機」＋「傳的是本機檔案或指令輸出」三者同時成立才會暫停。終端機會顯示詢問，回「好」就放行。已知主機用 `/egress list|allow|remove` 管理，主人訊息裡提過的網域會自動算已知。

## 本機執行

```sh
cp .env.example .env   # 填入 OPENROUTER_API_KEY 和 NIYA_MODEL
uv sync
uv run --env-file .env niya doctor
uv run --env-file .env niya repl
```

- REPL 裡輸入 `/new` 開新 session，`/quit` 離開。
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
