import json
import os
import urllib.parse
import urllib.request

SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

# 給模型看的菜單：有哪些工具、什麼時候用、要給什麼參數
TOOLS = [
    {
        "name": "web_search",
        "description": "搜尋網路，回傳幾筆結果的標題、網址和摘要。需要最新的資訊，或不確定某件事時使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要搜尋的關鍵字"},
            },
            "required": ["query"],
        },
    }
]


def web_search(query):
    request = urllib.request.Request(
        SEARCH_URL + "?" + urllib.parse.urlencode({"q": query, "count": 5}),
        headers={"X-Subscription-Token": os.environ["BRAVE_SEARCH_API_KEY"]},
    )

    with urllib.request.urlopen(request) as response:
        found = json.load(response)

    results = []
    for item in found["web"]["results"]:
        results.append(
            {
                "title": item["title"],
                "url": item["url"],
                "description": item["description"],
            }
        )
    return json.dumps(results, ensure_ascii=False)


# 模型說要用哪個工具，就去執行對應的函式，回傳結果的文字
def run_tool(name, tool_input):
    if name == "web_search":
        return web_search(tool_input["query"])
