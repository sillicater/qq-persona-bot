import os
from dataclasses import dataclass
from pathlib import Path


def _s(k, d=""):
    v = os.getenv(k)
    return d if v is None or v.strip() == "" else v.strip()


def _f(k, d):
    try:
        return float(_s(k, str(d)))
    except ValueError:
        return d


def _i(k, d):
    try:
        return int(_s(k, str(d)))
    except ValueError:
        return d


def _b(k, d):
    return _s(k, "1" if d else "0").lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = 1.1

    onebot_url: str = "ws://napcat:3001"
    onebot_token: str = ""
    self_qq: str = ""
    owner_qq: tuple = ()
    allow_groups: bool = False

    data_dir: Path = Path("/data")
    persona_path: Path = Path("/app/persona/persona.yaml")

    history_limit: int = 24          # 最近多少条原文进上下文
    history_tokens: int = 1200       # 最近原文的 token 上限
    memory_tokens: int = 1500        # 长期记忆注入的 token 上限
    episode_threshold: int = 12      # 攒够多少条就压缩成一段回忆
    reflect_every: int = 4           # 每几段回忆做一次人格状态更新
    decay_every_h: int = 12          # 每隔几小时做一次遗忘衰减

    proactive: bool = True
    proactive_per_hour: float = 0.3  # 每小时主动发消息的期望概率
    proactive_min_gap_min: int = 45  # 两次主动消息至少隔多久
    proactive_daily_max: int = 6     # 每天最多主动发几条
    wake_slots: str = "08:30-09:30,12:00-13:00,15:00-16:00,18:00-19:00,21:00-23:00"
    quiet_start: int = 1             # 静默时段开始（这个点之后不主动发）
    quiet_end: int = 8               # 静默时段结束（这个点之前不主动发）

    fast_reply_chance: float = 0.35  # 有多大概率秒回
    skip_enabled: bool = True        # 允许偶尔不回消息
    busy_hours: str = "9-12,14-17"   # 这些时段算在忙，回复会变慢

    schedule_url: str = ""           # 任务中心地址，比如 http://192.168.3.3:8787
    schedule_token: str = ""
    reminders: bool = True           # 允许"提醒我…"

    follow_up: bool = True           # 回完之后允许再补一句（联想续聊）
    follow_up_rate: float = 1.0      # 补话概率的倍数，1.0 是默认

    debounce: float = 0.9            # 收到连续消息时等多久合并
    night_slowdown: bool = True      # 深夜回复变慢

    away: bool = True                # 允许她聊着聊着说"我去洗个澡"然后真的消失一会儿
    away_chance: float = 0.12        # 一轮聊完之后离开的概率
    sleep_note: bool = True          # 半夜被吵醒时迷迷糊糊回一句就接着睡
    mood_shift: bool = True          # 被对方一句话带动心情
    life: bool = True                # 生成"她自己的生活"（真人人设用；AI 人设关掉）
    sticker_chance: float = 0.55     # 模型没主动发表情时，按这个概率补一张

    @classmethod
    def load(cls):
        owner = tuple(x.strip() for x in _s("OWNER_QQ").split(",") if x.strip())
        return cls(
            api_key=_s("DEEPSEEK_API_KEY"),
            base_url=_s("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=_s("MODEL", "deepseek-chat"),
            temperature=_f("TEMPERATURE", 1.1),
            onebot_url=_s("ONEBOT_WS_URL", "ws://napcat:3001"),
            onebot_token=_s("ONEBOT_TOKEN"),
            self_qq=_s("SELF_QQ"),
            owner_qq=owner,
            allow_groups=_b("ALLOW_GROUPS", False),
            data_dir=Path(_s("DATA_DIR", "/data")),
            persona_path=Path(_s("PERSONA_PATH", "/app/persona/persona.yaml")),
            history_limit=_i("HISTORY_LIMIT", 24),
            history_tokens=_i("HISTORY_TOKENS", 1200),
            memory_tokens=_i("MEMORY_TOKENS", 1500),
            episode_threshold=_i("EPISODE_THRESHOLD", 12),
            reflect_every=_i("REFLECT_EVERY", 4),
            decay_every_h=_i("DECAY_EVERY_H", 12),
            proactive=_b("PROACTIVE", True),
            proactive_per_hour=_f("PROACTIVE_PER_HOUR", 0.3),
            proactive_min_gap_min=_i("PROACTIVE_MIN_GAP_MIN", 45),
            proactive_daily_max=_i("PROACTIVE_DAILY_MAX", 6),
            wake_slots=_s(
                "WAKE_SLOTS", "08:30-09:30,12:00-13:00,15:00-16:00,18:00-19:00,21:00-23:00"
            ),
            quiet_start=_i("QUIET_START", 1),
            quiet_end=_i("QUIET_END", 8),
            fast_reply_chance=_f("FAST_REPLY_CHANCE", 0.35),
            skip_enabled=_b("SKIP_ENABLED", True),
            busy_hours=_s("BUSY_HOURS", "9-12,14-17"),
            schedule_url=_s("SCHEDULE_URL"),
            schedule_token=_s("SCHEDULE_TOKEN"),
            reminders=_b("REMINDERS", True),
            follow_up=_b("FOLLOW_UP", True),
            follow_up_rate=_f("FOLLOW_UP_RATE", 1.0),
            debounce=_f("DEBOUNCE", 0.9),
            night_slowdown=_b("NIGHT_SLOWDOWN", True),
            sticker_chance=_f("STICKER_CHANCE", 0.55),
            away=_b("AWAY", True),
            away_chance=_f("AWAY_CHANCE", 0.12),
            sleep_note=_b("SLEEP_NOTE", True),
            mood_shift=_b("MOOD_SHIFT", True),
            life=_b("LIFE", True),
        )

    def busy_ranges(self):
        out = []
        for part in (self.busy_hours or "").split(","):
            part = part.strip()
            if "-" not in part:
                continue
            a, _, b = part.partition("-")
            try:
                out.append((int(a), int(b)))
            except ValueError:
                continue
        return out
