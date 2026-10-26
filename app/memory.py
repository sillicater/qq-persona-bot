"""记忆系统：原文 → 片段压缩 → 事实抽取 → 检索调用 → 衰减遗忘。

设计上模仿人的记忆：
  工作记忆 = 最近若干条原文（永远在上下文里）
  情景记忆 = 压缩后的"回忆片段"，带重要度和被想起的次数
  语义记忆 = 从对话里抽出的事实条目（生日、工作、口味…）
  遗忘     = 很久没想起且不重要的片段，重要度衰减到休眠，但仍在库里可被搜到
"""
import json
import logging
import math
import pathlib
import time

from . import db

log = logging.getLogger("memory")

ARCHIVE_PATH = None
_CATEGORY_ORDER = ["身份", "偏好", "技能", "关系", "事件", "其他"]


def set_archive_path(p):
    global ARCHIVE_PATH
    ARCHIVE_PATH = pathlib.Path(p) if p else None


def seed_profile(path):
    """把 profile_seed.yaml 里的初始档案写进库；已存在的 key 不覆盖。"""
    import yaml

    path = pathlib.Path(path)
    if not path.exists():
        return 0
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    added = 0
    now = time.time()
    for f in data.get("facts") or []:
        key = str(f.get("key") or "").strip()
        val = str(f.get("value") or "").strip()
        if not key or not val:
            continue
        if db.one("SELECT id FROM facts WHERE key=?", (key,)):
            continue
        db.run(
            "INSERT INTO facts(key,value,category,confidence,salience,ts_created,ts_updated,last_used) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (key, val, str(f.get("category") or "其他"), 1.0, 0.95, now, now, now),
        )
        added += 1
    if added:
        log.info("初始档案写入 %d 条", added)
        export_profile()
    return added


def export_profile(path=None):
    """把事实库导出成一份人可读的档案。"""
    target = pathlib.Path(path) if path else ARCHIVE_PATH
    if not target:
        return
    rows = db.q("SELECT key,value,category,salience FROM facts ORDER BY category, salience DESC")
    groups = {}
    for r in rows:
        groups.setdefault(r["category"] or "其他", []).append(r)

    lines = ["# 关于你的档案", "", "*由鲸鲸根据聊天自动整理，随时会变*", ""]
    order = _CATEGORY_ORDER + [c for c in groups if c not in _CATEGORY_ORDER]
    for cat in order:
        if cat not in groups:
            continue
        lines.append(f"## {cat}")
        lines += [f"- **{r['key']}**：{r['value']}" for r in groups[cat]]
        lines.append("")
    try:
        target.write_text("\n".join(lines), encoding="utf-8")
    except OSError as e:
        log.warning("档案写入失败：%s", e)


def read_archive(limit=1800):
    if not ARCHIVE_PATH or not pathlib.Path(ARCHIVE_PATH).exists():
        return "档案还没建起来"
    txt = pathlib.Path(ARCHIVE_PATH).read_text(encoding="utf-8")
    return txt[:limit]


def est_tokens(s):
    return max(1, int(len(s) / 1.35))


def _bigrams(s):
    s = "".join(ch for ch in s if not ch.isspace())
    return {s[i : i + 2] for i in range(len(s) - 1)}


def similarity(a, b):
    """两句文本的字面相似度，用来避免反复说同一句话。"""
    A, B = _bigrams(a or ""), _bigrams(b or "")
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


# ---------------------------------------------------------------- 写入
def add_message(chat_id, role, text):
    return db.run(
        "INSERT INTO messages(ts,chat_id,role,text,tokens,summarized) VALUES(?,?,?,?,?,0)",
        (time.time(), str(chat_id), role, text, est_tokens(text)),
    )


def last_user_id(chat_id):
    row = db.one(
        "SELECT MAX(id) AS m FROM messages WHERE chat_id=? AND role='user'", (str(chat_id),)
    )
    return row["m"] if row else None


def recent_bot_texts(chat_id, limit=3):
    rows = db.q(
        "SELECT text FROM messages WHERE chat_id=? AND role='bot' ORDER BY id DESC LIMIT ?",
        (str(chat_id), limit),
    )
    return [r["text"] for r in rows]


