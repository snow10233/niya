from niya.debug import unpack


def test_plain_values_stay_the_same():
    assert unpack("hello") == "hello"
    assert unpack(42) == 42
    assert unpack(None) is None


def test_json_string_becomes_real_data():
    assert unpack('{"a": 1}') == {"a": 1}
    assert unpack("[1, 2]") == [1, 2]


def test_json_string_inside_dict_and_list():
    data = {
        "content": '[{"title": "t"}]',
        "items": ["x", '{"n": 2}'],
    }

    assert unpack(data) == {
        "content": [{"title": "t"}],
        "items": ["x", {"n": 2}],
    }


# JSON 字串裡面又包了一層 JSON 字串，也要拆到底
def test_json_string_inside_json_string():
    assert unpack('{"outer": "[1, 2]"}') == {"outer": [1, 2]}


# 開頭是 { 但其實不是 JSON，要原樣留著，不能出錯
def test_string_that_only_looks_like_json():
    assert unpack("{不是 JSON") == "{不是 JSON"
