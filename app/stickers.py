"""大肥鱼表情包：知识库检索 + 按需下载 + 发出去。

知识库来自 dsh-nekowhale-stickers（322 张，逐张标注了情绪、标签、使用场景），
图从 kiriyuki.com 拉，第一次用到哪张才下载哪张，之后走本地缓存。
"""
import json
import logging
import pathlib
import re
import random
from urllib.parse import quote

import aiohttp

log = logging.getLogger("stickers")

SITE = "https://kiriyuki.com"
PATH = None
_cache = {"mtime": 0, "items": []}
EMOTIONS = [
    "开心", "大笑", "喜欢", "害羞", "得意", "兴奋", "惊讶", "疑惑", "思考",
    "无语", "难过", "哭", "生气", "害怕", "疲惫", "赞同", "拒绝", "打招呼",
    "感谢", "道歉", "卖萌", "搞怪", "嘲讽", "安慰", "其他",
]


def set_path(p):
    global PATH
    PATH = pathlib.Path(p) if p else None


def load():
    if not PATH or not PATH.exists():
        return []
    try:
        mtime = PATH.stat().st_mtime
        if mtime != _cache["mtime"]:
            data = json.loads(PATH.read_text(encoding="utf-8"))
            items = []
            for s in data.get("stickers") or []:
                sid = str(s.get("id") or "")
                if "/" not in sid:
                    continue
                pack, stem = sid.split("/", 1)
                code = stem.split("_", 1)[0]
                items.append(
                    {
                        "id": sid,
                        "pack": pack,
                        "stem": stem,
                        "code": code,
                        "name": stem.split("_", 1)[1] if "_" in stem else stem,
                        "file": f"{stem}.gif",
                        "hash": str(s.get("hash") or ""),
                        "emotion": str(s.get("emotion") or ""),
                        "tags": [str(t) for t in (s.get("tags") or [])],
                        "use": str(s.get("use") or ""),
                        "desc": str(s.get("desc") or ""),
                    }
                )
            _cache["items"] = items
            _cache["mtime"] = mtime
            log.info("表情包知识库加载：%d 张", len(items))
    except Exception as e:  # noqa: BLE001
        log.warning("读表情包知识库失败：%s", e)
    return _cache["items"]


def _score(q, it):
    if q in (it["name"], it["stem"], it["code"], it["id"]):
        return 100
    s = 0
    if q in it["name"]:
        s += 12
    if q == it["emotion"]:
        s += 10
    elif q in it["emotion"]:
        s += 6
    for t in it["tags"]:
        if q == t:
            s += 8
        elif q in t or t in q:
            s += 3
    if q in it["use"]:
        s += 3
    if q in it["desc"]:
        s += 1
    return s


def pick(query, exclude=None):
    if isinstance(exclude, str):
        exclude = {exclude} if exclude else set()
    ex = set(exclude or ())
    all_items = load()
    items = [x for x in all_items if x["id"] not in ex]
    if not items:
        items = all_items
    if not items:
        return None
    q = (query or "").strip()
    if not q or q in ("随机", "随便", "来一个", "表情", "random"):
        return random.choice(items)
    # 名字/编号对上了就直接给
    for it in items:
        if q in (it["name"], it["stem"], it["code"], it["id"]):
            return it
    # 要的就是这张，但它刚发过：同情绪的换一张
    for it in all_items:
        if q in (it["name"], it["stem"], it["code"], it["id"]):
            q = it["emotion"] or (it["tags"][0] if it["tags"] else "随机")
            break
    # 情绪词：同类里随便挑一张，别老是同一张
    if q in EMOTIONS:
        same = [x for x in items if x["emotion"] == q]
        if same:
            return random.choice(same)
    scored = [(it, _score(q, it)) for it in items]
    best = max(s for _, s in scored)
    if best <= 0:
        return None
    pool = [it for it, s in scored if s >= max(3, best - 3)]
    return random.choice(pool)


_HINTS = [
    ("大笑", r"哈哈|笑死|绷不住|乐死|233|ww+"),
    ("安慰", r"累了|好累|累死|烦死|难受|难过|哭了|呜呜|加班|寄了|顶不住"),
    ("无语", r"无语|逆天|离谱|难绷|抽象|不是？|服了|6+"),
    ("疑惑", r"\?|？|真的假的|啊？|啥|为什么|怎么会|是吗"),
    ("开心", r"好耶|可以|爽|太好了|不错|牛|稳"),
    ("得意", r"厉害|强|当然|那必须|小意思|这不是"),
    ("喜欢", r"喜欢|爱你|想你|亲亲|抱抱|贴贴"),
    ("害羞", r"害羞|不好意思|脸红|别夸"),
    ("兴奋", r"冲|来了|走起|开香槟|起飞"),
    ("疲惫", r"困|想睡|睡了|没电|摆烂|躺平"),
    ("赞同", r"确实|同意|有道理|对对对|[+]1|正解"),
    ("拒绝", r"不行|不要|算了|拒绝|免了"),
    ("打招呼", r"早上好|早呀|晚安|嗨|在吗|你好"),
    ("感谢", r"谢谢|感谢|多谢|谢了"),
    ("道歉", r"抱歉|对不起|不好意思|我的锅"),
    ("生气", r"生气|气死|滚|烦不烦"),
    ("嘲讽", r"就这|菜|笑你|看不起"),
    ("惊讶", r"卧槽|我草|震惊|真的吗|离谱"),
    ("搞怪", r"整活|乐子|抽象|整一个"),
]


def guess(text):
    """按上下文猜一个情绪的图，猜不出就开心。"""
    text = text or ""
    scores = []
    for emo, pat in _HINTS:
        n = len(re.findall(pat, text))
        if n:
            scores.append((n, emo))
    if not scores:
        return random.choice(["开心", "搞怪", "卖萌"])
    top = max(n for n, _ in scores)
    return random.choice([e for n, e in scores if n == top])


def url(it):
    base = f"{SITE}/media/gif/{quote(it['pack'])}/{quote(it['file'])}"
    return f"{base}?h={it['hash']}" if it["hash"] else base


async def fetch(it, cache_dir, session=None):
    """取 GIF 字节；本地有就用本地的。"""
    cache_dir = pathlib.Path(cache_dir)
    path = cache_dir / it["pack"] / it["file"]
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    own = session is None
    if own:
        session = aiohttp.ClientSession()
    try:
        async with session.get(url(it), timeout=aiohttp.ClientTimeout(total=25)) as r:
            if r.status != 200:
                log.warning("下载表情失败 %s：HTTP %s", it["id"], r.status)
                return None
            data = await r.read()
        if len(data) > 6 * 1024 * 1024:
            log.warning("表情太大，跳过：%s", it["id"])
            return None
        path.write_bytes(data)
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("下载表情出错 %s：%s", it["id"], e)
        return None
    finally:
        if own:
            await session.close()
