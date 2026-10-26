"""读取任务中心的课表和日程，让鲸鲸知道对方现在在干嘛、最近要做什么。"""
import datetime as dt
import re
import time

_cache = {"at": 0.0, "state": None}
_dates = []
_WEEKDAYS = "一二三四五六日"


def load_dates(path):
    """特殊日子：生日、纪念日、考试。MM-DD 表示每年，YYYY-MM-DD 表示一次。"""
    global _dates
    import pathlib

    p = pathlib.Path(path)
    if not p.exists():
        return
    try:
        import yaml

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        _dates = [d for d in data.get("dates") or [] if isinstance(d, dict)]
    except Exception:  # noqa: BLE001
        _dates = []


def _today_dates(now):
    out = []
    md = now.strftime("%m-%d")
    ymd = now.strftime("%Y-%m-%d")
    for d in _dates:
        v = str(d.get("date") or "").strip()
        if not v:
            continue
        if len(v) == 5 and v == md:
            out.append(d)
        elif v[:10] == ymd:
            out.append(d)
    return out


async def fetch(cfg, force=False):
    """带缓存地拉 /api/state。拉不到就返回上一次的，不打断对话。"""
    if not cfg.schedule_url:
        return None
    if not force and _cache["state"] and time.time() - _cache["at"] < 600:
        return _cache["state"]
    url = cfg.schedule_url.rstrip("/") + "/api/state"
    headers = {"X-Token": cfg.schedule_token} if cfg.schedule_token else {}
    try:
        import aiohttp

        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return _cache["state"]
                data = await r.json()
    except Exception:  # noqa: BLE001
        return _cache["state"]
    _cache.update(at=time.time(), state=data)
    return data


# ---------------------------------------------------------------- 课表
def _week_num(state, d):
    raw = state.get("semesterStart") or (state.get("term") or {}).get("start")
    if not raw:
        return None
    try:
        start = dt.date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None
    n = (d - start).days // 7 + 1
    return n if n > 0 else None


def _weeks_match(spec, week):
    if not spec or week is None:
        return True
    for part in str(spec).replace("周", "").split(","):
        part = part.strip()
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if m and int(m.group(1)) <= week <= int(m.group(2)):
            return True
        if part.isdigit() and int(part) == week:
            return True
    return False


def _weekday_of(state, d):
    ov = state.get("dayOverrides") or {}
    key = d.isoformat()
    if key in ov:
        try:
            return int(ov[key])
        except (TypeError, ValueError):
            pass
    return d.weekday()


def _is_holiday(state, d):
    return any(h.get("date") == d.isoformat() for h in state.get("holidays") or [])


def courses_today(state, now=None):
    now = now or dt.datetime.now()
    d = now.date()
    if _is_holiday(state, d):
        return []
    wd = _weekday_of(state, d)
    week = _week_num(state, d)
    out = []
    for c in state.get("courses") or []:
        if int(c.get("day", -1)) != wd:
            continue
        if not _weeks_match(c.get("weeks"), week):
            continue
        out.append(c)
    out.sort(key=lambda c: c.get("startTime") or "99:99")
    return out


def tasks_soon(state, days=7, now=None):
    now = now or dt.datetime.now()
    out = []
    for t in state.get("tasks") or []:
        if (t.get("status") or "") == "done":
            continue
        raw = t.get("deadline")
        if not raw:
            continue
        try:
            due = dt.datetime.fromisoformat(str(raw)[:16])
        except ValueError:
            continue
        delta = (due - now).total_seconds()
        if -86400 <= delta <= days * 86400:
            out.append((due, t))
    out.sort(key=lambda x: x[0])
    return out


