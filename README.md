# niya

niya 是一個長期運行的 agent，想做到有時間感、會遺忘、會睡覺整理的記憶。

目前從頭重做中：每一步都要小到看得懂、講得出它在做什麼，再往上疊。

舊版留在 [`v0` 分支](https://github.com/snowbox-dev/niya/tree/v0)，只當參考，不在上面繼續開發。

## 現在能做什麼

在終端機跟 Claude 聊天，它會記得這次對話裡前面講過的話。程式關掉後就忘了。

它可以自己決定要不要上網搜尋（用 Brave Search），查完再回答。

## 怎麼跑

需要先裝好 [uv](https://docs.astral.sh/uv/)。

1. 把 `.env.example` 複製成 `.env`，填上 Anthropic 和 Brave Search 的 API key。
2. 執行：

   ```
   uv run niya
   ```

   第一次跑的時候，uv 會自動把需要的套件裝好。

3. 打 `/q` 離開。

## 要改程式的話

clone 下來之後，先跑一次：

```
uv run pre-commit install
```

之後每次 `git commit`，都會先自動檢查排版和程式（用 [ruff](https://docs.astral.sh/ruff/)），不過就擋下來。被擋下來時：

- 排版不對：跑 `uv run ruff format`，它會幫你排好，再 `git add`、重新 commit。
- 程式檢查不過：照訊息改好，再 `git add`、重新 commit。

想看每次送給模型、模型回覆的完整內容，用除錯模式跑：

```
uv run niya --debug
```

內容會寫進 `logs/niya.log`。想邊聊邊看，另開一個終端機跑 `tail -f logs/niya.log`。

測試用 `uv run pytest`。同樣的檢查和測試，開 PR 時 GitHub 也會再跑一次。
