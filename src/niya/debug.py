import json
import logging
import os
from datetime import date


# 把資料裡「其實是 JSON 的字串」拆回字典或清單，印出來才會一起排版
def unpack(data):
    if isinstance(data, dict):
        unpacked = {}
        for key, value in data.items():
            unpacked[key] = unpack(value)
        return unpacked

    if isinstance(data, list):
        unpacked = []
        for item in data:
            unpacked.append(unpack(item))
        return unpacked

    if isinstance(data, str) and data[:1] in ("{", "["):
        try:
            return unpack(json.loads(data))
        except json.JSONDecodeError:
            return data

    return data


# 除錯用：把資料排版成好讀的文字。只負責排版，要不要印出來由呼叫的人決定
def pretty(data):
    return json.dumps(unpack(data), indent=2, ensure_ascii=False)


# 今天的日期文字，例如 2026-11-27
def today():
    return date.today().isoformat()


# 一天一個檔的 log 寫手。
# 括號裡的 logging.Handler 是 logging 內建的「寫手」樣板：
# 照著它做，logging 就知道每一筆 log 要交給我們的 emit 處理
class DailyFileHandler(logging.Handler):
    def __init__(self, folder):
        # 先讓樣板做完它自己的準備工作
        super().__init__()

        self.folder = folder
        # 資料夾不存在就先建好；exist_ok=True 是「已經有了也不要報錯」
        os.makedirs(folder, exist_ok=True)

    # 現在該寫進哪個檔案，例如 logs/2026-11-27.log
    def current_file(self):
        return self.folder + "/" + today() + ".log"

    # logging 每收到一筆 log，就會呼叫一次 emit。
    # 每次都重新問「今天幾號」，所以過了半夜 12 點會自動換到新的檔案
    def emit(self, record):
        try:
            # "a" 是接在檔案後面寫；檔案還沒有就會自動建立
            with open(self.current_file(), "a", encoding="utf-8") as file:
                file.write(self.format(record) + "\n")
        except OSError:
            # OSError 是「跟檔案、硬碟有關的錯誤」，例如硬碟滿了、沒有權限。
            # 寫 log 失敗不該讓聊天跟著掛掉：
            # 交給樣板處理，它會把錯誤印出來，然後程式繼續跑
            self.handleError(record)