def recent_user_texts(chat_id, limit=3):
    rows = db.q(
        "SELECT text FROM messages WHERE chat_id=? AND role='user' ORDER BY id DESC LIMIT ?",
        (str(chat_id), limit),
    )
    return [r["text"] for r in rows]


def count_recent(chat_id, seconds):
    row = db.one(
        "SELECT COUNT(*) c FROM messages WHERE chat_id=? AND ts>?",
        (str(chat_id), time.time() - seconds),
    )
    return row["c"] if row else 0


def record_file(chat_id, name, size=""):
    name = str(name or "").strip()[:120]
    if not name:
        return
    db.run(
        "INSERT INTO files(ts,chat_id,name,size) VALUES(?,?,?,?)",
        (time.time(), str(chat_id), name, str(size or "")[:20]),
    )


def recent_files_text(chat_id, limit=5):
    rows = db.q(
        "SELECT name,ts FROM files WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (str(chat_id), limit),
    )
    if not rows:
        return ""
    lines = []
    for r in rows:
        day = time.strftime("%m-%d", time.localtime(r["ts"]))
        lines.append(f"- {r['name']}（{day}）")
    return "\n".join(lines)


def last_user_ts(chat_id):
    row = db.one(
        "SELECT MAX(ts) AS m FROM messages WHERE chat_id=? AND role='user'", (str(chat_id),)
    )
    return row["m"] if row else None


def recent(chat_id, limit):
    rows = db.q(
        "SELECT role,text,ts FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (str(chat_id), limit),
    )
    return list(reversed(rows))


def history_messages(chat_id, limit, token_budget):
    """最近原文，从最新往回取，直到超预算。"""
    out = []
    used = 0
    for r in reversed(recent(chat_id, limit * 3)):
        t = est_tokens(r["text"])
        if used + t > token_budget and out:
            break
        used += t
        out.append({"role": "user" if r["role"] == "user" else "assistant", "content": r["text"]})
    out.reverse()
    # 保证以 user 开头（部分模型要求）
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def unsummarized(chat_id):
    return db.q(
        "SELECT id,role,text,ts FROM messages WHERE chat_id=? AND summarized=0 AND role IN ('user','bot') ORDER BY id",
        (str(chat_id),),
    )


def maybe_compress_threshold(chat_id, threshold):
    return len(unsummarized(chat_id)) >= threshold


# ---------------------------------------------------------------- 压缩
async def compress(chat_id, llm, persona):
    rows = unsummarized(chat_id)
    if len(rows) < 6:
        return None
    transcript = "\n".join(
        f"{'对方' if r['role'] == 'user' else persona.name}：{r['text']}" for r in rows
    )
    from .prompts import COMPRESS_SYSTEM

    obj = await llm.chat_json(
        [
            {"role": "system", "content": COMPRESS_SYSTEM},
            {"role": "user", "content": transcript},
        ],
        temperature=0.2,
    )
    summary = str(obj.get("summary") or "").strip()[:240]
    if not summary:
        return None
    topics = "，".join(str(x) for x in (obj.get("topics") or [])[:6])
    try:
        salience = max(0.0, min(1.0, float(obj.get("salience", 0.5))))
    except (TypeError, ValueError):
        salience = 0.5

    eid = db.run(
        "INSERT INTO episodes(ts_start,ts_end,chat_id,summary,topics,tone,salience,n_msgs) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (rows[0]["ts"], rows[-1]["ts"], str(chat_id), summary, topics,
         str(obj.get("tone") or "")[:40], salience, len(rows)),
    )
    for r in rows:
        db.run("UPDATE messages SET summarized=1, episode_id=? WHERE id=?", (eid, r["id"]))

    for f in obj.get("facts") or []:
        if isinstance(f, dict):
            upsert_fact(f)
    export_profile()

    log.info("压缩了 %d 条消息 → 片段#%s（重要度 %.2f）", len(rows), eid, salience)
    return eid


