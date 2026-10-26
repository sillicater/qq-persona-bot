import copy
import json
import logging
import time

import yaml

from . import db

log = logging.getLogger("persona")

MUTABLE_KEYS = ("心情", "关系熟悉度", "最近在意的事", "正在养成的习惯", "自我认知", "说话习惯微调")

_FAMILIARITY_DESC = [
    (0.15, "还没熟，说话客气但有距离"),
    (0.35, "认识不久，会聊但还端着"),
    (0.6, "算熟了，会开玩笑、会吐槽"),
    (0.8, "很熟，说话随便，会损人"),
    (1.01, "非常熟，什么都敢说，有默契"),
]


def _as_lines(v):
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


class Persona:
    def __init__(self, path):
        self.path = path
        self._mtime = 0
        self.reload()
        self._load_state()

    # ---------- 加载 ----------
    def reload(self):
        with open(self.path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        self.name = str(raw.get("name") or "我")
        self.age = raw.get("age")
        self.gender = str(raw.get("gender") or "")
        self.core = raw.get("core") or {}
        self.default_state = raw.get("state") or {}
        self.proactive_topics = (raw.get("proactive") or {}).get("topics") or []
        self._mtime = time.time()
        log.info("人设已加载：%s", self.name)

    def maybe_reload(self):
        try:
            import os

            if os.path.getmtime(self.path) > self._mtime:
                self.reload()
                return True
        except OSError:
            pass
        return False

    # ---------- 状态 ----------
    def _load_state(self):
        row = db.one("SELECT state_json FROM persona_state WHERE id=1")
        self.state = json.loads(row["state_json"]) if row else copy.deepcopy(self.default_state)

    def _save_state(self):
        db.run(
            "INSERT INTO persona_state(id,state_json,updated_ts) VALUES(1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json, updated_ts=excluded.updated_ts",
            (json.dumps(self.state, ensure_ascii=False), time.time()),
        )

    def familiarity_desc(self):
        try:
            v = float(self.state.get("关系熟悉度", 0.25))
        except (TypeError, ValueError):
            v = 0.25
        for th, d in _FAMILIARITY_DESC:
            if v < th:
                return d
        return _FAMILIARITY_DESC[-1][1]

    # ---------- 渲染成提示词 ----------
    def core_block(self):
        lines = [f"【你是谁】"]
        head = f"你叫{self.name}"
        if self.age:
            head += f"，{self.age}岁"
        if self.gender:
            head += f"，{self.gender}"
        lines.append(head + "。")
        for k, v in self.core.items():
            if isinstance(v, list):
                lines.append(f"- {k}：" + "；".join(str(x) for x in v))
            else:
                lines.append(f"- {k}：" + str(v))
        return "\n".join(lines)

    def state_block(self):
        lines = ["【你现在的状态｜会随经历慢慢变化】"]
        lines.append(f"- 心情：{self.state.get('心情', '平静')}")
        lines.append(f"- 和对方的关系：{self.familiarity_desc()}")
        cares = self.state.get("最近在意的事") or []
        if cares:
            lines.append("- 最近在意的事：" + "、".join(str(x) for x in cares))
        habits = self.state.get("正在养成的习惯") or []
        if habits:
            lines.append("- 正在养成的习惯：" + "、".join(str(x) for x in habits))
        selfview = self.state.get("自我认知") or []
        if selfview:
            lines.append("【你最近对自己的认识｜会慢慢变】")
            lines += [f"- {x}" for x in selfview]
        quirks = self.state.get("说话习惯微调") or []
        if quirks:
            lines.append("- 你最近说话的微调：" + "、".join(str(x) for x in quirks))
        return "\n".join(lines)

    def summary(self):
        st = self.state
        return (
            f"{self.name}｜心情：{st.get('心情','平静')}｜关系：{st.get('关系熟悉度',0.25)}"
            f"（{self.familiarity_desc()}）｜在意：{'、'.join(st.get('最近在意的事') or []) or '无'}"
        )

    # ---------- 人设演化 ----------
    def apply_reflection(self, patch, reason="反思"):
        """只接受白名单字段，改动幅度受限，全部留档。"""
        if not isinstance(patch, dict):
            return []
        changes = {}

        if "心情" in patch:
            m = str(patch["心情"]).strip()[:12]
            if m and m != self.state.get("心情"):
                changes["心情"] = [self.state.get("心情"), m]
                self.state["心情"] = m

        if "关系熟悉度" in patch:
            try:
                new = float(patch["关系熟悉度"])
            except (TypeError, ValueError):
                new = None
            if new is not None:
                old = float(self.state.get("关系熟悉度", 0.25))
                new = max(0.0, min(1.0, new))
                # 单次最多漂移 0.06，避免一晚上从陌生人变生死之交
                new = max(old - 0.06, min(old + 0.06, new))
                if abs(new - old) > 0.005:
                    self.state["关系熟悉度"] = round(new, 3)
                    changes["关系熟悉度"] = [old, round(new, 3)]

        for key, cap, maxlen in (
            ("最近在意的事", 5, 16),
            ("正在养成的习惯", 4, 16),
            ("自我认知", 6, 28),
            ("说话习惯微调", 4, 20),
        ):
            if key in patch and isinstance(patch[key], list):
                items = []
                for x in patch[key]:
                    x = str(x).strip()[:maxlen]
                    if x and x not in items:
                        items.append(x)
                items = items[:cap]
                if items != (self.state.get(key) or []):
                    changes[key] = [self.state.get(key) or [], items]
                    self.state[key] = items

        if changes:
            self._save_state()
            db.run(
                "INSERT INTO persona_history(ts,diff,reason) VALUES(?,?,?)",
                (time.time(), json.dumps(changes, ensure_ascii=False), reason),
            )
            log.info("人设状态更新: %s", json.dumps(changes, ensure_ascii=False))
        return list(changes)

    def history(self, limit=5):
        return db.q(
            "SELECT id,ts,diff,reason FROM persona_history ORDER BY id DESC LIMIT ?", (limit,)
        )

    def rollback(self, hid):
        """把某一次人设改动退回去（审计用）。"""
        row = db.one("SELECT diff FROM persona_history WHERE id=?", (int(hid),))
        if not row:
            return f"没有 #{hid} 这条记录"
        try:
            diff = json.loads(row["diff"])
        except (TypeError, json.JSONDecodeError):
            return "这条记录的格式读不出来"
        old_vals = {}
        for k, pair in (diff or {}).items():
            if k in MUTABLE_KEYS and isinstance(pair, list) and len(pair) == 2:
                old_vals[k] = pair[0]
        if not old_vals:
            return "这条记录没有可回滚的字段"
        changes = {}
        for k, v in old_vals.items():
            changes[k] = [self.state.get(k), v]
            self.state[k] = v
        self._save_state()
        db.run(
            "INSERT INTO persona_history(ts,diff,reason) VALUES(?,?,?)",
            (time.time(), json.dumps(changes, ensure_ascii=False), f"回滚 #{hid}"),
        )
        return "已回滚 #{}：{}".format(hid, "、".join(old_vals))

    def soft_mood(self, mood):
        """聊天中被对方一句话带动的心情，幅度小、随时会过去。"""
        mood = str(mood or "").strip()[:12]
        if not mood or mood == self.state.get("心情"):
            return False
        old = self.state.get("心情")
        self.state["心情"] = mood
        self._save_state()
        db.run(
            "INSERT INTO persona_history(ts,diff,reason) VALUES(?,?,?)",
            (time.time(), json.dumps({"心情": [old, mood]}, ensure_ascii=False), "聊天中被带动"),
        )
        return True

    # ---------- 自我设定（手动或自动） ----------
    def add_self_note(self, text):
        return self._append_state("自我认知", text, 6)

    def add_style_note(self, text):
        return self._append_state("说话习惯微调", text, 4)

    def _append_state(self, key, text, cap):
        text = str(text).strip()[:60]
        if not text:
            return None
        old = list(self.state.get(key) or [])
        if text in old:
            return None
        items = (old + [text])[-cap:]
        self.state[key] = items
        self._save_state()
        db.run(
            "INSERT INTO persona_history(ts,diff,reason) VALUES(?,?,?)",
            (time.time(), json.dumps({key: [old, items]}, ensure_ascii=False), "手动设定"),
        )
        return text
