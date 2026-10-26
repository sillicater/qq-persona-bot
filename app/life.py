"""鲸鲸自己的生活：每天发生的小事、正在推进的长期线、此刻在干嘛。

没有这个，她就只是个接话的黑盒。有了它，聊天才有"她自己的内容"。
"""
import json
import logging
import time

from . import db

log = logging.getLogger("life")

_BUCKETS = [
    (0, 8, "早上"),
    (8, 12, "上午"),
    (12, 14, "中午"),
    (14, 18, "下午"),
    (18, 23, "晚上"),
    (23, 24, "深夜"),
]


def _today():
    return time.strftime("%Y-%m-%d")


def has_today():
    return bool(db.one("SELECT id FROM life_events WHERE day=? LIMIT 1", (_today(),)))


def events_today():
    return db.q("SELECT text FROM life_events WHERE day=? ORDER BY id", (_today(),))


def recent_events(days=3):
    return db.q(
        "SELECT text,day FROM life_events WHERE day<? ORDER BY id DESC LIMIT ?",
        (_today(), days * 2),
    )


def active_threads():
    return db.q(
        "SELECT title,note FROM life_threads WHERE status='active' ORDER BY id DESC LIMIT 5"
    )


def _doing_map():
    raw = db.meta_get(f"life:doing:{_today()}")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def current_doing(now=None):
    now = now or time.localtime()
    m = _doing_map()
    if not m:
        return ""
    h = now.tm_hour
    for a, b, name in _BUCKETS:
        if a <= h < b:
            return m.get(name, "")
    return ""


def render(now=None):
    lines = []
    evs = events_today()
    if evs:
        lines.append("（今天）")
        lines += [f"- {r['text']}" for r in evs]
    rec = recent_events()
    if rec:
        lines.append("（前两天）")
        lines += [f"- {r['text']}" for r in rec]
    th = active_threads()
    if th:
        lines.append("（你最近在忙的事）")
        lines += [f"- {t['title']}" + (f"：{t['note']}" if t["note"] else "") for t in th]
    doing = current_doing(now)
    if doing:
        lines.append(f"（这会儿）{doing}")
    return "\n".join(lines)


LIFE_SYSTEM = """你在给一个虚构的人物安排她自己的日常。她叫{name}，{age}岁，在大学读大一，性格温柔有点宅，喜欢海和音乐。

根据最近几天的经历，生成今天的素材。要求：
- 2 到 3 件具体的小事，要有画面感（比如"早八差点迟到，跑进教室的时候老师已经在点名了"），不要写成"今天学习了"这种空话。
- 必须和最近几天的事连贯，不要出现互相矛盾的内容（前天说在图书馆，今天别说没去过）。
- 再写今天几个时间段她大概在做什么，同样要具体。
- 如果最近有正在推进的事（考试、社团、朋友之间的小事），保持它继续，或者写成结束。

只输出 JSON：
{{"events": ["...", "..."],
  "doing": {{"早上": "...", "上午": "...", "中午": "...", "下午": "...", "晚上": "..."}},
  "threads": [{{"title": "在准备的东西", "note": "进展到哪了", "status": "active"}}]}}
"""


async def daily_tick(llm, persona):
    """每天生成一次。已经生成过就跳过。"""
    if has_today():
        return False
    ctx = []
    evs = recent_events(days=4)
    if evs:
        ctx.append("最近几天：")
        ctx += [f"- {r['day']} {r['text']}" for r in reversed(evs)]
    th = active_threads()
    if th:
        ctx.append("正在进行的事：")
        ctx += [f"- {t['title']}：{t['note']}" for t in th]

    obj = await llm.chat_json(
        [
            {
                "role": "system",
                "content": LIFE_SYSTEM.format(name=persona.name, age=persona.age or 19),
            },
            {"role": "user", "content": "\n".join(ctx) or "（这是第一天）"},
        ],
        temperature=1.1,
        max_tokens=800,
    )

    day = _today()
    now = time.time()
    n = 0
    for text in (obj.get("events") or [])[:4]:
        text = str(text).strip()[:120]
        if text:
            db.run("INSERT INTO life_events(ts,day,text) VALUES(?,?,?)", (now, day, text))
            n += 1

    doing = obj.get("doing") or {}
    if isinstance(doing, dict):
        db.meta_set(f"life:doing:{day}", json.dumps(doing, ensure_ascii=False))

    for t in (obj.get("threads") or [])[:4]:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or "").strip()[:30]
        if not title:
            continue
        status = "done" if str(t.get("status")) == "done" else "active"
        row = db.one("SELECT id FROM life_threads WHERE title=?", (title,))
        if row:
            db.run(
                "UPDATE life_threads SET note=?,status=?,updated=? WHERE id=?",
                (str(t.get("note") or "")[:80], status, now, row["id"]),
            )
        else:
            db.run(
                "INSERT INTO life_threads(title,note,status,updated) VALUES(?,?,?,?)",
                (title, str(t.get("note") or "")[:80], status, now),
            )

    log.info("今天的生活生成了 %d 件，长期线 %d 条", n, len(obj.get("threads") or []))
    return True
