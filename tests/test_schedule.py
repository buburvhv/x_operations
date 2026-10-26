import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import app.server as server


def test_daily_limit_and_gap(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    base = datetime(2026, 10, 22, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(server, "now", lambda: base)

    first = server.suggest_time(base + timedelta(minutes=30))
    with server.closing(server.connect()) as conn:
        conn.execute(
            "INSERT INTO drafts (text, scheduled_at, status, created_at, updated_at) VALUES ('a', ?, 'queued', ?, ?)",
            (first.isoformat(), base.isoformat(), base.isoformat()),
        )
        conn.commit()
    second = server.suggest_time(first + timedelta(hours=1))
    assert second - first >= timedelta(hours=4)

    with server.closing(server.connect()) as conn:
        for offset in (4, 8):
            when = first + timedelta(hours=offset)
            conn.execute(
                "INSERT INTO drafts (text, scheduled_at, status, created_at, updated_at) VALUES ('x', ?, 'queued', ?, ?)",
                (when.isoformat(), base.isoformat(), base.isoformat()),
            )
        conn.commit()
    fourth = server.suggest_time(first + timedelta(hours=12))
    assert fourth.date() == first.date() + timedelta(days=1)
    assert fourth.hour == 9


def test_topic_items_keep_engagement_fields():
    items = server.topic_items({"data": [{"id": "1", "text": "热点", "author": {"screenName": "alice"}, "metrics": {"likes": 9, "retweets": 2, "replies": 1}}]})
    assert items == [{"id": "1", "text": "热点", "author": "alice", "likes": 9, "retweets": 2, "replies": 1}]


def test_copy_variants_parse_model_json_and_enforce_limits():
    variants = server.copy_variants("热点", "我的判断", '{"variants":["第一条","第一条"," 第二 条 ",' + json.dumps("超" * 241) + "]}")
    assert variants == ["第一条", "第二条"]


def test_copy_variants_fall_back_without_model_output():
    assert server.copy_variants("热点内容", "我的判断", "") == ["热点内容 我的判断"]


def test_night_candidate_moves_to_morning(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    base = datetime(2026, 10, 22, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(server, "now", lambda: base)
    slot = server.suggest_time()
    assert slot.date() == base.date() + timedelta(days=1)
    assert server.DAY_START <= slot.hour < server.DAY_END


def test_account_state_reads_accounts_object(monkeypatch):
    payload = {"ok": True, "data": {"accounts": [{"name": "main", "screenName": "alice"}], "default": "main"}}
    monkeypatch.setattr(server, "run_agentx", lambda *a, **k: payload)
    assert server.account_state()["connected"] is True
    assert server.account_state()["screenName"] == "alice"
    monkeypatch.setattr(server, "run_agentx", lambda *a, **k: {"ok": True, "data": {"accounts": [], "default": ""}})
    assert server.account_state()["connected"] is False


def test_friendly_error_explains_verification():
    message = server.friendly_error({"ok": False, "error": {"code": "not_authenticated", "message": "login needs e-mail/phone verification (LoginAcid) — not automatable"}})
    assert "浏览器导入" in message


def test_parse_trends24_first_list_only():
    page = (
        '<div class=list-container><ol><li><span class=trend-name><a href="https://twitter.com/search?q=%E7%83%AD%E7%82%B9" class=trend-link>热点</a>'
        '<span class=tweet-count data-count="12000"></span></span></li></ol></div>'
        '<div class=list-container><a href="https://twitter.com/search?q=old" class=trend-link>old</a><span class=tweet-count data-count=""></span></div>'
    )
    assert server.parse_trends24(page) == [{"name": "热点", "query": "热点", "volume": 12000}]


def test_overdue_posts_expire_instead_of_burst(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    base = datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(server, "now", lambda: base)
    calls = []
    monkeypatch.setattr(server, "account_state", lambda: {"connected": True})
    monkeypatch.setattr(server, "run_agentx", lambda args, **k: calls.append(args) or {"ok": True, "data": {"id": "9", "author": {"screenName": "a"}}})
    with server.closing(server.connect()) as conn:
        for text, hours in (("old", 7), ("due1", 1), ("due2", 0.5)):
            when = (base - timedelta(hours=hours)).isoformat()
            conn.execute("INSERT INTO drafts (text, scheduled_at, status, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?)", (text, when, when, when))
        conn.commit()
    server.publish_due()
    status = {item["text"]: item["status"] for item in server.rows()}
    assert status == {"old": "expired", "due1": "posted", "due2": "queued"}
    assert len(calls) == 1
    assert server.rows("posted")[0]["tweet_url"] == "https://x.com/a/status/9"


def test_hot_query_targets_recent_chinese_originals(monkeypatch):
    monkeypatch.setattr(server, "now", lambda: datetime(2026, 10, 23, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
    query = server.hot_query("AI OR 大模型")
    assert query == "(AI OR 大模型) lang:zh min_faves:30 since:2026-10-22 -filter:replies -filter:retweets"


def test_rank_hot_weights_discussion_over_likes():
    items = [
        {"id": "1", "text": "a", "author": "x", "likes": 100, "retweets": 0, "replies": 0},
        {"id": "2", "text": "b", "author": "y", "likes": 20, "retweets": 20, "replies": 10},
    ]
    ranked = server.rank_hot(items)
    assert [item["id"] for item in ranked] == ["2", "1"]
    assert ranked[0]["url"] == "https://x.com/y/status/2"


def test_rank_hot_drops_giveaways_and_follow_spam():
    items = [
        {"id": "1", "text": "空投福利！评论区留下钱包地址", "author": "a", "likes": 900, "retweets": 500, "replies": 1200},
        {"id": "2", "text": "蓝V互关，有关必回", "author": "b", "likes": 90, "retweets": 1, "replies": 200},
        {"id": "3", "text": "Claude Code 你们一个会话干到底吗？", "author": "c", "likes": 394, "retweets": 11, "replies": 205},
    ]
    assert [item["id"] for item in server.rank_hot(items)] == ["3"]


def test_x_length_counts_chinese_double():
    assert server.x_length("abc") == 3
    assert server.x_length("中文") == 4
    assert server.x_length("看 https://example.com/very/long/path") == 2 + 1 + 23


def test_tidy_keeps_paragraphs_and_english_spaces():
    assert server.tidy("用了 Claude Code 两周\n\n\n\n结论 很 简单 ") == "用了 Claude Code 两周\n\n结论很简单"


def test_settings_change_daily_limit(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    client = server.app.test_client()
    assert client.post("/api/settings", json={"daily_limit": "9"}).status_code == 400
    assert client.post("/api/settings", json={"daily_limit": "1", "ai_api_key": "sk-secret-1234567890"}).status_code == 200
    assert server.daily_limit() == 1
    shown = client.get("/api/settings").get_json()["settings"]["ai_api_key"]
    assert "secret" not in shown
    client.post("/api/settings", json={"ai_api_key": ""})
    assert server.get_settings()["ai_api_key"] == "sk-secret-1234567890"


def test_post_now_respects_daily_limit_and_reschedules(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    base = datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(server, "now", lambda: base)
    monkeypatch.setattr(server, "account_state", lambda: {"connected": True})
    monkeypatch.setattr(server, "run_agentx", lambda args, **k: {"ok": True, "data": {"id": "1", "author": {"screenName": "a"}}})
    client = server.app.test_client()
    client.post("/api/drafts", json={"text": "第一条"})
    client.post("/api/drafts", json={"text": "第二条"})
    first, second = server.rows("queued")
    assert client.post(f"/api/drafts/{second['id']}/now").status_code == 200
    queued = server.rows("queued")[0]
    assert datetime.fromisoformat(queued["scheduled_at"]) - base >= timedelta(hours=4)
    server.save_settings({"daily_limit": "1"})
    response = client.post(f"/api/drafts/{queued['id']}/now")
    assert response.status_code == 409 and "上限" in response.get_json()["error"]


def test_generate_copy_runs_writer_then_editor(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    server.save_settings({"ai_api_key": "k"})
    replies = iter([
        json.dumps({"candidates": [{"type": "反常识", "text": "草稿一"}, {"type": "亲历", "text": "草稿二"}]}),
        "```json\n" + json.dumps({"variants": [{"text": "改好的一", "type": "亲历", "score": 26, "why": "具体"}]}) + "\n```",
    ])
    calls = []
    monkeypatch.setattr(server, "chat", lambda s, system, user, t, model=None: calls.append(t) or next(replies))
    result = server.generate_copy("话题", ["素材"], "")
    assert calls == [0.9, 0.2]
    assert result == [{"text": "改好的一", "type": "亲历", "score": 26, "why": "具体"}]


def test_parse_cn_hot_normalizes_and_drops_ads():
    weibo = {"data": {"realtime": [{"word": "微信适配新机", "num": 830461, "flag_desc": "科技"}, {"word": "广告位", "num": 1, "is_ad": 1}]}}
    assert server.parse_cn_hot("weibo", weibo) == [{"name": "微信适配新机", "summary": "科技", "heat": 830461, "url": "https://s.weibo.com/weibo?q=%23%E5%BE%AE%E4%BF%A1%E9%80%82%E9%85%8D%E6%96%B0%E6%9C%BA%23"}]
    douyin = {"data": {"word_list": [{"word": "新品发布", "hot_value": 100}]}}
    assert server.parse_cn_hot("douyin", douyin)[0]["heat"] == 100
    zhihu = {"data": [{"detail_text": "563 万热度", "target": {"id": "9", "title": "问题？", "excerpt": "摘要"}}]}
    assert server.parse_cn_hot("zhihu", zhihu) == [{"name": "问题？", "summary": "摘要", "heat": 5630000, "url": "https://www.zhihu.com/question/9"}]


def test_quick_post_picks_event_then_returns_one_post(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    server.save_settings({"ai_api_key": "k", "ai_model": "writer", "ai_editor_model": "editor"})
    monkeypatch.setattr(server, "account_state", lambda: {"connected": False})
    monkeypatch.setattr(server, "fetch_cn_hot", lambda source: [
        {"name": "地震伤亡", "summary": "", "heat": 9, "url": "u1"},
        {"name": "某 App 涨价到 199 元", "summary": "会员年费从 99 涨到 199", "heat": 5, "url": "u2"},
    ])
    replies = iter([
        json.dumps({"index": 1, "why": "和读者相关", "emotion": "克制的不满", "angle": "涨价背后的逻辑"}),
        json.dumps({"facts": [{"fact": "年费从 99 涨到 199", "source": 2}], "candidates": [{"type": "亲历", "text": "草稿"}]}),
        json.dumps({"variants": [{"text": "定稿", "type": "亲历", "score": 25, "why": "具体"}]}),
    ])
    calls = []
    monkeypatch.setattr(server, "chat", lambda s, system, user, t, model=None: calls.append((model, user)) or next(replies))
    result = server.quick_post("zhihu", "", None, ["地震伤亡"])
    assert "地震伤亡" not in calls[0][1]  # 跳过的事件不进候选
    assert [model for model, _ in calls] == ["editor", None, "editor"]
    assert result["post"]["text"] == "定稿"
    assert result["event"]["name"] == "某 App 涨价到 199 元" and result["event"]["emotion"] == "克制的不满"
    assert result["facts"] == [{"fact": "年费从 99 涨到 199", "source": "热榜摘要：会员年费从 99 涨到 199"}]


def test_revise_uses_preset_and_facts(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    server.save_settings({"ai_api_key": "k"})
    seen = {}
    monkeypatch.setattr(server, "chat", lambda s, system, user, t, model=None: seen.update(system=system, user=user) or '{"text":"短 一点 的版本"}')
    client = server.app.test_client()
    response = client.post("/api/revise", json={"text": "原文", "preset": "shorter", "facts": ["事实A"]}).get_json()
    assert response["text"] == "短一点的版本"
    assert "事实A" in seen["system"] and "60 个汉字" in seen["user"]
    assert client.post("/api/revise", json={"text": "原文"}).status_code == 400


def test_create_draft_returns_id(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    body = server.app.test_client().post("/api/drafts", json={"text": "一条"}).get_json()
    assert body["id"] == server.rows("queued")[0]["id"]


def test_thread_posts_only_when_asked(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    calls = []
    clock = {"at": datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Shanghai"))}
    monkeypatch.setattr(server, "now", lambda: clock["at"])
    monkeypatch.setattr(server, "account_state", lambda: {"connected": True})
    monkeypatch.setattr(server, "run_agentx", lambda args, **k: calls.append(args) or {"ok": True, "data": {"id": str(len(calls)), "author": {"screenName": "a"}}})
    client = server.app.test_client()

    # 带空行的普通帖子必须整条发出，不能被拆成线程
    plain = client.post("/api/drafts", json={"text": "第一段。\n\n第二段。"}).get_json()["id"]
    assert client.post(f"/api/drafts/{plain}/now").status_code == 200
    assert len(calls) == 1 and calls[0][0] == "post" and calls[0][-1] == "第一段。\n\n第二段。"

    # 勾选线程后才按空行拆分，逐条回复
    clock["at"] += timedelta(hours=2)
    calls.clear()
    thread = client.post("/api/drafts", json={"text": "第一段\n\n第二段\n\n第三段", "thread": True}).get_json()["id"]
    assert client.post(f"/api/drafts/{thread}/now").status_code == 200
    assert calls[0][-1] == "第一段"
    assert [(c[0], c[-2]) for c in calls[1:]] == [("reply", "1"), ("reply", "2")]
    assert server.rows("posted")[0]["error"] is None  # 发完整时不显示成错误


def test_thread_reports_partial_send(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    monkeypatch.setattr(server, "account_state", lambda: {"connected": True})

    def fake(args, **k):
        if args[0] == "reply":
            return {"ok": False, "error": {"code": "rate_limited", "message": "slow down"}}
        return {"ok": True, "data": {"id": "1", "author": {"screenName": "a"}}}

    monkeypatch.setattr(server, "run_agentx", fake)
    client = server.app.test_client()
    draft = client.post("/api/drafts", json={"text": "第一段\n\n第二段", "thread": True}).get_json()["id"]
    client.post(f"/api/drafts/{draft}/now")
    assert "只发出了前 1 段" in server.rows("posted")[0]["error"]


def test_thread_rejects_bad_splits(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    client = server.app.test_client()
    assert client.post("/api/drafts", json={"text": "只有一段", "thread": True}).status_code == 400
    assert client.post("/api/drafts", json={"text": "短\n\n" + "超" * 150, "thread": True}).status_code == 400
    # 单条帖子仍然受 280 限制
    assert client.post("/api/drafts", json={"text": "超" * 150}).status_code == 400


def test_late_evening_slot_rolls_to_next_best_hour(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    server.save_settings({"best_hours": "10,21"})
    base = datetime(2026, 10, 22, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(server, "now", lambda: base)
    slot = server.suggest_time(datetime(2026, 10, 22, 22, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert slot.date() == base.date() + timedelta(days=1)
    assert slot.hour == 10


def test_cross_site_request_is_refused(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    client = server.app.test_client()
    response = client.post("/api/drafts", json={"text": "跨站发帖"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert server.rows() == []


def test_parse_cn_hot_tolerates_odd_payloads():
    assert server.parse_cn_hot("douyin", {"data": {"word_list": [{"word": "话题", "hot_value": "很多"}]}})[0]["heat"] == 0
    assert server.parse_cn_hot("zhihu", {"data": [{"detail_text": "563 万热度", "target": {"id": "1", "title": "问", "excerpt": None}}]})[0]["heat"] == 5630000
    for source in ("weibo", "douyin", "zhihu"):
        assert server.parse_cn_hot(source, {"data": "坏数据"}) == []


def test_parse_json_ignores_thinking_preamble():
    # 推理模型会先输出 <think>，里面也可能有大括号，不能把这段算进 JSON
    text = '<think>先想一下 {"index": 9}</think>\n{"index": 1, "why": "相关"}'
    assert server.parse_json(text) == {"index": 1, "why": "相关"}
    assert server.parse_json("没有 json") == {}
    assert server.parse_json('前文 {"a": 1} 真正的答案是 {"b": 2, "c": 3}') == {"b": 2, "c": 3}
    payload = {"ok": True, "data": {"legacy": {"favorite_count": 7, "retweet_count": 3}, "views": {"count": "800"}}}
    assert server.parse_analytics(payload) == {"likes": 7, "retweets": 3}
    assert server.parse_analytics({"ok": True, "data": {}}) is None


def test_today_count_counts_posted_not_queued(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    base = datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(server, "now", lambda: base)
    with server.closing(server.connect()) as conn:
        conn.execute("INSERT INTO drafts (text, status, created_at, updated_at, posted_at) VALUES ('发了', 'posted', ?, ?, ?)", (base.isoformat(),) * 3)
        conn.execute("INSERT INTO drafts (text, status, created_at, updated_at, scheduled_at) VALUES ('排了', 'queued', ?, ?, ?)", (base.isoformat(),) * 3)
        conn.commit()
    assert server.app.test_client().get("/api/status").get_json()["todayCount"] == 1


def test_parse_analytics_accepts_fxtwitter_shape():
    payload = {"ok": True, "data": {"tweet": {"likes": 5, "retweets": 2, "replies": 1, "views": 800}}}
    assert server.parse_analytics(payload) == {"likes": 5, "retweets": 2, "replies": 1, "views": 800}
    assert server.engagement({"likes": 5, "retweets": 2, "replies": 1, "views": 800}) == 5 + 8 + 1 + 4


def test_best_hours_need_enough_samples(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    base = datetime(2026, 10, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    with server.closing(server.connect()) as conn:
        for day, hour, views in ((0, 10, 100), (1, 10, 100), (0, 21, 9000), (1, 21, 9000)):
            posted = (base + timedelta(days=day, hours=hour)).isoformat()
            metrics = json.dumps({"views": views, "likes": 0, "retweets": 0, "replies": 0})
            conn.execute(
                "INSERT INTO drafts (text, status, created_at, updated_at, posted_at, tweet_id, metrics) VALUES (?, 'posted', ?, ?, ?, ?, ?)",
                (f"{day}-{hour}", posted, posted, posted, f"id{day}{hour}", metrics),
            )
        conn.commit()
    server.update_best_hours()
    assert server.get_settings()["best_hours"] == ""  # 只有两个小时有样本，不足以判断
    with server.closing(server.connect()) as conn:
        for day, hour in ((0, 15), (1, 15)):
            posted = (base + timedelta(days=day, hours=hour)).isoformat()
            conn.execute(
                "INSERT INTO drafts (text, status, created_at, updated_at, posted_at, tweet_id, metrics) VALUES (?, 'posted', ?, ?, ?, ?, ?)",
                (f"m{day}", posted, posted, posted, f"m{day}{hour}", json.dumps({"views": 1000, "likes": 0, "retweets": 0, "replies": 0})),
            )
        conn.commit()
    server.update_best_hours()
    assert "21" in server.get_settings()["best_hours"].split(",")


def test_suggest_time_prefers_best_hours(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    server.save_settings({"best_hours": "21"})
    base = datetime(2026, 10, 22, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(server, "now", lambda: base)
    slot = server.suggest_time()
    assert slot.hour == 21


def test_card_attaches_image(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    image = tmp_path / "card.png"
    monkeypatch.setattr(server, "render_card", lambda text: image)
    client = server.app.test_client()
    draft_id = client.post("/api/drafts", json={"text": "配图这条"}).get_json()["id"]
    assert client.post(f"/api/drafts/{draft_id}/card").status_code == 200
    assert server.rows()[0]["image"] == str(image)
    assert client.get("/api/status").get_json()["drafts"][0]["hasImage"] is True


def test_saved_inspiration_round_trip(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    client = server.app.test_client()
    assert client.post("/api/saved", json={"text": "一条好帖子", "note": "可写"}).status_code == 200
    items = client.get("/api/saved").get_json()["items"]
    assert items[0]["text"] == "一条好帖子" and items[0]["note"] == "可写"
    assert client.delete(f"/api/saved/{items[0]['id']}").status_code == 200
    assert client.get("/api/saved").get_json()["items"] == []


def test_sample_appends_without_duplicating(tmp_path, monkeypatch):
    server.DB_PATH = tmp_path / "poster.db"
    server.init_db()
    server.save_settings({"samples": "已有的样本"})
    client = server.app.test_client()
    client.post("/api/samples", json={"text": "新爆款"})
    client.post("/api/samples", json={"text": "新爆款"})
    assert server.get_settings()["samples"] == "已有的样本\n\n新爆款"
