"""Model providers: OpenAI-compatible chat and OpenRouter-style decisions.

The union of Osprey Assist's and Kestrel's providers: every config key of both (``private``, ``extra_body``) and
Osprey's per-model cooldown. Stdlib only. Credentials are read at call time and never appear in errors or logs.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

_KEYS = {
    "kind", "base_url", "path", "api_key_file", "api_key_env", "allow_frames",
    "json_schema", "timeout_sec", "max_calls_per_hour", "max_calls_per_day", "headers",
    "private", "extra_body",
}
_DEFAULT_PATH = {"chat": "/chat/completions", "decisions": "/decisions"}


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class ModelReply:
    data: dict[str, Any]
    ms: float
    tokens_in: int | None
    tokens_out: int | None
    cost: float | None
    model: str


class Budget:
    """Sliding-window call budget (hour and day)."""

    def __init__(self, max_per_hour: int, max_per_day: int, clock: Callable[[], float] = time.time) -> None:
        self.max_per_hour, self.max_per_day, self._clock = max_per_hour, max_per_day, clock
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self._calls and self._calls[0] <= now - 86400:
            self._calls.popleft()

    def _counts(self) -> tuple[int, int]:
        now = self._clock()
        self._trim(now)
        return sum(1 for t in self._calls if t > now - 3600), len(self._calls)

    def allow(self) -> bool:
        with self._lock:
            h, d = self._counts()
            return h < self.max_per_hour and d < self.max_per_day

    def record(self) -> None:
        with self._lock:
            self._calls.append(self._clock())

    @property
    def calls_today(self) -> int:
        with self._lock:
            return self._counts()[1]

    @property
    def left(self) -> int:
        with self._lock:
            h, d = self._counts()
            return max(0, min(self.max_per_hour - h, self.max_per_day - d))


@dataclass(frozen=True)
class ProviderConfig:
    kind: str
    base_url: str
    path: str = ""
    api_key_file: str | None = None
    api_key_env: str | None = None
    allow_frames: bool = False
    json_schema: bool = False
    timeout_sec: float = 30
    max_calls_per_hour: int = 120
    max_calls_per_day: int = 2000
    headers: dict[str, str] = field(default_factory=dict)
    # ``private`` says pictures sent here stay private (local model, or a cloud route that denies data collection); the
    # library only carries it, the caller enforces it. ``extra_body`` is merged into every request, for example
    # OpenRouter's provider preferences.
    private: bool = False
    extra_body: dict[str, Any] = field(default_factory=dict)


def _bool(name: str, key: str, v: object) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"provider {name}: {key} must be a boolean")
    return v


def _int(name: str, key: str, v: object, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"provider {name}: {key} must be an integer in {lo}..{hi}")
    return v


def _extra(name: str, v: object) -> dict[str, Any]:
    if not isinstance(v, dict) or not all(isinstance(k, str) for k in v):
        raise ValueError(f"provider {name}: extra_body must be an object")
    return dict(v)


def parse_provider(name: str, raw: dict[str, Any]) -> ProviderConfig:
    if not isinstance(raw, dict):
        raise ValueError(f"provider {name}: must be an object")
    unknown = set(raw) - _KEYS
    if unknown:
        raise ValueError(f"provider {name}: unknown key(s) {sorted(unknown)}")
    kind = raw.get("kind")
    if kind not in _DEFAULT_PATH:
        raise ValueError(f"provider {name}: kind must be 'chat' or 'decisions'")
    base = raw.get("base_url")
    if not isinstance(base, str) or not base.startswith(("http://", "https://")):
        raise ValueError(f"provider {name}: base_url must be an http(s) URL")
    path = raw.get("path", _DEFAULT_PATH[kind])
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"provider {name}: path must start with '/'")
    strs: dict[str, str | None] = {}
    for k in ("api_key_file", "api_key_env"):
        v = raw.get(k)
        if v is not None and (not isinstance(v, str) or not v):
            raise ValueError(f"provider {name}: {k} must be a non-empty string")
        strs[k] = v
    json_schema = _bool(name, "json_schema", raw.get("json_schema", False))
    if json_schema and kind != "chat":
        raise ValueError(f"provider {name}: json_schema applies to chat only")
    t = raw.get("timeout_sec", 30)
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not 1 <= t <= 300:
        raise ValueError(f"provider {name}: timeout_sec must be 1..300")
    headers = raw.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ValueError(f"provider {name}: headers must map strings to strings")
    if any(k.lower() in ("authorization", "proxy-authorization") for k in headers):
        raise ValueError(f"provider {name}: headers must not carry auth")
    return ProviderConfig(
        kind=kind, base_url=base.rstrip("/"), path=path,
        api_key_file=strs["api_key_file"], api_key_env=strs["api_key_env"],
        allow_frames=_bool(name, "allow_frames", raw.get("allow_frames", False)),
        json_schema=json_schema, timeout_sec=float(t),
        max_calls_per_hour=_int(name, "max_calls_per_hour", raw.get("max_calls_per_hour", 120), 1, 100000),
        max_calls_per_day=_int(name, "max_calls_per_day", raw.get("max_calls_per_day", 2000), 1, 1000000),
        headers=dict(headers), private=_bool(name, "private", raw.get("private", False)),
        extra_body=_extra(name, raw.get("extra_body", {})),
    )


def _data_uri(img: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(img).decode("ascii")


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _int_or_none(v: object) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


_CAP_MARKERS: Final = ("no deployments available", "budget", "quota", "credits")


class _Base:
    def __init__(self, name: str, cfg: ProviderConfig, clock: Callable[[], float] = time.time) -> None:
        self.name, self.cfg = name, cfg
        self.budget = Budget(cfg.max_calls_per_hour, cfg.max_calls_per_day, clock)
        self._clock = clock
        # Per-model cooldown, so a capped or rate-limited route is not called again on every check:
        # a cap or budget answer waits for the next UTC day, other 429/5xx back off 60 s doubling to 1 h.
        self._cool: dict[str, tuple[float, float]] = {}  # model -> (until, last backoff seconds)
        self._cool_lock = threading.Lock()

    def _cooling(self, model: str) -> bool:
        with self._cool_lock:
            until = self._cool.get(model, (0.0, 0.0))[0]
        return self._clock() < until

    def _note(self, model: str, status: int | None, text: str) -> None:
        now = self._clock()
        with self._cool_lock:
            if status is None or status < 400:
                self._cool.pop(model, None)
                return
            capped = status == 402 or any(m in text.lower() for m in _CAP_MARKERS)
            if capped and status in (400, 402, 403, 429):
                self._cool[model] = (now - now % 86400 + 86400, 0.0)
            elif status == 429 or status >= 500:
                last = self._cool.get(model, (0.0, 0.0))[1]
                wait = min(3600.0, last * 2 if last else 60.0)
                self._cool[model] = (now + wait, wait)

    def _key(self) -> str | None:
        try:
            if self.cfg.api_key_file:
                return Path(self.cfg.api_key_file).read_text().strip() or None
        except OSError as exc:
            raise ProviderError("key file unreadable") from exc
        if self.cfg.api_key_env:
            return os.environ.get(self.cfg.api_key_env) or None
        return None

    def _post(self, body: dict[str, Any]) -> tuple[dict[str, Any], float]:
        model = str(body.get("model", ""))
        if self._cooling(model):
            raise ProviderError("cooling down")
        if not self.budget.allow():
            raise ProviderError("budget")
        self.budget.record()
        headers = {"Content-Type": "application/json", **self.cfg.headers}
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(
            self.cfg.base_url + self.cfg.path, data=json.dumps({**self.cfg.extra_body, **body}).encode(), headers=headers, method="POST",
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_sec) as r:
                raw = r.read()
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read(2048).decode("utf-8", "replace")
            except Exception:
                text = ""
            self._note(model, exc.code, text)
            raise ProviderError(f"http {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"network: {type(exc).__name__}") from None
        self._note(model, None, "")
        ms = (time.monotonic() - t0) * 1000
        try:
            obj = json.loads(raw)
        except ValueError:
            raise ProviderError("non-json response") from None
        if not isinstance(obj, dict):
            raise ProviderError("response is not an object")
        return obj, ms


class ChatProvider(_Base):
    def complete(self, model: str, system: str, user_text: str, images: list[bytes],
                 schema: dict[str, Any] | None) -> ModelReply:
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        content += [{"type": "image_url", "image_url": {"url": _data_uri(i)}} for i in images]
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "temperature": 0,
        }
        if self.cfg.json_schema and schema is not None:
            body["response_format"] = {
                "type": "json_schema", "json_schema": {"name": "raptor_vision", "strict": True, "schema": schema},
            }
        else:
            body["response_format"] = {"type": "json_object"}
        obj, ms = self._post(body)
        try:
            text = obj["choices"][0]["message"]["content"]
            data = json.loads(text[text.index("{"):text.rindex("}") + 1])
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("unparseable model content") from None
        if not isinstance(data, dict):
            raise ProviderError("model content is not an object")
        usage: dict[str, Any] = obj["usage"] if isinstance(obj.get("usage"), dict) else {}
        return ModelReply(data, ms, _int_or_none(usage.get("prompt_tokens")),
                          _int_or_none(usage.get("completion_tokens")), _num(usage.get("cost")),
                          str(obj.get("model") or model))


class DecisionsProvider(_Base):
    def decide(self, model: str, state: list[str | bytes], questions: dict[str, Any]) -> ModelReply:
        items: list[Any] = [
            s if isinstance(s, str) else {"type": "image_url", "image_url": {"url": _data_uri(s)}} for s in state
        ]
        obj, ms = self._post({"model": model, "state": items, "questions": questions})
        answers = obj.get("answers")
        if not isinstance(answers, dict):
            raise ProviderError("no answers")
        usage: dict[str, Any] = obj["usage"] if isinstance(obj.get("usage"), dict) else {}
        return ModelReply(answers, ms, _int_or_none(usage.get("prompt_tokens")),
                          _int_or_none(usage.get("completion_tokens")), _num(usage.get("cost")),
                          str(obj.get("model") or model))


def build_provider(name: str, cfg: ProviderConfig, clock: Callable[[], float] = time.time) -> ChatProvider | DecisionsProvider:
    return (ChatProvider if cfg.kind == "chat" else DecisionsProvider)(name, cfg, clock)
