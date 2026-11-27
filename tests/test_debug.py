import logging

from niya import debug
from niya.debug import DailyFileHandler, pretty, unpack


# pretty 回傳排版好的文字：有縮排、中文照原樣、JSON 字串也拆開一起排
def test_pretty_returns_indented_text():
    text = pretty({"名字": "小雪", "content": "[1]"})

    assert text == '{\n  "名字": "小雪",\n  "content": [\n    1\n  ]\n}'


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


# 做一筆假的 log，內容是指定的文字
def make_record(text):
    return logging.makeLogRecord({"msg": text})


# 日期換了，log 就要寫進新日期的檔案，舊的檔案不能被動到
def test_daily_file_handler_switches_file_when_day_changes(monkeypatch, tmp_path):
    handler = DailyFileHandler(str(tmp_path))

    def fake_first_day():
        return "2026-11-27"

    def fake_second_day():
        return "2026-11-28"

    monkeypatch.setattr(debug, "today", fake_first_day)
    handler.emit(make_record("半夜前"))
    handler.emit(make_record("還是半夜前"))

    monkeypatch.setattr(debug, "today", fake_second_day)
    handler.emit(make_record("半夜後"))

    first_file = tmp_path / "2026-11-27.log"
    second_file = tmp_path / "2026-11-28.log"
    assert first_file.read_text(encoding="utf-8") == "半夜前\n還是半夜前\n"
    assert second_file.read_text(encoding="utf-8") == "半夜後\n"


# 資料夾還不存在時，要自己建好
def test_daily_file_handler_creates_folder(tmp_path):
    folder = tmp_path / "logs"

    DailyFileHandler(str(folder))

    assert folder.is_dir()
