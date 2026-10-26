"""把回复拆成"人打字发出来"的样子：分条、停顿、打字延迟。"""
import random
import re

_SENT = re.compile(r"(?<=[。！？!?…~～])")
_BULLET = re.compile(r"^\s*[-*•·]\s+", re.M)


def clean(text):
    t = text.strip()
    t = _BULLET.sub("", t)
    t = t.replace("**", "").replace("`", "").replace("#", "")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _strip_period(s):
    s = s.strip()
    while s and s[-1] in "。.":
        s = s[:-1].rstrip()
    return s


def _split_long(s, maxlen=36):
    if len(s) <= maxlen:
        return [s]
    parts, cur = [], ""
    for ch in s:
        cur += ch
        if len(cur) >= maxlen and ch in "，,、；;：: ":
            parts.append(cur.strip("，,、；;：: "))
            cur = ""
        elif len(cur) >= maxlen + 24:  # 一路没标点，硬切
            parts.append(cur.strip())
            cur = ""
    if cur.strip():
        parts.append(cur.strip("，,、；;：: "))
    return [p for p in parts if p] or [s]


def plan(text, max_bubbles=3):
    """返回要逐条发出的消息列表。"""
    t = clean(text)
    if not t:
        return []
    if len(t) > 180:  # 认真答题的时候，允许说得长一点、分得多一点
        max_bubbles = max(max_bubbles, 5)

    lines = [x.strip() for x in t.split("\n") if x.strip()]
    if 1 <= len(lines) <= max_bubbles and all(len(x) <= 60 for x in lines):
        chunks = lines
    else:
        joined = "".join(lines) if all(len(x) < 60 for x in lines) else "\n".join(lines)
        chunks = []
        for seg in _SENT.split(joined):
            seg = seg.strip()
            if not seg:
                continue
            for part in _split_long(seg):
                chunks.append(part)

    # 太短的碎片并进上一条（但"在""嗯"这种独立的一句就让它单独发）
    merged = []
    for c in chunks:
        if merged and len(c) <= 2 and len(merged[-1]) >= 6:
            merged[-1] = merged[-1] + " " + c
        else:
            merged.append(c)

    # 超量就合并尾部
    if len(merged) > max_bubbles:
        head = merged[: max_bubbles - 1]
        head.append(" ".join(merged[max_bubbles - 1 :]))
        merged = head

    # 只回了一句、但这话挺长：拆成两条发，别一条长消息砸过去
    if len(merged) == 1 and max_bubbles >= 2 and len(merged[0]) > 16:
        if random.random() < 0.7:
            pieces = _split_mid(merged[0])
            if pieces:
                merged = pieces

    out = []
    for c in merged:
        c = _strip_period(c)
        if c:
            out.append(c)
    return out


def read_delay(user_len, night=False):
    """看到消息到开始打字之间的停顿。"""
    d = min(0.9 + user_len * 0.022, 4.5) * random.uniform(0.6, 1.8)
    if night:
        d *= 2.2
    return d


def typing_delay(chunk, night=False):
    """打这一条要花的时间。"""
    d = min(0.5 + len(chunk) * 0.07, 5.2) * random.uniform(0.7, 1.4)
    if night:
        d *= 1.4
    return d


def gap_delay(chunk):
    """两条之间的间隔：发完一句马上接着补下一句。"""
    return random.uniform(0.25, 0.9) + len(chunk) * 0.008


def _split_mid(s):
    """一句话挺长又没分行，从中间的逗号/空格断开，像先打半句再补。"""
    mid = len(s) // 2
    best = None
    for i, ch in enumerate(s):
        if ch in "，, 、~ ":
            if best is None or abs(i - mid) < abs(best - mid):
                best = i
    if best is None or best <= 1:
        return None
    a = s[:best].strip(" ，,、~")
    b = s[best + 1 :].strip()
    if len(a) >= 3 and len(b) >= 4:
        return [a, b]
    return None
