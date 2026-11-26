import json
import os
import urllib.request

API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = "claude-haiku-5-5"
URL = "https://api.anthropic.com/v1/messages"


def ask(question):
    body = {
        "model": MODEL,
        "max_tokens": 10000,
        "messages": [{"role": "user", "content": question}],
    }
    request = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2026-10-21",
            "Content-Type": "application/json",
        },
    )

    with urllib.request.urlopen(request) as response:
        reply = json.load(response)

    # print(json.dumps(reply, indent=2, ensure_ascii=False))

    for block in reply["content"]:
        if block["type"] == "text":
            return block["text"]


question = input("你：")
print("niya：" + ask(question))