def _hm(s):
    m = re.match(r"^(\d{1,2}):(\d{2})", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def current_course(state, now=None):
    now = now or dt.datetime.now()
    cur = now.hour * 60 + now.minute
    for c in courses_today(state, now):
        a, b = _hm(c.get("startTime")), _hm(c.get("endTime"))
        if not a or not b:
            continue
        if a[0] * 60 + a[1] <= cur < b[0] * 60 + b[1]:
            return c
    return None


def next_course(state, now=None):
    now = now or dt.datetime.now()
    cur = now.hour * 60 + now.minute
    for c in courses_today(state, now):
        a = _hm(c.get("startTime"))
        if a and a[0] * 60 + a[1] > cur:
            return c
    return None


# ---------------------------------------------------------------- 渲染进提示词
def render(state, now=None):
    if not state:
        return ""
    now = now or dt.datetime.now()
    d = now.date()
    lines = []
    week = _week_num(state, d)
    lines.append(f"今天是{now.month}月{now.day}日 周{_WEEKDAYS[d.weekday()]}" + (f"，第{week}周" if week else ""))
    for sp in _today_dates(now):
        lines.append(f"今天是「{sp.get('title','')}」，对他来说是个特别的日子。")
    if _is_holiday(state, d):
        lines.append("今天是假期，没课。")
    else:
        cs = courses_today(state, now)
        if cs:
            lines.append("他的课表：")
            for c in cs:
                lines.append(
                    f"- {c.get('startTime','')}-{c.get('endTime','')} {c.get('name','')}"
                    + (f" @{c['location']}" if c.get("location") else "")
                )
        else:
            lines.append("他今天没课。")

    cur = current_course(state, now)
    if cur:
        lines.append(f"他这会儿正在上「{cur.get('name','')}」（到 {cur.get('endTime','')}），大概率在课上。")
    else:
        nxt = next_course(state, now)
        if nxt:
            lines.append(f"他下一节课是「{nxt.get('name','')}」，{nxt.get('startTime','')} 开始。")

    ts = tasks_soon(state, 7, now)
    if ts:
        lines.append("他最近要做的事：")
        for due, t in ts[:6]:
            lines.append(f"- {due.strftime('%m-%d %H:%M')} {t.get('title','')}（{t.get('course','') or '无课程'}）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 提醒时间解析
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _cn2int(s):
    if s.isdigit():
        return int(s)
    return _CN_NUM.get(s)


def parse_when(text, now=None):
    """从「明天下午三点」「今晚 21:30」「26号 10点」这类话里解析出时间。"""
    now = now or dt.datetime.now()
    t = (text or "").strip()
    if not t:
        return None

    day = now.date()
    if "后天" in t:
        day = day + dt.timedelta(days=2)
    elif "明天" in t or "明早" in t or "明晚" in t:
        day = day + dt.timedelta(days=1)
    else:
        m = re.search(r"(\d{1,2})\s*[号日]", t)
        if m:
            try:
                day = day.replace(day=int(m.group(1)))
            except ValueError:
                pass

    m = re.search(r"(\d{1,2}|[一二两三四五六七八九十]{1,2})\s*[:：点時时]\s*(\d{1,2})?", t)
    if not m:
        return None
    hour = _cn2int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    if hour is None:
        return None

    if ("下午" in t or "晚上" in t or "傍晚" in t) and hour < 12:
        hour += 12
    if ("早上" in t or "上午" in t) and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None

    when = dt.datetime.combine(day, dt.time(hour, minute))
    if when <= now and "今天" not in t and day == now.date():
        when += dt.timedelta(days=1)
    return when


_WHEN_TOKENS = re.compile(
    r"(今天|明天|后天|今晚|明早|明晚|早上|上午|中午|下午|晚上|傍晚|\d{1,2}\s*[号日])?"
    r"\s*(\d{1,2}|[一二两三四五六七八九十]{1,2})\s*[:：点時时]\s*\d{0,2}\s*分?"
)


def strip_when(text):
    """去掉句子里的时间部分，剩下的是要提醒的内容。"""
    t = text or ""
    m = _WHEN_TOKENS.search(t)
    if not m:
        return t.strip()
    return t[m.end():].strip(" ，,。") or t.strip()
