import asyncio
import json
import logging
import re

import aiohttp

log = logging.getLogger("llm")


class LLM:
    def __init__(self, cfg):
        self.cfg = cfg
        self._session = None

    async def _sess(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            )
        return self._session

    async def chat(self, messages, temperature=None, json_mode=False, max_tokens=800):
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }
        last = None
        for attempt in range(3):
            try:
                s = await self._sess()
                async with s.post(url, json=payload, headers=headers) as r:
                    body = await r.text()
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}: {body[:300]}")
                    data = json.loads(body)
                    return (data["choices"][0]["message"]["content"] or "").strip()
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("LLM 调用失败 (%s/3): %s", attempt + 1, e)
                await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"LLM 连续失败: {last}")

    async def chat_json(self, messages, temperature=0.3, max_tokens=700):
        raw = await self.chat(messages, temperature=temperature, json_mode=True, max_tokens=max_tokens)
        return _parse_json(raw)


def _parse_json(raw):
    if not raw:
        return {}
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(raw[i : j + 1])
        except json.JSONDecodeError:
            pass
    log.warning("JSON 解析失败: %s", raw[:200])
    return {}
