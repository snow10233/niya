import argparse
import json
import logging
import os
import urllib.request

from dotenv import load_dotenv

from niya.debug import pretty
from niya.tools import TOOLS, run_tool

load_dotenv()

# 這個檔案專用的記錄器。__name__ 是這個檔案的名字（niya.main），會出現在每一行 log 的開頭
logger = logging.getLogger(__name__)

# 除錯紀錄放哪。路徑是從「執行指令時所在的資料夾」算起
LOG_DIR = "logs"
LOG_FILE = LOG_DIR + "/niya.log"

MODEL = "claude-haiku-5-5"
URL = "https://api.anthropic.com/v1/messages"
messages = []


def call_model():
    body = {
        "model": MODEL,
        "max_tokens": 10000,
        "tools": TOOLS,
        "messages": messages,
    }
    request = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2026-10-21",
            "Content-Type": "application/json",
        },
    )

    # 這行一直留著：平常不會記，啟動時加 --debug 才會寫進 log 檔
    logger.debug("送給模型的內容：\n" + pretty(body))

    with urllib.request.urlopen(request) as response:
        reply = json.load(response)

    logger.debug("模型回覆的內容：\n" + pretty(reply))

    return reply


def ask(question):
    messages.append({"role": "user", "content": question})

    while True:
        reply = call_model()

        messages.append({"role": "assistant", "content": reply["content"]})

        if reply["stop_reason"] != "tool_use":
            break

        tool_results = []
        for block in reply["content"]:
            if block["type"] == "tool_use":
                print(
                    "（使用工具："
                    + block["name"]
                    + " "
                    + json.dumps(block["input"], ensure_ascii=False)
                    + "）"
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": run_tool(block["name"], block["input"]),
                    }
                )

        messages.append({"role": "user", "content": tool_results})

    for block in reply["content"]:
        if block["type"] == "text":
            return block["text"]


def chat_loop():
    while True:
        question = input("你：")

        # To exit current session
        if question == "/q":
            return

        print("niya：" + ask(question))
        print("----------------------------------------")


# 程式的入口：先讀命令列選項、做好啟動設定，再開始聊天
def main():
    parser = argparse.ArgumentParser()
    # action="store_true"：這個選項後面不用接值，有出現就是 True，沒出現就是 False
    parser.add_argument(
        "--debug",
        action="store_true",
        help="把每次送給模型、模型回覆的完整內容寫進 " + LOG_FILE,
    )
    args = parser.parse_args()

    # 除錯開關：有加 --debug，才把 DEBUG 等級的 log 寫進檔案
    if args.debug:
        # 資料夾不存在就先建好；exist_ok=True 是「已經有了也不要報錯」
        os.makedirs(LOG_DIR, exist_ok=True)
        logging.basicConfig(
            level=logging.DEBUG,
            # 有給 filename，log 就改寫進檔案（接在舊內容後面），不再印到終端機
            filename=LOG_FILE,
            encoding="utf-8",
            # 每一筆 log 的開頭長什麼樣子：時間、等級、哪個檔案記的，然後才是內容
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        print("（除錯紀錄會寫到 " + LOG_FILE + "）")

    chat_loop()


if __name__ == "__main__":
    main()
