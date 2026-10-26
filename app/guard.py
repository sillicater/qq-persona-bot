"""穿帮审查：说话前对一遍现实，别把上午当半夜。

这是一层确定性的检查，不靠模型自觉：
  白天说"睡不着/这么晚/晚安"   → 穿帮
  非早上说"早安/早上好"         → 穿帮
  明天是周三却说"明天周二"       → 穿帮
对方自己说过的话不算（他在复述，可以跟着说）。
"""
import re

NIGHT = re.compile(r"(睡不着|不想睡|还没睡|睡意|失眠|熬夜|这么晚|大半夜|半夜|该睡了|早点睡|晚安|凌晨|夜里|做梦|梦到)")
MORNING = re.compile(r"(早安|早上好|早呀|起床了|早饭吃|晨跑)")
WEEKDAY_CLAIM = re.compile(r"(今天|明天|后天)(?:是)?(?:周|星期)([一二三四五六日天])")

_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _weekday_name(i):
    return "一二三四五六日"[i % 7]


def check(reply, user_text="", now=None):
    """返回穿帮说明；没问题返回空字符串。"""
    import time

    now = now or time.localtime()
    h = now.tm_hour
    text = reply or ""
    said = user_text or ""
    issues = []

    m = NIGHT.search(text)
    if m and 6 <= h < 18 and not NIGHT.search(said):
        issues.append(f"现在是{h}点的白天，别用「{m.group(1)}」这种深夜说法")

    m = MORNING.search(text)
    if m and not (5 <= h < 11) and not MORNING.search(said):
        issues.append(f"现在不是早上（{h}点），别用「{m.group(1)}」")

    for m in WEEKDAY_CLAIM.finditer(text):
        word, day = m.group(1), m.group(2)
        offset = {"今天": 0, "明天": 1, "后天": 2}[word]
        real = _CN[day]
        actual = (now.tm_wday + offset) % 7
        if real != actual and m.group(0) not in said:
            issues.append(f"{word}是周{_weekday_name(actual)}，不是周{day}")

    return "；".join(issues)
