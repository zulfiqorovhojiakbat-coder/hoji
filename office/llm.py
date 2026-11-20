"""Streaming LLM client: Gemini and any OpenAI-compatible API, with model fallback."""
import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from . import config

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
MAX_ATTEMPTS = 14

DeltaCb = Callable[[str], Awaitable[None]]
NoticeCb = Callable[[str], Awaitable[None]]


class LLMError(Exception):
    pass


class _ModelUnavailable(Exception):
    def __init__(self, message: str, cooldown: float):
        super().__init__(message)
        self.cooldown = cooldown


@dataclass
class LLMResult:
    text: str
    model: str
    truncated: bool


FIRST_BYTE_TIMEOUT = 90  # overloaded models hang instead of failing fast
HEDGE_DELAY = 40  # seconds a model may stay silent before the next one is started beside it
HEDGE_WIDTH = 2
STICKY_SECONDS = 600

_cooldown_until: dict[str, float] = {}
_last_good: dict[bool, tuple[str, float]] = {}
_gemini_models: tuple[float, str, tuple[list[str], list[str]]] | None = None
_limit = asyncio.Semaphore(3)


async def gemini_models(client: httpx.AsyncClient, key: str) -> tuple[list[str], list[str]]:
    """(full, lite) model chains discovered from the account's model list, cached for an hour."""
    global _gemini_models
    if _gemini_models and _gemini_models[1] == key and time.time() - _gemini_models[0] < 3600:
        return _gemini_models[2]
    r = await client.get(f"{GEMINI_BASE}/models", params={"pageSize": 1000}, headers={"x-goog-api-key": key}, timeout=30)
    if r.status_code != 200:
        raise LLMError(f"Gemini kaliti qabul qilinmadi (HTTP {r.status_code}): {_error_message(r.text)}")
    names = [
        m["name"].removeprefix("models/")
        for m in r.json().get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    def ranked(pattern: str) -> list[str]:
        found = [(re.fullmatch(pattern, n), n) for n in names]
        found = [(m, n) for m, n in found if m]
        return [n for m, n in sorted(found, key=lambda x: (0 if x[0].group(2) else 1, float(x[0].group(1))), reverse=True)]

    flash = ranked(r"gemini-(\d+(?:\.\d+)?)-flash(-preview)?")
    lite = ranked(r"gemini-(\d+(?:\.\d+)?)-flash-lite(-preview)?")
    heavy = [n for n in ("gemini-flash-latest",) if n in names] + flash[:6]
    light = [n for n in ("gemini-flash-lite-latest",) if n in names] + lite[:3]
    if not heavy + light:
        raise LLMError("Bu kalit uchun mos Gemini modeli topilmadi.")
    _gemini_models = (time.time(), key, (heavy, light))
    return heavy, light


def _error_message(body: str) -> str:
    try:
        err = json.loads(body).get("error", {})
        return str(err.get("message") or err)[:300] if isinstance(err, dict) else str(err)[:300]
    except ValueError:
        return body[:300]


def _retry_delay(body: str) -> float | None:
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body)
    return float(m.group(1)) if m else None


def _unavailable(status: int, body: str) -> Exception:
    msg = f"HTTP {status}: {_error_message(body)}"
    if status == 429:
        per_day = "PerDay" in body
        cooldown = 6 * 3600 if per_day else min(_retry_delay(body) or 30, 3600)
        # "-latest" aliases share the quota of the model they point to
        for alias in re.findall(r'"model"\s*:\s*"([\w.\-]+)"', body):
            _cooldown_until[alias] = time.time() + cooldown
        return _ModelUnavailable("kunlik bepul limit tugagan" if per_day else msg, cooldown)
    if status == 404:
        return _ModelUnavailable(msg, 3600)
    if status in (500, 502, 503, 504, 529):
        return _ModelUnavailable(msg, 120)
    return LLMError(msg)


class _Stream:
    """`client.stream()` that gives up if the response headers do not arrive in time."""

    def __init__(self, client: httpx.AsyncClient, url: str, **kwargs):
        self._cm = client.stream("POST", url, **kwargs)

    async def __aenter__(self) -> httpx.Response:
        try:
            return await asyncio.wait_for(self._cm.__aenter__(), FIRST_BYTE_TIMEOUT)
        except asyncio.TimeoutError:
            raise _ModelUnavailable(f"{FIRST_BYTE_TIMEOUT} soniyada javob boshlanmadi", 300) from None

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)


