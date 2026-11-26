import json


# 把資料裡「其實是 JSON 的字串」拆回字典或清單，印出來才會一起排版
def unpack(data):
    if isinstance(data, dict):
        return {key: unpack(value) for key, value in data.items()}

    if isinstance(data, list):
        return [unpack(item) for item in data]

    if isinstance(data, str) and data[:1] in ("{", "["):
        try:
            return unpack(json.loads(data))
        except json.JSONDecodeError:
            return data

    return data


# 除錯用：把資料排版後印出來
def show(data):
    print(json.dumps(unpack(data), indent=2, ensure_ascii=False))
