from niya import tools


# 模型說要用 web_search，run_tool 要把關鍵字交給 web_search，並把它的結果傳回來
def test_run_tool_calls_web_search(monkeypatch):
    searched = []

    def fake_web_search(query):
        searched.append(query)
        return "假的搜尋結果"

    monkeypatch.setattr(tools, "web_search", fake_web_search)

    result = tools.run_tool("web_search", {"query": "python"})

    assert searched == ["python"]
    assert result == "假的搜尋結果"


# 菜單上的每個工具，都要有模型需要的三樣東西
def test_every_tool_has_name_description_and_schema():
    for tool in tools.TOOLS:
        assert tool["name"]
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"
