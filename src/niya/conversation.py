# 一場對話的紀錄：你說的、模型說的、工具的結果，照發生的順序排好。
# 之後跟對話紀錄有關的功能（例如壓縮）都加在這個 class 裡
class Conversation:
    def __init__(self):
        # 每建立一個 Conversation，就有一份自己的空紀錄，不會跟別的混在一起
        self.messages = []

    # 加一句你說的話
    def add_user(self, text):
        self.messages.append({"role": "user", "content": text})

    # 加一則模型的回覆。content 是模型回信裡的 content，原樣放進去
    def add_assistant(self, content):
        self.messages.append({"role": "assistant", "content": content})

    # 加工具的結果。API 規定工具結果要用 user 的身分寄回去
    def add_tool_results(self, tool_results):
        self.messages.append({"role": "user", "content": tool_results})