def upsert_fact(f):
    key = str(f.get("key") or "").strip()[:40]
    val = str(f.get("value") or "").strip()[:150]
    if not key or not val:
        return
    try:
        conf = max(0.0, min(1.0, float(f.get("confidence", 0.8))))
    except (TypeError, ValueError):
        conf = 0.8
    cat = str(f.get("category") or "其他")[:12]
    now = time.time()
    row = db.one("SELECT id,value FROM facts WHERE key=?", (key,))
    if row:
        if row["value"] != val:
            db.run(
                "INSERT INTO fact_history(ts,key,old_value,new_value,reason) VALUES(?,?,?,?,?)",
                (now, key, row["value"], val, "对话中更新"),
            )
            db.run(
                "UPDATE facts SET value=?,category=?,confidence=?,salience=MIN(1.0,salience+0.1),ts_updated=? WHERE id=?",
                (val, cat, conf, now, row["id"]),
            )
    else:
        db.run(
            "INSERT INTO facts(key,value,category,confidence,salience,ts_created,ts_updated,last_used) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (key, val, cat, conf, 0.6, now, now, now),
        )


# ---------------------------------------------------------------- 检索
def recall(chat_id, query, token_budget):
    """返回注入用的文本块 + 历史。只在需要时才拿出细节。"""
    facts_block = _pick_facts(query, int(token_budget * 0.5))
    ep_block = _pick_episodes(query, int(token_budget * 0.5))
    return {"facts": facts_block, "episodes": ep_block}


def _pick_facts(query, budget):
    rows = db.q("SELECT * FROM facts ORDER BY salience DESC, ts_updated DESC")
    if not rows:
        return ""
    qb = _bigrams(query)
    now = time.time()
    scored = []
    for r in rows:
        hit = 1.0 if qb & _bigrams(r["key"] + r["value"]) else 0.0
        score = r["salience"] * 0.5 + hit * 0.5 + r["confidence"] * 0.1
        # 到了复习期的记忆，主动往前排一点（间隔重复）
        try:
            if r["due_at"] and float(r["due_at"]) <= now:
                score += 0.12
        except (IndexError, KeyError, TypeError):
            pass
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])

    lines, used = [], 0
    for score, r in scored:
        if score < 0.25 and lines:
            break
        line = f"- {r['key']}：{r['value']}"
        t = est_tokens(line)
        if used + t > budget:
            break
        used += t
        lines.append(line)
        _review(r, now)
    return "\n".join(lines)


def _review(r, now):
    """SM-2 简版：用上一次就拉长下次复习的间隔，用得越勤越不容易忘。"""
    try:
        ease = float(r["ease"] or 2.5)
        interval = float(r["interval"] or 1)
    except (IndexError, KeyError, TypeError):
        ease, interval = 2.5, 1.0
    interval = min(120.0, max(1.0, interval * ease))
    ease = min(3.2, ease + 0.02)
    db.run(
        "UPDATE facts SET last_used=?, interval=?, ease=?, due_at=? WHERE id=?",
        (now, interval, ease, now + interval * 86400, r["id"]),
    )


def _pick_episodes(query, budget):
    rows = db.q("SELECT * FROM episodes WHERE dormant=0 ORDER BY id DESC LIMIT 400")
    if not rows:
        return ""
    qb = _bigrams(query)
    now = time.time()
    scored = []
    best_overlap = 0.0
    for r in rows:
        text = (r["summary"] or "") + " " + (r["topics"] or "") + " " + (r["tone"] or "")
        overlap = len(qb & _bigrams(text)) / max(1, len(qb))
        best_overlap = max(best_overlap, overlap)
        age_days = (now - (r["ts_end"] or now)) / 86400
        recency = math.exp(-age_days / 60)
        score = overlap * 0.62 + r["salience"] * 0.23 + recency * 0.15
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])

    # 证据门：一条都对不上，就别硬塞回忆，让模型老实说想不起来
    if qb and best_overlap < 0.04:
        return ""

    picked, used = [], 0
    for score, r in scored:
        if (not qb or score < 0.1) and picked:
            break
        if not qb and len(picked) >= 2:
            break
        if qb and score < 0.06:
            break
        line = f"- （{_ago(r['ts_end'] or now)}）{r['summary']}"
        t = est_tokens(line)
        if used + t > budget:
            break
        used += t
        picked.append(line)
        db.run(
            "UPDATE episodes SET recall_count=recall_count+1, last_recalled=?, salience=MIN(1.0,salience+0.04) WHERE id=?",
            (now, r["id"]),
        )
    return "\n".join(picked)


