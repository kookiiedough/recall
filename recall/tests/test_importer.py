import io
import json
import zipfile

from recall import importer

A = "https://www.tiktokv.com/share/video/7301234567890123456/"
B = "https://www.tiktok.com/@chef/video/7309999999999999999?is_from_webapp=1"


def ids(saves):
    return sorted(s.video_id for s in saves)


def test_standard_layout():
    data = {"Activity": {"Favorite Videos": {"FavoriteVideoList": [
        {"Date": "2024-03-01 12:00:00", "Link": A}, {"Date": "2024-03-02 08:30:00", "Link": B}]}}}
    saves = importer.parse_json_obj(data)
    assert ids(saves) == ["7301234567890123456", "7309999999999999999"]
    assert saves[0].saved_at is not None


def test_your_activity_lowercase_keys():
    data = {"Your Activity": {"Favorite Videos": {"FavoriteVideoList": [{"date": "2024-01-05 10:00:00", "link": A}]}}}
    assert ids(importer.parse_json_obj(data)) == ["7301234567890123456"]


def test_variant_key_names_and_videolink():
    data = {"Activity": {"Favourite Videos": {"Favourite Video List": [{"FavoriteDate": "2023-12-31T23:59:00Z", "VideoLink": B}]}}}
    s = importer.parse_json_obj(data)
    assert ids(s) == ["7309999999999999999"]
    assert abs(s[0].saved_at - 1704067140) < 1


def test_likes_and_history_are_not_saves():
    data = {"Activity": {
        "Favorite Videos": {"FavoriteVideoList": [{"Date": "2024-03-01 12:00:00", "Link": A}]},
        "Like List": {"ItemFavoriteList": [{"Date": "2024-03-01", "Link": B}]},
        "Video Browsing History": {"VideoList": [{"Date": "2024-03-01", "Link": B}]}}}
    assert ids(importer.parse_json_obj(data)) == ["7301234567890123456"]


def test_unknown_layout_fallback_skips_history():
    data = {"weird": {"stuff": [{"time": 1700000000, "url": A}]},
            "Watch History": {"VideoList": [{"Date": "2024-03-01", "Link": B}]}}
    s = importer.parse_json_obj(data)
    assert ids(s) == ["7301234567890123456"]
    assert s[0].saved_at == 1700000000


def test_dedupe_keeps_earliest_date():
    data = {"Activity": {"Favorite Videos": {"FavoriteVideoList": [
        {"Date": "2024-03-05 12:00:00", "Link": A}, {"Date": "2024-03-01 12:00:00", "Link": A}]}}}
    s = importer.parse_json_obj(data)
    assert len(s) == 1 and s[0].saved_at == importer.parse_date("2024-03-01 12:00:00")


def test_txt_export_blocks_and_sections():
    txt = ("Favorite Videos:\nDate: 2024-02-01 10:00:00\nLink: " + A + "\n\nDate: 2024-02-02 10:00:00\nLink: " + B +
           "\n\nWatch History:\nDate: 2024-02-03 10:00:00\nLink: https://www.tiktokv.com/share/video/7000000000000000009/\n")
    s = importer.parse_txt(txt)
    assert ids(s) == ["7301234567890123456", "7309999999999999999"]
    assert all(x.saved_at for x in s)


def test_txt_plain_url_list():
    s = importer.parse_bytes("links.txt", f"{A}\n{B}\n".encode())
    assert len(s) == 2


def test_zip_with_json_in_subfolder():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("TikTok_Data_123/user_data_tiktok.json", json.dumps(
            {"Activity": {"Favorite Videos": {"FavoriteVideoList": [{"Date": "2024-03-01 12:00:00", "Link": A}]}}}))
        z.writestr("TikTok_Data_123/readme.txt", "nothing here")
    assert ids(importer.parse_bytes("export.zip", buf.getvalue())) == ["7301234567890123456"]


def test_zip_with_txt_export():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Activity/Favorite Videos.txt", f"Date: 2024-02-01 10:00:00\nLink: {B}\n")
        z.writestr("Activity/Browsing History.txt", f"Date: 2024-02-01 10:00:00\nLink: {A}\n")
    assert ids(importer.parse_bytes("export.zip", buf.getvalue())) == ["7309999999999999999"]


def test_video_id_from_url_forms():
    assert importer.video_id_from_url("https://m.tiktok.com/v/7301234567890123456.html") == "7301234567890123456"
    assert importer.video_id_from_url("https://www.tiktok.com/@a.b/video/7301234567890123456?lang=en") == "7301234567890123456"
    assert importer.video_id_from_url("https://vm.tiktok.com/ZMabc123/") is None


def test_bom_and_utf8():
    raw = ("\ufeff" + json.dumps({"Activity": {"Favorite Videos": {"FavoriteVideoList": [{"Date": "2024-03-01 12:00:00", "Link": A}]}}})).encode("utf-8")
    assert len(importer.parse_bytes("user_data.json", raw)) == 1