async def _sse_json(resp: httpx.Response):
    async for line in resp.aiter_lines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                try:
                    yield json.loads(data)
                except ValueError:
                    continue


async def _gemini_call(client, key, model, system, messages, max_tokens, on_delta) -> LLMResult:
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["text"]}]} for m in messages
        ],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7},
    }
    url = f"{GEMINI_BASE}/models/{model}:streamGenerateContent"
    parts, finish = [], ""
    async with _Stream(client, url, params={"alt": "sse"}, json=body, headers={"x-goog-api-key": key}) as resp:
        if resp.status_code != 200:
            raise _unavailable(resp.status_code, (await resp.aread()).decode("utf-8", "replace"))
        async for chunk in _sse_json(resp):
            if "error" in chunk:
                raise _ModelUnavailable(str(chunk["error"].get("message", chunk["error"]))[:300], 45)
            for cand in chunk.get("candidates", [])[:1]:
                for part in cand.get("content", {}).get("parts", []):
                    text = part.get("text")
                    if text and not part.get("thought"):
                        parts.append(text)
                        await on_delta(text)
                finish = cand.get("finishReason") or finish
    text = "".join(parts)
    if not text.strip():
        raise _ModelUnavailable(f"bo'sh javob (finishReason={finish or 'yo`q'})", 20)
    return LLMResult(text, model, finish == "MAX_TOKENS")


async def _openai_call(client, key, base_url, model, system, messages, max_tokens, on_delta) -> LLMResult:
    body = {
        "model": model,
        "stream": True,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}]
        + [{"role": m["role"], "content": m["text"]} for m in messages],
    }
    url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    parts, finish = [], ""
    async with _Stream(client, url, json=body, headers={"Authorization": f"Bearer {key}"}) as resp:
        if resp.status_code != 200:
            raise _unavailable(resp.status_code, (await resp.aread()).decode("utf-8", "replace"))
        async for chunk in _sse_json(resp):
            for choice in chunk.get("choices", [])[:1]:
                text = (choice.get("delta") or {}).get("content")
                if text:
                    parts.append(text)
                    await on_delta(text)
                finish = choice.get("finish_reason") or finish
    text = "".join(parts)
    if not text.strip():
        raise _ModelUnavailable("bo'sh javob", 20)
    return LLMResult(text, model, finish == "length")


async def _noop(_: str) -> None:
    return None


class _Attempt:
    """One model's try. Its text is buffered until it is picked as the winner."""

    def __init__(self, model: str, forward: DeltaCb):
        self.model, self.forward = model, forward
        self.buffer: list[str] = []
        self.winner = False
        self.started = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def on_delta(self, text: str) -> None:
        if self.winner:
            await self.forward(text)
        else:
            self.buffer.append(text)
            self.started.set()

    async def promote(self) -> None:
        self.winner = True
        for text in self.buffer:
            await self.forward(text)
        self.buffer.clear()