def _ago(ts):
    d = max(0, time.time() - ts)
    if d < 3600:
        return f"{int(d/60)}分钟前"
    if d < 86400:
        return f"{int(d/3600)}小时前"
    return f"{int(d/86400)}天前"


# ---------------------------------------------------------------- 遗忘
def decay():
    now = time.time()
    eps = db.q("SELECT id,salience,last_recalled,ts_end,dormant FROM episodes")
    slept = 0
    for r in eps:
        last = r["last_recalled"] or r["ts_end"] or now
        idle_days = (now - last) / 86400
        if idle_days < 3:
            continue
        s = r["salience"] * (0.985 ** idle_days)
        dormant = 1 if (s < 0.12 and not r["dormant"]) else r["dormant"]
        if dormant and not r["dormant"]:
            slept += 1
        db.run("UPDATE episodes SET salience=?, dormant=? WHERE id=?", (s, dormant, r["id"]))

    for r in db.q("SELECT id,salience,last_used,ts_updated FROM facts"):
        last = r["last_used"] or r["ts_updated"] or now
        idle_days = (now - last) / 86400
        if idle_days > 7:
            db.run(
                "UPDATE facts SET salience=? WHERE id=?",
                (max(0.05, r["salience"] * (0.995 ** idle_days)), r["id"]),
            )
    log.info("衰减完成，本次休眠 %d 段记忆", slept)
    return slept


# ---------------------------------------------------------------- 反思：让人设随对话变化
async def reflect(llm, persona, chat_id):
    from .prompts import REFLECT_SYSTEM

    eps = db.q(
        "SELECT summary,tone,ts_end FROM episodes WHERE chat_id=? ORDER BY id DESC LIMIT 8",
        (str(chat_id),),
    )
    if not eps:
        return []
    block = "\n".join(f"- {e['summary']}" for e in reversed(eps))
    obj = await llm.chat_json(
        [
            {"role": "system", "content": REFLECT_SYSTEM.format(name=persona.name)},
            {
                "role": "user",
                "content": f"当前状态：{json.dumps(persona.state, ensure_ascii=False)}\n\n最近的经历：\n{block}",
            },
        ],
        temperature=0.3,
    )
    return persona.apply_reflection(obj, reason="对话后反思")


# ---------------------------------------------------------------- 运维命令
def report(persona):
    facts = db.q("SELECT key,value FROM facts ORDER BY salience DESC LIMIT 25")
    eps = db.q(
        "SELECT id,summary,salience,recall_count,dormant FROM episodes ORDER BY id DESC LIMIT 5"
    )
    total_msg = db.one("SELECT COUNT(*) c FROM messages")["c"]
    total_ep = db.one("SELECT COUNT(*) c FROM episodes")["c"]
    lines = [f"【人设】{persona.summary()}", f"【规模】原文 {total_msg} 条 / 回忆片段 {total_ep} 段", "【记得的事实】"]
    lines += [f"- {r['key']}：{r['value']}" for r in facts] or ["（暂无）"]
    lines.append("【最近片段】")
    lines += [
        f"- #{r['id']}（重要度{r['salience']:.2f}，被想起{r['recall_count']}次{'' if not r['dormant'] else '，已休眠'}）{r['summary']}"
        for r in eps
    ] or ["（暂无）"]
    return "\n".join(lines)


def forget_fact(key):
    row = db.one("SELECT value FROM facts WHERE key=?", (key,))
    if not row:
        return f"没有找到关于「{key}」的记忆"
    db.run("DELETE FROM facts WHERE key=?", (key,))
    db.run(
        "INSERT INTO fact_history(ts,key,old_value,new_value,reason) VALUES(?,?,?,?,?)",
        (time.time(), key, row["value"], "", "手动遗忘"),
    )
    export_profile()
    return f"已遗忘：{key}（原值：{row['value']}）"


def forget_episode(eid):
    row = db.one("SELECT summary FROM episodes WHERE id=?", (eid,))
    if not row:
        return f"没有 #{eid} 这段记忆"
    db.run("UPDATE episodes SET dormant=1, salience=0 WHERE id=?", (eid,))
    return f"已让 #{eid} 休眠：{row['summary'][:30]}…"
