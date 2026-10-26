import asyncio
import base64
import datetime as dt
import json
import logging
import random
import re
import time
import uuid

import aiohttp

from . import db, guard, life, memes, memory, pacing, schedule, stickers
from .humanize import gap_delay, plan
from .prompts import FOLLOW_UP_HINT, build_proactive_system, build_system

log = logging.getLogger("bot")


# ----------------------------------------------------------------- OneBot 客户端
class OneBot:
    def __init__(self, url, token, on_event):
        self.url = url
        self.token = token
        self.on_event = on_event
        self.ws = None
        self.pending = {}
        self.last_close = None

    async def run(self):
        backoff = 2
        while True:
            started = time.time()
            try:
                await self._once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("OneBot 连接失败: %s", e)
            lived = time.time() - started
            if lived > 60:
                backoff = 2
                continue
            log.warning("连接只维持了 %.0f 秒（关闭码 %s），%d 秒后重连", lived, self.last_close, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _once(self):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.last_close = None
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(self.url, headers=headers, heartbeat=None) as ws:
                self.ws = ws
                log.info("OneBot 已连接：%s", self.url)
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        continue
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    echo = data.get("echo")
                    if echo and echo in self.pending:
                        fut = self.pending.pop(echo)
                        if not fut.done():
                            fut.set_result(data)
                    else:
                        asyncio.create_task(self._safe_event(data))
                self.last_close = ws.close_code
                self.ws = None

    async def _safe_event(self, data):
        try:
            await self.on_event(data)
        except Exception:  # noqa: BLE001
            log.exception("处理事件失败")

    async def call(self, action, **params):
        if self.ws is None:
            raise RuntimeError("OneBot 未连接")
        echo = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.pending[echo] = fut
        await self.ws.send_json({"action": action, "params": params, "echo": echo})
        try:
            return await asyncio.wait_for(fut, 20)
        finally:
            self.pending.pop(echo, None)


# ----------------------------------------------------------------- 机器人
class Bot:
    def __init__(self, cfg, persona, llm):
        self.cfg = cfg
        self.persona = persona
        self.llm = llm
        self.onebot = OneBot(cfg.onebot_url, cfg.onebot_token, self.on_event)
        self.inbox = {}
        self.last_activity = float(db.meta_get("last_activity", 0) or 0)
        self.last_chat = db.meta_get("last_chat", "") or (cfg.owner_qq[0] if cfg.owner_qq else "")
        self._background = set()
        self._momentum = 0.0

    # -------------------------------------------------- 事件入口
    async def on_event(self, data):
        if data.get("post_type") != "message":
            return
        if str(data.get("user_id")) == str(data.get("self_id")):
            return
        uid = str(data.get("user_id"))
        if self.cfg.owner_qq and uid not in self.cfg.owner_qq:
            log.info("忽略非白名单消息：来自 %s（白名单=%s）", uid, ",".join(self.cfg.owner_qq))
            return
        if data.get("message_type") == "group":
            if not self.cfg.allow_groups:
                return
            if not self._mentioned(data):
                return
            chat = f"g{data.get('group_id')}"
        else:
            chat = uid
        text = self._seg_text(data.get("message"))
        self._catch_files(chat, data.get("message"))
        if not text:
            return
        log.info("收到 %s：%s", uid, text[:60])
        self.last_chat = chat
        db.meta_set("last_chat", chat)
        self._touch()

        st = self.inbox.setdefault(chat, {"buf": [], "task": None})
        st["buf"].append(text)
        if st["task"]:
            st["task"].cancel()
        st["task"] = asyncio.create_task(self._flush(chat))

    @staticmethod
    def _mentioned(data):
        segs = data.get("message")
        if isinstance(segs, list):
            return any(
                s.get("type") == "at" and str(s.get("data", {}).get("qq")) in ("all", str(data.get("self_id")))
                for s in segs
            )
        raw = data.get("raw_message") or ""
        return f"[CQ:at,qq={data.get('self_id')}]" in raw

    @staticmethod
    def _seg_text(msg):
        if isinstance(msg, str):
            return msg.strip()
        out = []
        for seg in msg or []:
            t = seg.get("type")
            d = seg.get("data") or {}
            if t == "text":
                out.append(d.get("text", ""))
            elif t == "at":
                out.append(f"@{d.get('qq')}")
            elif t == "image":
                s = (d.get("summary") or "").strip()
                out.append(f"（发了张图片：{s}）" if s and s not in ("[图片]", "[动画表情]") else "（发了张图片）")
            elif t == "mface":
                s = (d.get("summary") or "").strip()
                out.append(f"（发了个表情包：{s}）" if s else "（发了个表情包）")
            elif t == "face":
                s = (d.get("summary") or "").strip()
                out.append(f"（发了个表情：{s}）" if s else "（发了个表情）")
            elif t == "record":
                out.append("（发了段语音）")
            elif t == "video":
                out.append("（发了段视频）")
            elif t == "file":
                name = str(d.get("file") or d.get("name") or "").strip()
                out.append(f"（发了个文件：{name}）" if name else "（发了个文件）")
        return "".join(out).strip()

    def _catch_files(self, chat_id, msg):
        """文件记下名字和日期，内容看不到，但以后能提起。"""
        if not isinstance(msg, list):
            return
        for seg in msg:
            if seg.get("type") != "file":
                continue
            d = seg.get("data") or {}
            name = str(d.get("file") or d.get("name") or "").strip()
            if name:
                memory.record_file(chat_id, name, d.get("file_size"))
                log.info("记下他发的文件：%s", name[:40])

    def _touch(self):
        self.last_activity = time.time()
        db.meta_set("last_activity", self.last_activity)

    # -------------------------------------------------- 合并连续消息
    async def _flush(self, chat_id):
        try:
            await asyncio.sleep(self.cfg.debounce)
        except asyncio.CancelledError:
            return
        st = self.inbox.get(chat_id)
        if not st:
            return
        # 连着发了好几条的时候，偶尔会等他把话说完再回
        extra = pacing.burst_extra_delay(len(st["buf"]), self.cfg.debounce)
        if extra:
            try:
                await asyncio.sleep(extra)
            except asyncio.CancelledError:
                return
            st = self.inbox.get(chat_id)
            if not st:
                return
        text = "\n".join(st["buf"]).strip()
        st["buf"] = []
        st["task"] = None
        if text:
            await self.handle(chat_id, text)

    # -------------------------------------------------- 主流程
    async def handle(self, chat_id, text):
        try:
            if text.startswith("#"):
                await self.command(chat_id, text)
                return
            self._touch()

            # 刚发过表情包，他说换一个：直接换一张，不用麻烦模型
            if self._sticker_retry(text):
                memory.add_message(chat_id, "user", text)
                await self._send_sticker(chat_id, db.meta_get("last_sticker") or "随机")
                return

            # 她正在外面/在忙/在睡：话先收着，等回来再说
            away_until = float(db.meta_get("away_until", 0) or 0)
            if away_until > time.time():
                log.info("她这会儿不在（%s），这条先收着", (db.meta_get("away_reason") or "")[:18])
                memory.add_message(chat_id, "user", text)
                db.meta_set("away_unread", "1")
                return

            hour = time.localtime().tm_hour
            night = self.cfg.night_slowdown and pacing.is_night(
                hour, self.cfg.quiet_start, self.cfg.quiet_end
            )
            busy = bool(self.cfg.busy_hours) and pacing.busy_at(hour, self.cfg.busy_ranges())
            depth = pacing.depth_of(text)

            last_ts = memory.last_user_ts(chat_id)
            gap = time.time() - last_ts if last_ts else 0
            self._momentum = pacing.momentum_from_gap(gap)

            action, delay, hint = pacing.decide_reply(
                text, hour, self.persona.state.get("心情"), busy, night
            )
            if not self.cfg.skip_enabled and action == "skip":
                action, delay, hint = "now", 0, ""
            if gap > 8 * 3600:
                hint = (hint + "\n" + pacing.gap_hint(gap)).strip()

            rec = memory.recall(chat_id, text, self.cfg.memory_tokens)
            rec["files"] = memory.recent_files_text(chat_id)
            hist = memory.history_messages(
                chat_id, self.cfg.history_limit, self.cfg.history_tokens
            )
            mid = memory.add_message(chat_id, "user", text)
            self._bump_mood(text)

            # 半夜被吵醒：懵一句就接着睡，别再聊
            if night and self.cfg.sleep_note and depth < 2 and await self._sleep_note(chat_id):
                return

            if action == "skip":
                log.info("这条先不回：%s", text[:24])
                return
            if action == "later":
                log.info("过 %.0f 秒再回（%s）", delay, hint[:16])
                self._spawn(self._reply_later(chat_id, mid, text, hist, rec, delay, hint, depth))
                return
            await self._answer(chat_id, text, hist, rec, hint, depth, mid)
        except Exception:  # noqa: BLE001
            log.exception("回复失败")

    async def _reply_later(self, chat_id, mid, text, hist, rec, delay, hint, depth):
        await asyncio.sleep(delay)
        latest = memory.last_user_id(chat_id)
        if latest and latest > mid:
            log.info("等待期间来了新消息，这条延迟回复作废")
            return
        await self._answer(chat_id, text, hist, rec, hint, depth, mid)

    async def _answer(self, chat_id, text, hist, rec, hint, depth, mid=None):
        extra = pacing.DEPTH_HINT.get(depth, "")
        if hint:
            extra = (extra + "\n" + hint).strip()
        if pacing.bad_mood(self.persona.state.get("心情")):
            extra = (extra + "\n你现在心情一般，话少一点，别硬撑热情。").strip()
        short_in = len(text.strip()) <= 6
        if short_in:
            extra = (extra + "\n他就回了很短一句，你也短，一两句就行，别硬找话题。").strip()
        elif memory.count_recent(chat_id, 120) <= 3:
            recent = memory.recent_user_texts(chat_id, 3)
            if recent and sum(len(t) for t in recent) / len(recent) < 6:
                extra = (extra + "\n他这会儿回得都很短，像是没心思聊，别自嗨。").strip()
        try:
            residue = float(db.meta_get("residue", 0) or 0)
        except ValueError:
            residue = 0
        if residue > time.time() and depth < 2:
            extra = (extra + "\n你刚认真答完一大段，现在有点懒得动脑，回短一点。").strip()

        sched = await schedule.fetch(self.cfg)
        sched_text = schedule.render(sched) if sched else ""
        life_text = life.render() if self.cfg.life else ""

        msgs = (
            [
                {
                    "role": "system",
                    "content": build_system(
                        self.persona, rec, extra, sched_text, life_text, memes.render()
                    ),
                }
            ]
            + hist
            + [{"role": "user", "content": text}]
        )
        max_tokens = {0: 200, 1: 450, 2: 900}.get(depth, 450)
        reply = await self.llm.chat(msgs, max_tokens=max_tokens)
        reply = self._extract_reminders(chat_id, reply)
        reply = self._extract_alarms(chat_id, reply)
        want_more, reply = self._extract_marker(reply)
        problem = ""
        slip = guard.check(reply, text)
        if slip:
            log.info("穿帮了：%s", slip)
            problem = f"（你刚说的话穿帮了：{slip}。按现在的时间重说一遍，别再说错）"
        elif self._too_repetitive(chat_id, reply):
            log.info("这句和前面说的太像，换个说法")
            problem = "（刚才那句跟前面说过的重复了，换个说法，别用一样的句式和词）"
        elif self._service_tone(reply):
            log.info("这句太像客服，重说")
            problem = "（这句太像客服或助手了，别问「什么问题」「有什么可以帮你」，用朋友的口吻重说，短一点）"
        if problem:
            retry = msgs + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": problem},
            ]
            reply2 = await self.llm.chat(retry, max_tokens=max_tokens)
            reply2 = self._extract_reminders(chat_id, reply2)
            more2, reply2 = self._extract_marker(reply2)
            if reply2 and not self._service_tone(reply2):
                slip2 = guard.check(reply2, text)
                if slip2:
                    log.info("重说还是穿帮：%s", slip2)
                reply, want_more = reply2, more2
        stick, reply = self._extract_sticker(reply)
        if depth >= 2:
            stick = ""  # 认真答题的时候不发表情
        elif not stick:
            stick = self._maybe_sticker(text, reply, depth, short_in)
        max_b = 2 if short_in else (5 if depth >= 1 else 4)
        if reply.strip():
            await self.say(chat_id, reply, user_text=text, depth=depth, max_bubbles=max_b)
        if stick:
            self._spawn(self._send_sticker(chat_id, stick))
        self._spawn(self._after_reply(chat_id))
        if depth >= 2:
            db.meta_set("residue", str(time.time() + 600))
        if want_more:
            self._schedule_follow_up(chat_id, mid, depth)
        self._maybe_leave(chat_id, depth)

    _SERVICE_RE = re.compile(
        r"(什么问题|有什么可以帮|需要我(做|帮)什么|请讲|很高兴为你|你可以告诉我|我来帮你解决)"
    )

    def _service_tone(self, reply):
        return bool(self._SERVICE_RE.search(reply or ""))

    def _too_repetitive(self, chat_id, reply):
        if len(reply or "") < 10:
            return False
        return any(
            memory.similarity(reply, prev) > 0.6
            for prev in memory.recent_bot_texts(chat_id, 4)
            if len(prev) >= 6
        )

    def _bump_mood(self, text):
        """对方一句话把我带到另一个情绪，但十分钟内只变一次。"""
        if not self.cfg.mood_shift:
            return
        now = time.time()
        if now - float(db.meta_get("mood_ts", 0) or 0) < 600:
            return
        mood = pacing.mood_after(self.persona.state.get("心情"), text)
        if mood and self.persona.soft_mood(mood):
            db.meta_set("mood_ts", now)
            log.info("心情被带动：%s", mood)

    # -------------------------------------------------- 她自己的时间：离开 / 睡着 / 回来
    def _go_away(self, until, reason):
        db.meta_set("away_until", str(until))
        db.meta_set("away_reason", reason)
        db.meta_set("away_unread", "0")

    def _next_quiet_end(self):
        now = dt.datetime.now()
        end = now.replace(
            hour=self.cfg.quiet_end, minute=random.randint(0, 25), second=0, microsecond=0
        )
        if end <= now:
            end += dt.timedelta(days=1)
        return end.timestamp()

    async def _sleep_note(self, chat_id):
        """半夜被吵醒，回一句迷糊的就接着睡到早上。"""
        today = time.strftime("%Y-%m-%d")
        if db.meta_get(f"sleepnote:{today}"):
            db.meta_set("away_unread", "1")
            return False
        if random.random() > 0.8:
            return False
        db.meta_set(f"sleepnote:{today}", "1")
        self._go_away(self._next_quiet_end(), "睡着")
        await asyncio.sleep(random.uniform(3, 8))
        line = random.choice(pacing.SLEEP_LINES)
        log.info("半夜被吵醒：%s", line)
        await self.say(chat_id, line, depth=0, max_bubbles=1)
        return True

    def _maybe_leave(self, chat_id, depth):
        if not self.cfg.away or self._is_night() or depth >= 2:
            return
        if float(db.meta_get("away_until", 0) or 0) > time.time():
            return
        if memory.count_recent(chat_id, 3600) < 6:
            return
        if random.random() > self.cfg.away_chance:
            return
        self._spawn(self._leave(chat_id))

    async def _leave(self, chat_id):
        line = pacing.away_line(life.current_doing())
        until = time.time() + random.uniform(12, 45) * 60
        self._go_away(until, line)
        log.info("她先离开一会儿：%s（%.0f 分钟后回来）", line, (until - time.time()) / 60)
        await asyncio.sleep(random.uniform(1.5, 4.5))
        await self.say(chat_id, line, depth=0, max_bubbles=1)

    async def away_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                until = float(db.meta_get("away_until", 0) or 0)
                if not until or until > time.time():
                    continue
                unread = db.meta_get("away_unread") == "1"
                db.meta_set("away_until", "0")
                db.meta_set("away_unread", "0")
                if until < time.time() - 86400 or self._is_night():
                    continue
                chat = db.meta_get("last_chat") or self.last_chat
                if not chat:
                    continue
                log.info("她回来了（有未读=%s）", unread)
                if unread:
                    await self._back_with_unread(chat)
                elif random.random() < 0.6:
                    await self.say(
                        chat, random.choice(pacing.RETURN_LINES), depth=0, max_bubbles=1
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("回来检查失败")

    async def _back_with_unread(self, chat_id):
        try:
            rec = memory.recall(chat_id, "", self.cfg.memory_tokens)
            rec["files"] = memory.recent_files_text(chat_id)
            hist = memory.history_messages(chat_id, 12, 700)
            sched = await schedule.fetch(self.cfg)
            sched_text = schedule.render(sched) if sched else ""
            hint = (
                "你刚才不在（去忙了或在睡），现在回来了，看到他给你发的消息。"
                "先说一句刚在干嘛、才看到，然后接着他的话回。别道歉太多次。"
            )
            msgs = (
                [
                    {
                        "role": "system",
                        "content": build_system(
                            self.persona, rec, hint, sched_text, life.render() if self.cfg.life else "", memes.render()
                        ),
                    }
                ]
                + hist
                + [{"role": "user", "content": "（你刚回来，看到他给你发的消息）"}]
            )
            text = await self.llm.chat(msgs, max_tokens=300)
            text = self._extract_reminders(chat_id, text)
            _, text = self._extract_marker(text)
            if text:
                await self.say(chat_id, text, depth=0, max_bubbles=2)
        except Exception:  # noqa: BLE001
            log.exception("回消息失败")

    # -------------------------------------------------- 联想续聊（由它自己决定）
    _STICKER_RE = re.compile(
        r"\[\[\s*表情\s*[:：]?\s*([^\]\n]{1,30}?)\s*\]\]|\[sticker\s*[:：]\s*([^\]\n]{1,30}?)\]",
        re.I,
    )

    def _extract_sticker(self, reply):
        name = ""

        def take(m):
            nonlocal name
            if not name:
                name = (m.group(1) or m.group(2) or "").strip()
            return ""

        cleaned = self._STICKER_RE.sub(take, reply or "")
        return name, cleaned.strip()
    _MORE_RE = re.compile(r"^\s*\[\[\s*接着\s*\]\]\s*$")

    def _extract_marker(self, reply):
        want = False
        kept = []
        for line in reply.splitlines():
            if self._MORE_RE.match(line):
                want = True
                continue
            kept.append(line)
        return want, "\n".join(kept).strip()

    def _schedule_follow_up(self, chat_id, mid, depth):
        if not self.cfg.follow_up or self._is_night():
            return
        log.info("续聊：它自己想再说一句")
        self._spawn(self._follow_up(chat_id, mid, depth))

    async def _follow_up(self, chat_id, mid, depth):
        await asyncio.sleep(pacing.follow_up_delay(depth))
        if (memory.last_user_id(chat_id) or 0) > mid:
            return  # 他已经回了，不用补
        try:
            rec = memory.recall(chat_id, "", self.cfg.memory_tokens)
            rec["files"] = memory.recent_files_text(chat_id)
            hist = memory.history_messages(chat_id, 12, 700)
            sched = await schedule.fetch(self.cfg)
            sched_text = schedule.render(sched) if sched else ""
            life_text = life.render() if self.cfg.life else ""
            msgs = (
                [
                    {
                        "role": "system",
                        "content": build_system(
                            self.persona, rec, FOLLOW_UP_HINT, sched_text, life_text,
                            memes.render(6),
                        ),
                    }
                ]
                + hist
                + [{"role": "user", "content": "（你刚回完他，又想到一点，想再补一句）"}]
            )
            reply = await self.llm.chat(msgs, max_tokens=120)
            reply = self._extract_reminders(chat_id, reply)
            if not reply:
                return
            log.info("补一句：%s", reply[:30])
            await self.say(chat_id, reply, user_text="", depth=depth, max_bubbles=2)
        except Exception:  # noqa: BLE001
            log.exception("补话失败")

    _REMIND_RE = re.compile(
        r"\[\[\s*提醒\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s+(.+?)\s*\]\]"
    )

    _ALARM_RE = re.compile(
        r"\[\[\s*闹钟\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s+(.+?)\s*\]\]"
    )

    def _extract_alarms(self, chat_id, reply):
        """她自己定的闹钟：到点由她主动开口，不是提醒对方做事。"""
        if "[[" not in reply:
            return reply
        kept = []
        for line in reply.splitlines():
            m = self._ALARM_RE.search(line)
            if not m:
                kept.append(line)
                continue
            try:
                due = dt.datetime.fromisoformat(f"{m.group(1)} {m.group(2)}")
            except ValueError:
                kept.append(line)
                continue
            if due.timestamp() > time.time():
                db.run(
                    "INSERT INTO alarms(ts,chat_id,due,reason,fired) VALUES(?,?,?,?,0)",
                    (time.time(), str(chat_id), due.timestamp(), m.group(3).strip()[:80]),
                )
                log.info("她自己定了闹钟：%s %s", due.strftime("%m-%d %H:%M"), m.group(3)[:20])
            continue
        return "\n".join(kept).strip()

    def _extract_reminders(self, chat_id, reply):
        if not self.cfg.reminders or "[[" not in reply:
            return reply
        kept = []
        for line in reply.splitlines():
            m = self._REMIND_RE.search(line)
            if m:
                try:
                    due = dt.datetime.fromisoformat(f"{m.group(1)} {m.group(2)}")
                except ValueError:
                    kept.append(line)
                    continue
                if due.timestamp() > time.time():
                    db.run(
                        "INSERT INTO reminders(ts,chat_id,due,text,sent) VALUES(?,?,?,?,0)",
                        (time.time(), str(chat_id), due.timestamp(), m.group(3).strip()[:80]),
                    )
                    log.info("设了提醒：%s %s", due.strftime("%m-%d %H:%M"), m.group(3)[:20])
                continue
            kept.append(line)
        return "\n".join(kept).strip()

    async def say(self, chat_id, text, user_text="", depth=0, max_bubbles=3, momentum=None):
        bubbles = plan(text, max_bubbles=max_bubbles)
        if not bubbles:
            return
        night = self.cfg.night_slowdown and self._is_night()
        m = self._momentum if momentum is None else momentum
        # 先看一会儿手机，开始打字了对方才会看到"正在输入"
        await asyncio.sleep(
            pacing.read_delay(user_text, depth, night, self.cfg.fast_reply_chance, m)
        )
        for i, b in enumerate(bubbles):
            if i:
                await asyncio.sleep(gap_delay(b))
            await self._typing(chat_id)
            await asyncio.sleep(pacing.typing_delay(b, depth, night))
            await self._send(chat_id, b)
            memory.add_message(chat_id, "bot", b)
        self._touch()

    async def _typing(self, chat_id):
        try:
            await self.onebot.call("set_input_status", user_id=int(chat_id), event_type=1)
        except Exception:  # noqa: BLE001
            pass

    async def _send(self, chat_id, text):
        log.info("发出 → %s：%s", chat_id, text[:60])
        params = {"message": [{"type": "text", "data": {"text": text}}]}
        if self.cfg.allow_groups and str(chat_id).startswith("g"):
            params["group_id"] = int(str(chat_id)[1:])
            await self.onebot.call("send_group_msg", **params)
        else:
            params["user_id"] = int(chat_id)
            await self.onebot.call("send_private_msg", **params)

    _RETRY_WORDS = ("换一个", "换一张", "换个", "换张", "再来一个", "再换一个", "不要这个")

    def _sticker_retry(self, text):
        if text.strip() not in self._RETRY_WORDS:
            return False
        try:
            ts = float(db.meta_get("last_sticker_ts", 0) or 0)
        except ValueError:
            return False
        return time.time() - ts < 180 and bool(db.meta_get("last_sticker"))

    def _sticker_hist(self):
        raw = db.meta_get("sticker_hist") or ""
        return [x for x in raw.split(",") if x]

    def _maybe_sticker(self, user_text, reply, depth, short_in):
        """模型没写 [sticker:…] 时，按熟人聊天的频率补一张。"""
        chance = float(getattr(self.cfg, "sticker_chance", 0.55) or 0)
        if chance <= 0 or "```" in (reply or ""):
            return ""
        if depth >= 1:
            chance *= 0.45
        if not short_in and len(reply or "") > 70:
            chance *= 0.6
        try:
            last = float(db.meta_get("last_sticker_ts", 0) or 0)
        except ValueError:
            last = 0
        if time.time() - last < 25:
            return ""
        if random.random() >= chance:
            return ""
        return stickers.guess((user_text or "") + " " + (reply or ""))

    async def _send_sticker(self, chat_id, query, exclude=None):
        hist = self._sticker_hist()
        if exclude:
            hist.append(exclude)
        it = stickers.pick(query, exclude=hist)
        if not it:
            it = stickers.pick("随机", exclude=hist)
        if not it:
            log.info("没找到合适表情：%s", query)
            return
        data = await stickers.fetch(it, self.cfg.data_dir / "stickers")
        if not data:
            return
        await asyncio.sleep(random.uniform(0.4, 1.0))
        seg = [
            {
                "type": "image",
                "data": {"file": "base64://" + base64.b64encode(data).decode()},
            }
        ]
        log.info("发了个表情包 → %s：%s", chat_id, it["id"])
        if self.cfg.allow_groups and str(chat_id).startswith("g"):
            await self.onebot.call(
                "send_group_msg", group_id=int(str(chat_id)[1:]), message=seg
            )
        else:
            await self.onebot.call("send_private_msg", user_id=int(chat_id), message=seg)
        memory.add_message(chat_id, "bot", f"（发了个表情包：{it['name']}）")
        db.meta_set("last_sticker", query)
        db.meta_set("last_sticker_id", it["id"])
        db.meta_set("last_sticker_ts", str(time.time()))
        db.meta_set("sticker_hist", ",".join((hist + [it["id"]])[-10:]))

    # -------------------------------------------------- 记忆维护
    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._background.add(t)
        t.add_done_callback(self._background.discard)

    async def _after_reply(self, chat_id):
        try:
            self.persona.maybe_reload()
            if memory.maybe_compress_threshold(chat_id, self.cfg.episode_threshold):
                eid = await memory.compress(chat_id, self.llm, self.persona)
                if eid:
                    n = db.one("SELECT COUNT(*) c FROM episodes WHERE chat_id=?", (str(chat_id),))["c"]
                    if n and n % self.cfg.reflect_every == 0:
                        await memory.reflect(self.llm, self.persona, chat_id)
        except Exception:  # noqa: BLE001
            log.exception("记忆维护失败")

    # -------------------------------------------------- 主动发消息（按作息时间点唤醒）
    async def wake_loop(self):
        slots = pacing.parse_slots(self.cfg.wake_slots)
        log.info("主动唤醒时间点：%s", self.cfg.wake_slots or "无")
        while True:
            await asyncio.sleep(60)
            try:
                if not self.cfg.proactive or not self.last_chat or not slots:
                    continue
                if self._is_night():
                    continue
                if float(db.meta_get("away_until", 0) or 0) > time.time():
                    continue
                now = time.localtime()
                now_min = now.tm_hour * 60 + now.tm_min
                today = time.strftime("%Y-%m-%d")
                for i, (s, e) in enumerate(slots):
                    key = f"wake:{today}:{i}"
                    at = db.meta_get(key)
                    if at is None:
                        at = random.randint(s, max(s, e - 1))
                        db.meta_set(key, str(at))
                    if now_min < int(at) or db.meta_get(key + ":fired"):
                        continue
                    db.meta_set(key + ":fired", "1")
                    if time.time() - self.last_activity < self.cfg.proactive_min_gap_min * 60:
                        log.info("刚聊过，跳过第 %d 个时间点的主动消息", i + 1)
                        continue
                    if not self._proactive_quota_ok(today):
                        log.info("今天主动消息已达上限")
                        continue
                    if random.random() < 0.9:
                        await self.proactive(slot=i)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("主动消息失败")

    def _proactive_quota_ok(self, today):
        used = int(db.meta_get(f"proactive:{today}", 0) or 0)
        return used < self.cfg.proactive_daily_max

    async def proactive(self, slot=0, custom=""):
        chat = self.last_chat
        rec = memory.recall(chat, "", self.cfg.memory_tokens)
        rec["files"] = memory.recent_files_text(chat)
        hist = memory.history_messages(chat, 10, 500)
        hint = custom or pacing.SLOT_HINT.get(slot, "")
        sched = await schedule.fetch(self.cfg)
        sched_text = schedule.render(sched) if sched else ""
        life_text = life.render() if self.cfg.life else ""
        msgs = (
            [
                {
                    "role": "system",
                    "content": build_proactive_system(
                        self.persona, rec, hint, sched_text, life_text, memes.render(6)
                    ),
                }
            ]
            + hist
            + [{"role": "user", "content": "（没有新消息，你想主动说一句）"}]
        )
        text = await self.llm.chat(msgs, max_tokens=200)
        slip = guard.check(text or "", "")
        if slip:
            log.info("主动消息穿帮：%s", slip)
            text = await self.llm.chat(
                msgs
                + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"（这句穿帮了：{slip}。按现在的时间重说一句）"},
                ],
                max_tokens=200,
            )
        stick, text = self._extract_sticker(text or "")
        if not text and not stick:
            return
        today = time.strftime("%Y-%m-%d")
        db.meta_set(f"proactive:{today}", str(int(db.meta_get(f"proactive:{today}", 0) or 0) + 1))
        log.info("主动消息[%d]：%s", slot + 1, text[:40])
        await self.say(chat, text, max_bubbles=2, momentum=0.0)
        if stick:
            self._spawn(self._send_sticker(chat, stick))

    # -------------------------------------------------- 运维命令
    async def command(self, chat_id, text):
        cmd = text.lstrip("#").strip()
        name, _, arg = cmd.partition(" ")
        name, arg = name.strip(), arg.strip()
        out = "?"
        if name in ("记忆", "memory"):
            out = memory.report(self.persona)
        elif name in ("人设", "persona"):
            out = self.persona.summary() + "\n\n" + self.persona.state_block()
        elif name in ("遗忘", "forget"):
            out = memory.forget_fact(arg) if not arg.isdigit() else memory.forget_episode(int(arg))
        elif name in ("压缩", "compress"):
            eid = await memory.compress(chat_id, self.llm, self.persona)
            out = f"已压缩为片段 #{eid}" if eid else "消息还不够，先聊一会儿"
        elif name in ("重载", "reload"):
            self.persona.reload()
            out = f"人设已重载：{self.persona.name}"
        elif name in ("档案", "profile"):
            out = memory.read_archive()
        elif name in ("设定", "self"):
            added = self.persona.add_self_note(arg)
            out = f"记住了：{added}" if added else "没写进去（空的或已经有了）"
        elif name in ("习惯", "style"):
            added = self.persona.add_style_note(arg)
            out = f"好，我以后这样说话：{added}" if added else "没写进去（空的或已经有了）"
        elif name in ("提醒", "remind"):
            due = schedule.parse_when(arg)
            if not due:
                out = "没看懂时间，试试：#提醒 明天 15:00 交作业"
            else:
                content = schedule.strip_when(arg) or arg
                db.run(
                    "INSERT INTO reminders(ts,chat_id,due,text,sent) VALUES(?,?,?,?,0)",
                    (time.time(), str(chat_id), due.timestamp(), content[:80]),
                )
                out = f"好 {due.strftime('%m-%d %H:%M')} 提醒你{content}"
        elif name in ("任务", "schedule"):
            st = await schedule.fetch(self.cfg, force=True)
            out = schedule.render(st) if st else "连不上任务中心，检查 SCHEDULE_URL 和 SCHEDULE_TOKEN"
        elif name in ("生活", "life"):
            out = (life.render() or "今天还没生成，等一会儿") if self.cfg.life else "这个人设下没有日常"
        elif name in ("文件", "files"):
            out = memory.recent_files_text(chat_id) or "他还没发过文件"
        elif name in ("审计", "audit"):
            rows = self.persona.history(10)
            lines = []
            for r in rows:
                t = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
                lines.append(f"#{r['id']} {t} {r['reason']} {r['diff'][:90]}")
            out = "\n".join(lines) or "还没改过"
        elif name in ("回滚", "rollback"):
            out = self.persona.rollback(arg) if arg.isdigit() else "用法：#回滚 <编号>"
        elif name in ("闹钟", "alarm"):
            rows = db.q("SELECT due,reason,fired FROM alarms ORDER BY due DESC LIMIT 10")
            out = "\n".join(
                f"{time.strftime('%m-%d %H:%M', time.localtime(r['due']))} "
                f"{'已响' if r['fired'] else '待响'} {r['reason']}"
                for r in rows
            ) or "还没定过闹钟"
        elif name in ("表情", "sticker", "贴纸"):
            await self._send_sticker(chat_id, arg or "随机")
            return
        elif name in ("梗", "memes"):
            if arg:
                term, _, meaning = arg.partition(" ")
                if meaning.strip():
                    ok = memes.add(term, meaning.strip())
                    out = f"学到了：{ok}" if ok else "没写进去（词或意思太短）"
                else:
                    out = memes.listing()
            else:
                out = memes.listing()
        elif name in ("熟悉", "familiarity"):
            try:
                v = float(arg)
            except ValueError:
                out = "用法：#熟悉 0.5（0 到 1，越大越熟越放得开）"
            else:
                v = max(0.0, min(1.0, v))
                self.persona.state["关系熟悉度"] = v
                self.persona._save_state()
                out = f"关系熟悉度 = {v}（{self.persona.familiarity_desc()}）"
        elif name in ("醒醒", "醒", "wake"):
            db.meta_set("away_until", "0")
            db.meta_set("away_unread", "0")
            db.meta_set(f"sleepnote:{time.strftime('%Y-%m-%d')}", "1")
            out = "嗯…我在 怎么了"
        elif name in ("帮助", "help", ""):
            out = (
                "#记忆 看记得什么\n#人设 看当前状态\n#档案 看你的个人档案\n"
                "#生活 看她今天过得怎么样\n#文件 看她记下的你发过的文件\n"
                "#醒醒 叫醒她/立刻结束离开状态\n"
                "#熟悉 0.5 调关系熟悉度（越大说话越放得开）\n"
                "#梗 看最近在学什么梗；#梗 词 意思 教她一个新梗\n"
                "#表情 开心 让她发一张表情包\n"
                "#审计 看人设改动记录；#回滚 <编号> 退回某次改动\n"
                "#闹钟 看她给自己定的主动消息\n"
                "#设定 <一句话> 让它记住对自己的认识\n#习惯 <一句话> 调整它的说话方式\n"
                "#提醒 明天 15:00 交作业\n#任务 看今天的课和最近的安排\n"
                "#遗忘 <条目>|<片段号> 忘掉一条\n#压缩 立刻整理记忆\n#重载 重新读取 persona.yaml"
            )
        await self._send(chat_id, out)

    # -------------------------------------------------- 杂项
    def _is_night(self):
        h = time.localtime().tm_hour
        a, b = self.cfg.quiet_start, self.cfg.quiet_end
        return (a <= h < b) if a < b else (h >= a or h < b)

    async def decay_loop(self):
        while True:
            await asyncio.sleep(self.cfg.decay_every_h * 3600)
            try:
                memory.decay()
            except Exception:  # noqa: BLE001
                log.exception("衰减失败")

    # -------------------------------------------------- 她自己的生活
    async def life_loop(self):
        while True:
            try:
                await life.daily_tick(self.llm, self.persona)
            except Exception:  # noqa: BLE001
                log.exception("生成日常失败")
            await asyncio.sleep(1800)

    # -------------------------------------------------- 定时提醒 / 久不联系
    async def reminder_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                rows = db.q(
                    "SELECT * FROM reminders WHERE sent=0 AND due<=? ORDER BY due", (time.time(),)
                )
                for r in rows:
                    db.run("UPDATE reminders SET sent=1 WHERE id=?", (r["id"],))
                    if time.time() - r["due"] > 6 * 3600:
                        continue
                    msg = random.choice(
                        [
                            f"诶 到点了 {r['text']}",
                            f"提醒你一下 {r['text']}",
                            f"{r['text']} 别忘了",
                        ]
                    )
                    log.info("提醒：%s", r["text"][:30])
                    await self.say(r["chat_id"], msg)
            except Exception:  # noqa: BLE001
                log.exception("提醒发送失败")

    async def alarm_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                rows = db.q(
                    "SELECT * FROM alarms WHERE fired=0 AND due<=? ORDER BY due", (time.time(),)
                )
                for r in rows:
                    db.run("UPDATE alarms SET fired=1 WHERE id=?", (r["id"],))
                    if time.time() - r["due"] > 6 * 3600:
                        continue
                    if self._is_night():
                        continue
                    today = time.strftime("%Y-%m-%d")
                    if not self._proactive_quota_ok(today):
                        continue
                    log.info("她自己定的闹钟到点：%s", r["reason"][:30])
                    await self.proactive(slot=2, custom=f"你之前想着这会儿找他聊：{r['reason']}")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("闹钟失败")

    async def silence_loop(self):
        while True:
            await asyncio.sleep(600)
            try:
                await self._silence_check()
            except Exception:  # noqa: BLE001
                log.exception("沉默检查失败")

    async def _silence_check(self):
        if not self.cfg.proactive or not self.last_chat or self._is_night():
            return
        last = memory.last_user_ts(self.last_chat)
        if not last or time.time() - last < 24 * 3600:
            return
        today = time.strftime("%Y-%m-%d")
        key = f"silence:{today}"
        if db.meta_get(key) or not self._proactive_quota_ok(today):
            return
        db.meta_set(key, "1")
        log.info("对方 %.0f 小时没说话，主动问一句", (time.time() - last) / 3600)
        await self.proactive(slot=3)

    async def run(self):
        if self.cfg.life:
            self._spawn(self.life_loop())
        self._spawn(self.wake_loop())
        self._spawn(self.away_loop())
        self._spawn(self.decay_loop())
        self._spawn(self.reminder_loop())
        self._spawn(self.alarm_loop())
        self._spawn(self.silence_loop())
        log.info("机器人启动，人设=%s，监听=%s", self.persona.name, self.cfg.onebot_url)
        await self.onebot.run()