async def generate(
    system: str,
    messages: list[dict],
    *,
    max_tokens: int = 32000,
    light: bool = False,
    on_delta: DeltaCb = _noop,
    on_reset: Callable[[], Awaitable[None]] | None = None,
    on_notice: NoticeCb = _noop,
) -> LLMResult:
    """Stream one completion. `light` tasks prefer the small fast models.

    Models are tried in order. When one is slow to start answering, the next is started
    alongside it and the first to produce text wins, so an overloaded model costs seconds
    instead of a full timeout.
    """
    cfg = config.load()
    key = cfg["api_key"]
    if not key:
        raise LLMError("AI kaliti kiritilmagan. Sozlamalardan API kalitini kiriting.")
    timeout = httpx.Timeout(connect=15, read=90, write=30, pool=30)
    async with _limit, httpx.AsyncClient(timeout=timeout) as client:
        if cfg["provider"] == "gemini" and not cfg["models"]:
            heavy, lite = await gemini_models(client, key)
            chain = lite + heavy if light else heavy + lite
        elif cfg["provider"] == "gemini":
            chain = cfg["models"]
        else:
            chain = cfg["models"]
            if not chain:
                raise LLMError("OpenAI-mos provayder uchun model nomini sozlamalarda kiriting.")

        async def call(attempt: _Attempt) -> LLMResult:
            if cfg["provider"] == "gemini":
                return await _gemini_call(client, key, attempt.model, system, messages, max_tokens, attempt.on_delta)
            return await _openai_call(
                client, key, cfg["base_url"], attempt.model, system, messages, max_tokens, attempt.on_delta
            )

        def fail(attempt: _Attempt, error: BaseException) -> str:
            cooldown = error.cooldown if isinstance(error, _ModelUnavailable) else 120
            _cooldown_until[attempt.model] = time.time() + cooldown
            detail = str(error) if isinstance(error, _ModelUnavailable) else f"tarmoq xatosi ({type(error).__name__})"
            return f"{attempt.model}: {detail}"

        active: list[_Attempt] = []
        tried: set[str] = set()
        last_error = "noma'lum xato"
        launches = 0
        try:
            while True:
                now = time.time()
                ready = [m for m in chain if m not in tried and _cooldown_until.get(m, 0) <= now]
                good = _last_good.get(light)
                if good and good[0] in ready and now - good[1] < STICKY_SECONDS:
                    ready.remove(good[0])
                    ready.insert(0, good[0])
                if not active:
                    if not ready and tried:
                        tried.clear()  # everything failed once: start a new round after cooldowns
                        continue
                    if not ready:
                        wait = min(_cooldown_until.get(m, 0) for m in chain) - now
                        if wait > 90 or launches >= MAX_ATTEMPTS:
                            break
                        await on_notice(f"Modellar band, {int(wait) + 1} soniya kutilyapti…")
                        await asyncio.sleep(max(wait, 0) + 0.5)
                        continue
                if launches >= MAX_ATTEMPTS and not active:
                    break
                hedge_delay = None
                if ready and len(active) < HEDGE_WIDTH and launches < MAX_ATTEMPTS:
                    nxt = ready[0]
                    hedge_delay = max(0.0, active[-1].launched + HEDGE_DELAY - now) if active else 0
                    if hedge_delay == 0:
                        attempt = _Attempt(nxt, on_delta)
                        attempt.launched = now
                        attempt.task = asyncio.create_task(call(attempt))
                        active.append(attempt)
                        tried.add(nxt)
                        launches += 1
                        if len(active) > 1:
                            await on_notice(f"{active[0].model} sekin, {nxt} ham ishga tushirildi…")
                        continue
                waiters = [a.task for a in active] + [asyncio.create_task(a.started.wait()) for a in active]
                done, _ = await asyncio.wait(waiters, timeout=hedge_delay, return_when=asyncio.FIRST_COMPLETED)
                for w in waiters[len(active):]:
                    w.cancel()
                # drop attempts that failed before producing text
                for attempt in list(active):
                    if attempt.task.done() and attempt.task.exception() is not None:
                        error = attempt.task.exception()
                        if isinstance(error, LLMError):
                            raise error
                        last_error = fail(attempt, error)
                        active.remove(attempt)
                        await on_notice(f"{attempt.model} javob bermadi, boshqa modelga o'tilyapti…")
                winner = next((a for a in active if a.started.is_set() or a.task.done()), None)
                if not winner:
                    continue
                for attempt in active:
                    if attempt is not winner:
                        attempt.task.cancel()
                active = [winner]
                await winner.promote()
                try:
                    result = await winner.task
                except LLMError:
                    raise
                except (_ModelUnavailable, httpx.TimeoutException, httpx.TransportError) as error:
                    last_error = fail(winner, error)
                    active = []
                    if on_reset:
                        await on_reset()
                    await on_notice(f"{winner.model} uzilib qoldi, qaytadan boshlanmoqda…")
                    continue
                if not (good and good[0] == winner.model):
                    _last_good[light] = (winner.model, now)
                return result
        finally:
            for attempt in active:
                if attempt.task and not attempt.task.done():
                    attempt.task.cancel()
        raise LLMError(f"AI modellari javob bermadi. Oxirgi xato: {last_error}")
