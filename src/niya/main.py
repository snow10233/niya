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

    # 這行一直留著：平常不會印，啟動時加 --debug 才會印出來
    logger.debug("送給模型的內容：\n" + pretty(body))

    with urllib.request.urlopen(request) as response:
        return json.load(response)


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
        help="印出每次送給模型的完整內容",
    )
    args = parser.parse_args()

    # 除錯開關：有加 --debug，才把 DEBUG 等級的 log 印出來
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    chat_loop()


if __name__ == "__main__":
    main()
