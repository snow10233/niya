from niya.conversation import Conversation


# 剛建立的對話紀錄是空的
def test_starts_empty():
    conversation = Conversation()

    assert conversation.messages == []


# 三種訊息加進去之後，要照加入的順序排好，而且是 API 要的格式
def test_messages_are_kept_in_order():
    conversation = Conversation()

    conversation.add_user("今天天氣如何？")
    conversation.add_assistant([{"type": "text", "text": "我查一下"}])
    conversation.add_tool_results([{"type": "tool_result", "content": "晴天"}])

    assert conversation.messages == [
        {"role": "user", "content": "今天天氣如何？"},
        {"role": "assistant", "content": [{"type": "text", "text": "我查一下"}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "晴天"}]},
    ]


# 兩個對話紀錄各自獨立，寫進其中一個不會跑到另一個
def test_two_conversations_do_not_share_messages():
    first = Conversation()
    second = Conversation()

    first.add_user("只講給第一個聽")

    assert len(first.messages) == 1
    assert second.messages == []
