# niya

niya 是一個長期運行的 agent，想做到有時間感、會遺忘、會睡覺整理的記憶。

目前從頭重做中：每一步都要小到看得懂、講得出它在做什麼，再往上疊。

舊版留在 [`v0` 分支](https://github.com/snowbox-dev/niya/tree/v0)，只當參考，不在上面繼續開發。

## 現在能做什麼

在終端機跟 Claude 聊天，它會記得這次對話裡前面講過的話。程式關掉後就忘了。

## 怎麼跑

需要先裝好 [uv](https://docs.astral.sh/uv/)。

1. 把 `.env.example` 複製成 `.env`，填上 Anthropic 的 API key。
2. 執行：

   ```
   uv run niya.py
   ```

   第一次跑的時候，uv 會自動把需要的套件裝好。

3. 打 `/q` 離開。
