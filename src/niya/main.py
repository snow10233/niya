import json
import os
import urllib.request

from dotenv import load_dotenv

# show 平常沒用到，是留給除錯用的（把下面 call_model 裡的 show(body) 取消註解）。
# 行尾的 noqa 是告訴 ruff：這行「引入了卻沒用到」（代號 F401）是故意的，不要報錯
from niya.debug import show  # noqa: F401
from niya.tools import TOOLS, run_tool

load_dotenv()
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

    # show(body)

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


if __name__ == "__main__":
    chat_loop()
