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

    # 這行一直留著：平常不會印，NIYA_DEBUG=1 時才會印出來
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
    # 除錯開關：環境變數 NIYA_DEBUG 是 1，才把 DEBUG 等級的 log 印出來
    if os.environ.get("NIYA_DEBUG") == "1":
        logging.basicConfig(level=logging.DEBUG)

    while True:
        question = input("你：")

        # To exit current session
        if question == "/q":
            return

        print("niya：" + ask(question))
        print("----------------------------------------")


if __name__ == "__main__":
    chat_loop()
