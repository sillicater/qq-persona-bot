"""行为控制：决定「回多快」「想多深」「回不回」「什么时候主动找你」。

目标不是最快，而是像人：随手的话秒回，复杂的问题先想一会儿，
忙的时候晚点回，困的时候第二天才看到，心情差的时候懒得回。
"""
import random
import re

_DEEP_HINT = re.compile(
    r"(为什么|怎么|如何|能不能|可不可以|帮我|解释|分析|原理|区别|对比|配置|参数|装机|"
    r"代码|报错|调试|建模|公式|计算|推荐|方案|步骤|思路|怎么写|怎么做|选哪个)"
)
_EMO_HINT = re.compile(r"(难过|难受|烦|累|崩溃|开心|好爽|生气|委屈|焦虑|害怕|孤单|压力|分手|挂了)")
_QUESTION = re.compile(r"[?？]")

_BAD_MOOD_WORDS = ("烦", "累", "难过", "生气", "委屈", "低气压", "不想说话", "没意思")


def depth_of(text):
    """0 = 随口一句；1 = 需要接住；2 = 得认真答。"""
    t = (text or "").strip()
    d = 0
    if len(t) >= 40:
        d += 1
    if _QUESTION.search(t):
        d += 1
    if _DEEP_HINT.search(t):
        d += 1
    if _EMO_HINT.search(t):
        d = max(d, 1)
    return min(d, 2)


def bad_mood(mood_text):
    return any(w in (mood_text or "") for w in _BAD_MOOD_WORDS)


def is_night(hour, quiet_start, quiet_end):
    if quiet_start < quiet_end:
        return quiet_start <= hour < quiet_end
    return hour >= quiet_start or hour < quiet_end


def momentum_from_gap(gap):
    """刚聊完就接着聊，回得快；隔了一阵才看到，回得慢。"""
    if gap < 45:
        return 1.0
    if gap < 180:
        return 0.6
    if gap < 900:
        return 0.3
    return 0.0


def read_delay(text, depth, night, fast_chance=0.35, momentum=0.0):
    """AI 的"反应时间"：基本立刻接话，复杂问题才多想一会儿。"""
    n = len(text or "")
    if random.random() < 0.75:
        d = random.uniform(0.15, 0.6) + n * 0.006
    else:
        d = random.uniform(0.5, 1.2) * (1.0 - 0.3 * momentum)
        d += depth * random.uniform(0.5, 1.5)
    if night:
        d *= 1.3
    return min(d, 12.0)


def typing_delay(chunk, depth=0, night=False):
    """打这一条要花的时间：AI 打字快，但也留一点"在敲"的实感。"""
    n = len(chunk or "")
    d = 0.15 + n * random.uniform(0.035, 0.055) + depth * 0.08
    if night:
        d *= 1.2
    return min(d, 2.0)


def decide_reply(text, hour, mood_text, busy, night):
    """返回 (动作, 延迟秒数, 提示)。
    动作：now 立刻回 / later 过一会儿再回 / skip 这条不回。
    """
    d = depth_of(text)
    mood_bad = bad_mood(mood_text)
    r = random.random()

    # 半夜被吵醒：除非是正事，否则会隔一阵才迷迷糊糊回
    if night and d < 2 and r < 0.55:
        return "later", random.uniform(120, 480), "你刚被消息吵醒，回得有点迷糊，先说一句才看到"

    # 在忙（上课、上班、出门）
    if busy and r < 0.45:
        return "later", random.uniform(180, 900), "你刚才在忙没顾上看手机，先提一句才看到"

    # 心情不好 + 对方只发了很短一句：懒得回
    if mood_bad and len(text.strip()) <= 8 and r < 0.25:
        return "skip", 0, ""

    # 就一个"嗯""哦""在吗"，偶尔直接不理
    if len(text.strip()) <= 4 and not _QUESTION.search(text) and r < 0.08:
        return "skip", 0, ""

    return "now", 0, ""


DEPTH_HINT = {
    0: "随口聊，一两句就行，也可以顺手接一句自己的反应或反问。",
    1: "正常聊，有自己的态度。",
    2: "这条对方在认真问/认真说，好好答，需要的话分步骤讲清楚。",
}



def follow_up_delay(depth):
    """补话的间隔，像突然想起来一样。"""
    if depth >= 2:
        return random.uniform(5, 14)
    return random.uniform(2.5, 8)


def burst_extra_delay(count, debounce):
    """对方连着发了好几条：等一下，但别让人等太久。"""
    if count >= 3 or (count == 2 and random.random() < 0.25):
        return random.uniform(1.2, 3.0)
    return 0


# ---------------------------------------------------------------- 情绪、离开、久别
_MOOD_RULES = [
    (re.compile(r"(难过|难受|烦躁|烦死|崩溃|委屈|想哭|焦虑|压力大|好累|累死|撑不住)"), "有点担心他"),
    (re.compile(r"(哈哈|笑死|好耶|太好了|开心|好爽|牛啊|厉害)"), "聊得挺开心"),
    (re.compile(r"(晚安|睡啦|困了|好困)"), "有点困了"),
    (re.compile(r"(无聊|在干嘛|想你|陪我)"), "有点黏他"),
]


def mood_after(current, text):
    """对方这句话会把她的心情带到哪；没变化返回 None。"""
    t = text or ""
    for pat, mood in _MOOD_RULES:
        if pat.search(t):
            return None if mood == current else mood
    return None


def gap_hint(seconds):
    """隔了很久没聊，提示她自然提一句。"""
    hours = seconds / 3600
    if hours < 24:
        return f"你们有 {hours:.0f} 个小时没聊了，可以顺口提一句，别太刻意"
    return f"你们有 {hours / 24:.0f} 天没聊了，可以顺口提一句，别太刻意"


AWAY_LINES = [
    "我去洗个澡 等下说",
    "先吃个饭 回来找你",
    "出门一趟 晚点回你",
    "室友叫我 等我一下",
    "去趟超市 马上回来",
]
RETURN_LINES = ["回来了", "我回来啦", "刚忙完 终于能歇会儿了"]
SLEEP_LINES = ["…我睡了呀", "这么晚了 我明天还有早八呢", "…几点了 我都睡着了"]


def away_line(doing=""):
    """结合她这会儿在干嘛，挑一句自然的离开理由。"""
    d = doing or ""
    if "食堂" in d or "吃" in d:
        return "我先去吃饭了 等下说"
    if "图书馆" in d:
        return "我在图书馆 晚点回你"
    if "教室" in d or "上课" in d:
        return "在上课 下课再说"
    if "宿舍" in d:
        return random.choice(["室友叫我 等我一下", "我先去洗澡了 等下说"])
    return random.choice(AWAY_LINES)


def busy_at(hour, ranges):
    """ranges 形如 [(9, 12), (14, 17)]，左闭右开。"""
    return any(a <= hour < b for a, b in ranges)


def parse_slots(spec):
    """'08:30-09:30,12:00-13:00' -> [(510, 570), (720, 780)] 分钟。"""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if "-" not in part:
            continue
        a, b = part.split("-", 1)
        try:
            ah, am = [int(x) for x in a.strip().split(":")]
            bh, bm = [int(x) for x in b.strip().split(":")]
        except ValueError:
            continue
        out.append((ah * 60 + am, bh * 60 + bm))
    return out


SLOT_HINT = {
    0: "现在是早上，可以说句早，问他今天忙不忙",
    1: "现在是中午，可以问吃没吃、在干嘛",
    2: "现在是晚上，可以说说今天累不累、在做什么",
    3: "他好一阵子没出现了，你有点想他，但别太黏，问一句最近是不是很忙",
}
