"""离线冒烟测试：不联网、不连 QQ，验证记忆链路和分条逻辑。"""
import asyncio
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, memory  # noqa: E402
from app import bot as _bot  # noqa: E402,F401  （确保 bot.py 能编译）
from app.humanize import plan  # noqa: E402
from app.persona import Persona  # noqa: E402
from app.prompts import build_system  # noqa: E402


class FakeLLM:
    async def chat(self, messages, **kw):
        return "今天好累啊。\n改到第七版了 我真的会谢"

    async def chat_json(self, messages, **kw):
        sysmsg = messages[0]["content"] if messages else ""
        if "虚构的人物" in sysmsg:
            return {
                "events": [
                    "早八差点迟到，跑进教室老师已经开始点名了",
                    "食堂新出的糖醋里脊排队排了十分钟",
                ],
                "doing": {
                    "早上": "在赶去教室的路上",
                    "上午": "在上高数",
                    "中午": "食堂",
                    "下午": "图书馆",
                    "晚上": "宿舍躺着听歌",
                },
                "threads": [{"title": "大学英语四级", "note": "刚开始背单词", "status": "active"}],
            }
        return {
            "summary": "他说最近工作很累，天天加班改方案，我吐槽了两句",
            "topics": ["工作", "加班", "改稿"],
            "tone": "放松，愿意吐槽",
            "salience": 0.72,
            "facts": [
                {"key": "工作", "value": "在互联网公司做运营，最近天天加班", "category": "身份", "confidence": 0.9},
                {"key": "讨厌的事", "value": "反复改稿", "category": "偏好", "confidence": 0.8},
            ],
        }


