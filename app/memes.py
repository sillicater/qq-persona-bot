"""网络热梗库：让她知道最近网上在说什么，但一次只看一小把。

来源两份：
  persona/memes.yaml —— 我整理的当期热梗（只读）
  memes 表          —— 聊天里学到的、你手动加的
"""
import logging
import pathlib
import random
import time

import yaml

from . import db

log = logging.getLogger("memes")

PATH = None
_cache = {"mtime": 0, "items": []}


def set_path(p):
    global PATH
    PATH = pathlib.Path(p) if p else None


def _from_file():
    if not PATH or not PATH.exists():
        return []
    try:
        mtime = PATH.stat().st_mtime
        if mtime != _cache["mtime"]:
            data = yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}
            _cache["items"] = [x for x in (data.get("memes") or []) if x.get("term")]
            _cache["mtime"] = mtime
    except Exception as e:  # noqa: BLE001
        log.warning("读梗库失败：%s", e)
    return list(_cache["items"])


def _from_db():
    rows = db.q("SELECT term,meaning,example,ts FROM memes ORDER BY id DESC LIMIT 80")
    return [
        {
            "term": r["term"],
            "meaning": r["meaning"],
            "example": r["example"],
            "since": time.strftime("%Y-%m-%d", time.localtime(r["ts"] or time.time())),
        }
        for r in rows
    ]


def collect():
    return _from_file() + _from_db()


def pick(n=10):
    """随机挑一批，新的优先，但不会每次都一样。"""
    items = collect()
    if not items:
        return []
    items.sort(key=lambda x: str(x.get("since") or ""), reverse=True)
    pool = items[: max(n * 3, n)]
    return random.sample(pool, min(n, len(pool)))


def render(n=10):
    picked = pick(n)
    if not picked:
        return ""
    lines = []
    for x in picked:
        meaning = str(x.get("meaning") or "").strip()
        example = str(x.get("example") or "").strip()
        line = f"- {x['term']}：{meaning}"
        if example:
            line += f"（比如：{example}）"
        lines.append(line)
    return "\n".join(lines)


def add(term, meaning, example=""):
    term = str(term or "").strip()[:20]
    meaning = str(meaning or "").strip()[:100]
    if not term or not meaning:
        return None
    if db.one("SELECT id FROM memes WHERE term=?", (term,)):
        db.run("UPDATE memes SET meaning=?, example=? WHERE term=?", (meaning, example[:80], term))
    else:
        db.run(
            "INSERT INTO memes(term,meaning,example,ts) VALUES(?,?,?,?)",
            (term, meaning, example[:80], time.time()),
        )
    return term


def listing(limit=15):
    items = collect()
    if not items:
        return "梗库是空的"
    items.sort(key=lambda x: str(x.get("since") or ""), reverse=True)
    terms = [x["term"] for x in items[:limit]]
    return f"现在有 {len(items)} 条，最新的：" + "、".join(terms)
