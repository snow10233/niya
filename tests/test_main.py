import pytest

from niya import main


# 每個測試開始前，先把對話紀錄清空，測試之間才不會互相影響
@pytest.fixture(autouse=True)
def empty_messages():
    main.messages.clear()


# 做一個假的模型：每被問一次，就照順序交出事先準備好的下一封回信
def fake_model(monkeypatch, replies):
    remaining = list(replies)

    def fake_call_model():
        return remaining.pop(0)

    monkeypatch.setattr(main, "call_model", fake_call_model)


def text_reply(text):
    return {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
    }


def tool_reply(tool_use_id, query):
    return {
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "id": tool_use_id,
                "name": "web_search",
                "input": {"query": query},
            }
        ],
    }


# 模型直接回答：問一次就結束，紀錄裡是一問一答
def test_answer_without_tools(monkeypatch):
    fake_model(monkeypatch, [text_reply("你好")])

    answer = main.ask("嗨")

    assert answer == "你好"
    assert main.messages == [
        {"role": "user", "content": "嗨"},
        {"role": "assistant", "content": [{"type": "text", "text": "你好"}]},
    ]


# 模型先要求搜尋：程式要執行工具、把結果寄回去，再問一次模型才拿到回答
def test_tool_use_then_answer(monkeypatch):
    fake_model(monkeypatch, [tool_reply("toolu_1", "天氣"), text_reply("今天晴天")])

    used = []

    def fake_run_tool(name, tool_input):
        used.append((name, tool_input))
        return "搜尋結果：晴天"

    monkeypatch.setattr(main, "run_tool", fake_run_tool)

    answer = main.ask("今天天氣如何？")

    assert answer == "今天晴天"
    assert used == [("web_search", {"query": "天氣"})]

    # 紀錄的順序：你的問題 → 模型要求工具 → 工具結果 → 模型的回答
    assert len(main.messages) == 4
    assert main.messages[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "搜尋結果：晴天",
            }
        ],
    }


# 模型連續要求兩次工具：迴圈要一直轉到它不再要求為止
def test_keeps_looping_until_model_stops_asking(monkeypatch):
    fake_model(
        monkeypatch,
        [
            tool_reply("toolu_1", "第一次"),
            tool_reply("toolu_2", "第二次"),
            text_reply("查完了"),
        ],
    )

    queries = []

    def fake_run_tool(_name, tool_input):
        queries.append(tool_input["query"])
        return "結果"

    monkeypatch.setattr(main, "run_tool", fake_run_tool)

    answer = main.ask("幫我查兩件事")

    assert answer == "查完了"
    assert queries == ["第一次", "第二次"]


# 第二次問話時，寄給模型的紀錄要包含第一輪的對話
def test_remembers_earlier_turns(monkeypatch):
    fake_model(monkeypatch, [text_reply("你好，小雪"), text_reply("你叫小雪")])

    main.ask("我叫小雪")
    main.ask("我叫什麼？")

    assert main.messages[0] == {"role": "user", "content": "我叫小雪"}
    assert main.messages[2] == {"role": "user", "content": "我叫什麼？"}
    assert len(main.messages) == 4