async def main():
    tmp = tempfile.mkdtemp()
    db.init(pathlib.Path(tmp) / "test.db")
    memory.set_archive_path(pathlib.Path(tmp) / "档案.md")
    seeded = memory.seed_profile(ROOT / "persona" / "profile_seed.yaml")
    assert seeded >= 8, seeded
    archive = (pathlib.Path(tmp) / "档案.md").read_text(encoding="utf-8")
    assert "某某大学" in archive and "某某专业" in archive
    print(f"✓ 初始档案写入 {seeded} 条，已导出人可读档案")

    persona = Persona(ROOT / "persona" / "persona.yaml")
    llm = FakeLLM()
    chat = "10001"

    # 1. 人设
    core = persona.core_block()
    assert persona.name in core and "性格" in core, core
    print("✓ 人设内核注入")
    print(persona.summary())

    # 2. 对话写入
    for i in range(12):
        memory.add_message(chat, "user", f"第{i}条：今天又在改方案，快烦死了")
        memory.add_message(chat, "bot", "又改？第几版了啊")

    # 3. 压缩
    eid = await memory.compress(chat, llm, persona)
    assert eid, "压缩失败"
    eps = db.q("SELECT * FROM episodes")
    assert len(eps) == 1
    facts = db.q("SELECT key,value FROM facts")
    keys = {r["key"] for r in facts}
    assert "工作" in keys and "讨厌的事" in keys, keys
    print(f"✓ 压缩：片段 #{eid}，事实共 {len(facts)} 条（含初始 9 条）")

    # 4. 检索
    rec = memory.recall(chat, "你工作最近怎么样", 1500)
    assert "工作" in rec["facts"], rec
    assert "加班" in rec["episodes"], rec
    print("✓ 检索：按需召回事实与片段")
    print("  事实:", rec["facts"].replace("\n", " | "))
    print("  片段:", rec["episodes"])

    # 5. 历史
    hist = memory.history_messages(chat, 24, 1200)
    assert hist and hist[0]["role"] == "user"
    print(f"✓ 最近原文 {len(hist)} 条进上下文")

    # 6. 系统提示词
    sysp = build_system(persona, rec)
    assert persona.name in sysp and "怎么说话" in sysp
    print(f"✓ 系统提示词 {len(sysp)} 字")

    # 7. 反思演化（含单次漂移限幅）
    changes = await memory.reflect(llm, persona, chat)
    print(f"✓ 人设反思，变动字段: {changes or '无'}")
    before = persona.state["关系熟悉度"]
    persona.apply_reflection({"关系熟悉度": 1.0, "心情": "烦"}, reason="test")
    after = persona.state["关系熟悉度"]
    assert after - before <= 0.061, (before, after)
    assert persona.state["心情"] == "烦"
    print(f"✓ 关系熟悉度单次最多漂移 0.06：{before} → {after}")
    core2 = persona.core_block()
    assert "性格" in core2 and "大肥鱼" in core2, "内核被动过了"
    print("✓ 反思不会动到人格内核")

    # 自我设定
    assert persona.add_self_note("发现自己很怕冷场")
    assert persona.add_style_note("会跟着用省略号")
    block = persona.state_block()
    assert "怕冷场" in block and "省略号" in block
    print("✓ 自我设定写入并进入提示词")

    # 8. 分条
    for case in ["今天好累啊。\n改到第七版了 我真的会谢", "嗯", "他说他明天要去上海出差，大概三天，问我要不要带点什么特产回来，我说随便带点吃的就行"]:
        print("  分条:", plan(case))

    # 9. 衰减
    memory.decay()
    print("✓ 遗忘衰减")

    # 10. 节奏控制
    from app import pacing

    assert pacing.depth_of("你好") == 0
    assert pacing.depth_of("帮我看看这个装机配置怎么样？") >= 1
    assert pacing.depth_of("为什么光追开高了帧数掉这么多，是不是显存不够？") == 2
    slots = pacing.parse_slots("08:30-09:30,12:00-13:00")
    assert slots == [(510, 570), (720, 780)], slots
    assert pacing.busy_at(10, [(9, 12), (14, 17)])
    assert not pacing.busy_at(13, [(9, 12), (14, 17)])
    acts = [pacing.decide_reply("嗯", 3, "烦", False, True)[0] for _ in range(40)]
    assert "later" in acts or "skip" in acts
    assert pacing.burst_extra_delay(0, 1.6) == 0
    assert pacing.burst_extra_delay(3, 1.6) > 0
    short = pacing.typing_delay("好")
    long = pacing.typing_delay("这个变量一直报未定义 你看看是不是作用域写错了")
    assert short < long and 0.1 < short < 3 and 0.6 < long < 5, (short, long)
    hot = pacing.read_delay("嗯", 0, False, 0.35, 1.0)
    cold = pacing.read_delay("嗯", 0, False, 0.35, 0.0)
    assert hot < 14 and cold < 60, (hot, cold)
    print(f"✓ 打字/思考延迟：短句 {short:.1f}s，长句 {long:.1f}s，首条 {cold:.1f}s")
    print("✓ 节奏控制：深度判定、唤醒时间点、深夜不回")

    # 11. 课表解析
    import datetime as dt

    from app import schedule

    st = {
        "semesterStart": "2026-09-14",
        "dayOverrides": {},
        "holidays": [],
        "courses": [
            {"name": "大学英语", "day": 2, "startTime": "10:00", "endTime": "11:40", "weeks": "1-16", "location": "Ⅳ409"},
            {"name": "影视摄影", "day": 2, "startTime": "14:00", "endTime": "15:40", "weeks": "1-16", "location": "A201"},
        ],
        "tasks": [
            {"id": "t1", "title": "交纪录片作业", "course": "影视摄影", "deadline": "2026-10-22T23:59", "status": "todo"},
        ],
    }
    wed = dt.datetime(2026, 10, 21, 9, 30)
    text = schedule.render(st, wed)
    assert "大学英语" in text and "第6周" in text, text
    assert schedule.next_course(st, wed)["name"] == "大学英语"
    assert schedule.current_course(st, dt.datetime(2026, 10, 21, 10, 30))["name"] == "大学英语"
    base_now = dt.datetime(2026, 10, 25, 20, 0)
    assert schedule.parse_when("明天下午三点", base_now) == dt.datetime(2026, 10, 26, 15, 0)
    assert schedule.parse_when("今晚 21:30", base_now) == dt.datetime(2026, 10, 25, 21, 30)
    assert schedule.strip_when("明天下午三点 交纪录片作业") == "交纪录片作业"
    print("✓ 课表解析：今天的课、当前课程、下一节")

    # 12. 她自己的生活
    from app import life

    ok = await life.daily_tick(llm, persona)
    assert ok
    txt = life.render()
    assert "糖醋里脊" in txt and "四级" in txt, txt
    assert "食堂" in life.current_doing(dt.datetime(2026, 10, 26, 13, 0).timetuple())
    assert not await life.daily_tick(llm, persona), "同一天不应该重复生成"
    print("✓ 自己的生活：每日生成、时段状态、长期线")

    # 13. 文件 / 情绪 / 重复 / 离开
    memory.record_file(chat, "纪录片分镜表.xlsx", "20480")
    files = memory.recent_files_text(chat)
    assert "分镜表" in files, files
    rec["files"] = files
    sysp3 = build_system(persona, rec)
    assert "他发给你的文件" in sysp3 and "分镜表" in sysp3, sysp3[-800:]
    assert "他发给你的文件" not in build_system(persona, {"facts": "x", "episodes": ""})
    print("✓ 文件：记下名字，只有发过才出现这一栏")

    assert pacing.mood_after("挺好的", "今天好累啊") == "有点担心他"
    assert pacing.mood_after("挺好的", "我在看显卡价格") is None
    assert pacing.mood_after("有点担心他", "好累") is None
    print("✓ 情绪带动：被对方的话牵着走，不来回抖")

    assert memory.similarity("今天好累啊", "今天好累呀") > 0.5
    assert memory.similarity("今天好累啊", "显卡降价了") < 0.2
    print("✓ 重复检测：相似度算得出来")

    assert pacing.gap_hint(20 * 3600).startswith("你们有 20")
    assert pacing.gap_hint(3 * 86400).startswith("你们有 3 天")
    assert "吃饭" in pacing.away_line("在食堂排队")
    assert "图书馆" in pacing.away_line("在图书馆写作业")
    print("✓ 久别提示、离开理由跟着她的日程走")

    for msg in ["（发了张图片）", "（发了个表情包：狗头）", "（发了段语音）"]:
        s = build_system(persona, {"facts": "", "episodes": ""})
        assert "看不到内容" in s
    print("✓ 图片/语音仍然只当看不到内容处理")

    # 14. 审计回滚 / 证据门 / 闹钟 / 间隔重复
    persona.apply_reflection({"心情": "烦"}, reason="audit-test")
    hid = db.one("SELECT MAX(id) m FROM persona_history")["m"]
    assert "已回滚" in persona.rollback(hid)
    print("✓ 审计：人设改动可回滚")

    assert memory.recall(chat, "我妈让我穿秋裤", 1500)["episodes"] == ""
    print("✓ 证据门：对不上的问题不硬塞回忆")

    _bot.Bot._extract_alarms(_bot.Bot.__new__(_bot.Bot), chat, "嗯\n[[闹钟 2099-01-01 15:00 想问他面试]]")
    assert db.one("SELECT COUNT(*) c FROM alarms")["c"] >= 1
    print("✓ 闹钟：她自己定的主动消息能落库")

    f = db.one("SELECT interval,due_at FROM facts WHERE key='学校'")
    assert f["interval"] and f["due_at"]
    print("✓ 间隔重复：用过的记忆会排下次复习")

    # 15. 穿帮审查
    from app import guard

    am = dt.datetime(2026, 10, 26, 10, 30).timetuple()
    pm = dt.datetime(2026, 10, 26, 15, 0).timetuple()
    sun = dt.datetime(2026, 10, 25, 20, 0).timetuple()
    assert "睡不着" in guard.check("你是不是睡不着", "", am)
    assert guard.check("你怎么失眠了", "我失眠了", am) == ""
    assert "早安" in guard.check("早安呀", "", pm)
    assert "周一" in guard.check("明天周二交作业", "", sun)
    assert guard.check("明天周一交作业", "", sun) == ""
    print("✓ 穿帮审查：白天/早上/星期对不上会被抓出来")

    # 16. 表情包：情绪判断 + 不重样
    from app import stickers as _st

    _st.set_path(ROOT / "persona" / "stickers.json")
    assert _st.guess("笑死我了") == "大笑"
    assert _st.guess("今天好累啊") == "安慰"
    assert _st.guess("逆天 这也能行") == "无语"
    first = _st.pick("开心")
    second = _st.pick("开心", exclude=[first["id"]])
    assert first and second and second["id"] != first["id"]
    named = _st.pick("木大木大")
    again = _st.pick("木大木大", exclude=[named["id"]])
    assert named and again and again["id"] != named["id"], "换一个还换出同一张"
    assert _st.pick("完全不存在的词") is None
    print("✓ 表情包：按情绪选图，换一个不重样")

    import types

    b2 = _bot.Bot.__new__(_bot.Bot)
    b2.cfg = types.SimpleNamespace(sticker_chance=1.0)
    assert b2._maybe_sticker("笑死", "哈哈", 0, True) == "大笑"
    assert b2._maybe_sticker("笑死", "```code```", 0, True) == ""
    assert b2._maybe_sticker("笑死", "哈哈", 2, True) == ""
    print("✓ 表情包：模型没写标记时也会按频率补一张")

    print("\n全部通过")


if __name__ == "__main__":
    asyncio.run(main())
