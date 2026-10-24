"""LLM client with an on-disk response cache and a token/cost counter.

The pipeline calls an LLM only where deterministic code cannot decide (image
evidence, multilingual intent). This module provides:

* ``LLMClient`` -- sends a text and/or image request to a provider and returns
  an ``LLMResponse``. Every accepted response is stored in a ``ResponseCache``
  keyed by a hash of everything that influences the answer (provider, model,
  prompts, image bytes, sampling parameters), so a second run with the same
  inputs makes **zero** provider calls and needs no API key.
* ``UsageTracker`` -- counts live calls and tokens, and separately the calls
  that were served from the cache (with the tokens they originally cost), so
  usage reports can be produced for either kind of run.
* ``LLMConfig.from_env()`` -- provider, model, and prices come from environment
  variables; the API key is read from the environment (or an uncommitted
  ``.env`` file at the repository root) and is never logged or cached.

Only the standard library is used (``urllib`` for HTTP) so no extra install is
needed. Supported providers: ``anthropic`` (Messages API), ``openai`` (Chat
Completions API) and ``groq`` (OpenAI-compatible Chat Completions API).

Environment variables
---------------------
``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` / ``GROQ_API_KEY``  provider credentials.
``LLM_PROVIDER``     ``anthropic``, ``openai`` or ``groq`` (default: whichever key is set).
``LLM_MODEL``        model identifier (provider-specific default otherwise).
``LLM_CACHE_DIR``    cache directory (default: ``<repo>/.llm_cache``).
``LLM_PRICE_INPUT_PER_MTOK`` / ``LLM_PRICE_OUTPUT_PER_MTOK``  USD per million
                     tokens, overriding the built-in price table.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = REPO_ROOT / ".llm_cache"

# Bumped when the cache entry layout or the cache-key recipe changes.
CACHE_FORMAT_VERSION = 1

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
PROVIDER_GROQ = "groq"
PROVIDERS = (PROVIDER_ANTHROPIC, PROVIDER_OPENAI, PROVIDER_GROQ)

DEFAULT_MODELS: Mapping[str, str] = {
    PROVIDER_ANTHROPIC: "claude-sonnet-4-5",
    PROVIDER_OPENAI: "gpt-4o",
    PROVIDER_GROQ: "qwen/qwen3.8-27b",
}

API_KEY_VARIABLES: Mapping[str, str] = {
    PROVIDER_ANTHROPIC: "ANTHROPIC_API_KEY",
    PROVIDER_OPENAI: "OPENAI_API_KEY",
    PROVIDER_GROQ: "GROQ_API_KEY",
}

# USD per million tokens (input, output). Override with the LLM_PRICE_* variables.
PRICE_TABLE: Mapping[str, tuple[float, float]] = {
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}

SUPPORTED_IMAGE_TYPES: Mapping[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024

T = TypeVar("T")


class LLMError(RuntimeError):
    """Base class for LLM client failures."""


class LLMUnavailableError(LLMError):
    """No cached answer exists and no API key / provider is configured."""


class LLMRequestError(LLMError):
    """The provider call failed after retries."""


class LLMResponseError(LLMError):
    """The provider answered, but the answer was rejected by the caller."""


# ---------------------------------------------------------------------------
# Environment handling
# ---------------------------------------------------------------------------


def load_dotenv(path: Path | None = None) -> None:
    """Load ``KEY=VALUE`` lines from ``.env`` without overriding the environment."""
    target = path or REPO_ROOT / ".env"
    if not target.is_file():
        return
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Provider selection and sampling settings (never contains the API key)."""

    provider: str
    model: str
    cache_dir: Path = DEFAULT_CACHE_DIR
    temperature: float = 0.0
    timeout_s: float = 120.0
    max_attempts: int = 6
    price_input_per_mtok: float | None = None
    price_output_per_mtok: float | None = None

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError(f"unsupported LLM provider {self.provider!r}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LLMConfig:
        if env is None:
            load_dotenv()
            env = os.environ
        provider = env.get("LLM_PROVIDER", "").strip().lower()
        if not provider:
            if env.get("ANTHROPIC_API_KEY"):
                provider = PROVIDER_ANTHROPIC
            elif env.get("OPENAI_API_KEY"):
                provider = PROVIDER_OPENAI
            elif env.get("GROQ_API_KEY"):
                provider = PROVIDER_GROQ
            else:
                provider = PROVIDER_ANTHROPIC
        model = env.get("LLM_MODEL", "").strip() or DEFAULT_MODELS[provider]
        return cls(
            provider=provider,
            model=model,
            cache_dir=Path(env.get("LLM_CACHE_DIR") or DEFAULT_CACHE_DIR),
            price_input_per_mtok=_optional_float(env.get("LLM_PRICE_INPUT_PER_MTOK")),
            price_output_per_mtok=_optional_float(env.get("LLM_PRICE_OUTPUT_PER_MTOK")),
        )

    @property
    def api_key_variable(self) -> str:
        return API_KEY_VARIABLES[self.provider]

    def prices(self) -> tuple[float, float] | None:
        """USD per million (input, output) tokens, or ``None`` when unknown."""
        if (
            self.price_input_per_mtok is not None
            and self.price_output_per_mtok is not None
        ):
            return (self.price_input_per_mtok, self.price_output_per_mtok)
        return PRICE_TABLE.get(self.model)


def _optional_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    return float(value)


# ---------------------------------------------------------------------------
# Requests, responses and usage
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImageInput:
    """An image attached to a request."""

    data: bytes
    media_type: str

    @classmethod
    def from_path(cls, path: Path | str) -> ImageInput:
        file = Path(path)
        media_type = SUPPORTED_IMAGE_TYPES.get(file.suffix.lower())
        if media_type is None:
            raise ValueError(f"unsupported image type: {file.suffix!r}")
        data = file.read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"{file.name}: image exceeds {MAX_IMAGE_BYTES} bytes")
        return cls(data=data, media_type=media_type)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """One model call. ``purpose`` is a label for usage reports only."""

    prompt: str
    system: str = ""
    images: tuple[ImageInput, ...] = ()
    max_tokens: int = 4096
    purpose: str = ""

    def fingerprint(self) -> dict[str, Any]:
        """Everything that influences the answer, without raw image bytes."""
        return {
            "system": self.system,
            "prompt": self.prompt,
            "images": [[img.media_type, img.sha256] for img in self.images],
            "max_tokens": self.max_tokens,
        }


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    usage: TokenUsage
    provider: str
    model: str
    cached: bool = False


@dataclass(slots=True)
class ModelUsage:
    """Aggregated usage of one ``(provider, model)`` pair."""

    provider: str
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_calls: int = 0  # answers served from the cache
    cached_input_tokens: int = 0  # tokens those answers originally cost
    cached_output_tokens: int = 0
    cost_usd: float | None = 0.0  # None when the model has no known price
    cached_cost_usd: float | None = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class UsageTracker:
    """Thread-safe counter of live calls, cached replays, tokens and cost."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._models: dict[tuple[str, str], ModelUsage] = {}

    def record(
        self,
        config: LLMConfig,
        usage: TokenUsage,
        *,
        cached: bool,
    ) -> None:
        cost = _cost(config, usage)
        with self._lock:
            entry = self._models.setdefault(
                (config.provider, config.model),
                ModelUsage(config.provider, config.model),
            )
            if cached:
                entry.cached_calls += 1
                entry.cached_input_tokens += usage.input_tokens
                entry.cached_output_tokens += usage.output_tokens
                entry.cached_cost_usd = _add_cost(entry.cached_cost_usd, cost)
            else:
                entry.calls += 1
                entry.input_tokens += usage.input_tokens
                entry.output_tokens += usage.output_tokens
                entry.cost_usd = _add_cost(entry.cost_usd, cost)

    @property
    def calls(self) -> int:
        """Number of live provider calls made (cache hits excluded)."""
        with self._lock:
            return sum(m.calls for m in self._models.values())

    @property
    def cached_calls(self) -> int:
        with self._lock:
            return sum(m.cached_calls for m in self._models.values())

    def models(self) -> list[ModelUsage]:
        with self._lock:
            return [replace(m) for _, m in sorted(self._models.items())]

    def summary(self) -> dict[str, Any]:
        """JSON-serializable per-model and overall totals."""
        models = self.models()
        return {
            "models": [
                {
                    "provider": m.provider,
                    "model": m.model,
                    "calls": m.calls,
                    "input_tokens": m.input_tokens,
                    "output_tokens": m.output_tokens,
                    "total_tokens": m.total_tokens,
                    "cost_usd": m.cost_usd,
                    "cached_calls": m.cached_calls,
                    "cached_input_tokens": m.cached_input_tokens,
                    "cached_output_tokens": m.cached_output_tokens,
                    "cached_cost_usd": m.cached_cost_usd,
                }
                for m in models
            ],
            "total": {
                "calls": sum(m.calls for m in models),
                "input_tokens": sum(m.input_tokens for m in models),
                "output_tokens": sum(m.output_tokens for m in models),
                "cached_calls": sum(m.cached_calls for m in models),
            },
        }


def _cost(config: LLMConfig, usage: TokenUsage) -> float | None:
    prices = config.prices()
    if prices is None:
        return None
    return (usage.input_tokens * prices[0] + usage.output_tokens * prices[1]) / 1e6


def _add_cost(total: float | None, extra: float | None) -> float | None:
    if total is None or extra is None:
        return None
    return total + extra


# ---------------------------------------------------------------------------
# Response cache
# ---------------------------------------------------------------------------


class ResponseCache:
    """One JSON file per request hash under ``directory``; writes are atomic."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    @staticmethod
    def key(config: LLMConfig, request: LLMRequest) -> str:
        payload = {
            "format": CACHE_FORMAT_VERSION,
            "provider": config.provider,
            "model": config.model,
            "temperature": config.temperature,
            **request.fingerprint(),
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def load(self, key: str) -> LLMResponse | None:
        path = self._path(key)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            usage = entry["usage"]
            return LLMResponse(
                text=entry["response"],
                usage=TokenUsage(
                    int(usage["input_tokens"]), int(usage["output_tokens"])
                ),
                provider=entry["provider"],
                model=entry["model"],
                cached=True,
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            return None  # unreadable entry: treat as a miss and overwrite it

    def store(self, key: str, request: LLMRequest, response: LLMResponse) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "format": CACHE_FORMAT_VERSION,
            "provider": response.provider,
            "model": response.model,
            "purpose": request.purpose,
            "request": request.fingerprint(),
            "response": response.text,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }
        fd, tmp_name = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entry, handle, ensure_ascii=False, indent=1, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, self._path(key))
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def discard(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def __len__(self) -> int:
        return (
            len(list(self.directory.glob("*.json"))) if self.directory.is_dir() else 0
        )


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawCompletion:
    text: str
    usage: TokenUsage


class Transport(Protocol):
    """Sends one request to a provider; tests can supply a fake."""

    def send(self, config: LLMConfig, request: LLMRequest) -> RawCompletion: ...


_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
_MAX_RETRY_WAIT_S = 90.0
_TRY_AGAIN_RE = re.compile(r"try again in\s+(?:(\d+)m)?\s*([\d.]+)s", re.IGNORECASE)


def _retry_delay(attempt: int, headers: Any, detail: str) -> float:
    """Seconds to wait before retrying: server hint when given, else backoff."""
    backoff = min(2.0**attempt, 30.0)
    hinted: float | None = None
    value = headers.get("retry-after") if headers is not None else None
    if value:
        try:
            hinted = float(value)
        except ValueError:
            hinted = None
    if hinted is None:
        match = _TRY_AGAIN_RE.search(detail)
        if match:
            hinted = float(match.group(1) or 0) * 60 + float(match.group(2))
    if hinted is None:
        return backoff
    return min(max(hinted + 1.0, backoff), _MAX_RETRY_WAIT_S)


def _post_json(
    url: str,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    config: LLMConfig,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """POST ``body`` as JSON, retrying transient failures with backoff."""
    data = json.dumps(body).encode("utf-8")
    last_error: Exception | None = None
    delay = 0.0
    for attempt in range(config.max_attempts):
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "content-type": "application/json",
                "user-agent": "ghost-data-audit/1.0",
                **headers,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=config.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            last_error = LLMRequestError(f"HTTP {exc.code} from {url}: {detail}")
            if exc.code not in _RETRYABLE_STATUS:
                raise last_error from exc
            delay = _retry_delay(attempt, exc.headers, detail)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = LLMRequestError(f"network error calling {url}: {exc}")
            delay = _retry_delay(attempt, None, "")
        if attempt + 1 < config.max_attempts:
            sleep(delay)
    assert last_error is not None
    raise last_error


class AnthropicTransport:
    URL = "https://api.anthropic.com/v1/messages"
    VERSION = "2023-06-01"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def send(self, config: LLMConfig, request: LLMRequest) -> RawCompletion:
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": img.base64(),
                },
            }
            for img in request.images
        ]
        content.append({"type": "text", "text": request.prompt})
        body: dict[str, Any] = {
            "model": config.model,
            "max_tokens": request.max_tokens,
            "temperature": config.temperature,
            "messages": [{"role": "user", "content": content}],
        }
        if request.system:
            body["system"] = request.system
        payload = _post_json(
            self.URL,
            {"x-api-key": self._api_key, "anthropic-version": self.VERSION},
            body,
            config,
        )
        text = "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )
        usage = payload.get("usage", {})
        return RawCompletion(
            text,
            TokenUsage(
                int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
            ),
        )


class OpenAITransport:
    """Chat Completions transport; also serves OpenAI-compatible providers."""

    URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def send(self, config: LLMConfig, request: LLMRequest) -> RawCompletion:
        content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
        for img in request.images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{img.media_type};base64,{img.base64()}"
                    },
                }
            )
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": content})
        payload = _post_json(
            self.URL,
            {"authorization": f"Bearer {self._api_key}"},
            {
                "model": config.model,
                "messages": messages,
                "max_tokens": request.max_tokens,
                "temperature": config.temperature,
            },
            config,
        )
        choices = payload.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        usage = payload.get("usage", {})
        return RawCompletion(
            text,
            TokenUsage(
                int(usage.get("prompt_tokens", 0)),
                int(usage.get("completion_tokens", 0)),
            ),
        )


class GroqTransport(OpenAITransport):
    URL = "https://api.groq.com/openai/v1/chat/completions"


def build_transport(
    config: LLMConfig, env: Mapping[str, str] | None = None
) -> Transport:
    """Create the provider transport; the key is read from the environment."""
    source = os.environ if env is None else env
    api_key = source.get(config.api_key_variable, "").strip()
    if not api_key:
        raise LLMUnavailableError(
            f"no cached response and {config.api_key_variable} is not set "
            f"(provider={config.provider}); set it in the environment or in .env"
        )
    if config.provider == PROVIDER_ANTHROPIC:
        return AnthropicTransport(api_key)
    if config.provider == PROVIDER_GROQ:
        return GroqTransport(api_key)
    return OpenAITransport(api_key)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)


def parse_json_text(text: str) -> Any:
    """Parse a model answer as JSON, tolerating a surrounding code fence or prose."""
    stripped = text.strip()
    fenced = _FENCE_RE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start != -1 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


class LLMClient:
    """Cached, usage-counted access to one configured model."""

    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        transport: Transport | None = None,
        cache: ResponseCache | None = None,
        usage: UsageTracker | None = None,
    ) -> None:
        self.config = config or LLMConfig.from_env()
        self.cache = cache or ResponseCache(self.config.cache_dir)
        self.usage = usage or UsageTracker()
        self._transport = transport

    def _get_transport(self) -> Transport:
        if self._transport is None:
            self._transport = build_transport(self.config)
        return self._transport

    def complete(
        self,
        request: LLMRequest,
        accept: Callable[[str], T] | None = None,
    ) -> tuple[LLMResponse, T | None]:
        """Answer ``request`` from the cache, else from the provider.

        ``accept`` validates/parses the answer text. Only accepted answers are
        cached; a cached answer that ``accept`` rejects is discarded and
        re-requested. A rejected live answer is retried once, then raises
        ``LLMResponseError``. Returns ``(response, accept(response.text))``.
        """
        key = self.cache.key(self.config, request)
        hit = self.cache.load(key)
        if hit is not None:
            try:
                parsed = accept(hit.text) if accept else None
            except Exception:
                self.cache.discard(key)
            else:
                self.usage.record(self.config, hit.usage, cached=True)
                return hit, parsed

        last_error: Exception | None = None
        for _ in range(2):
            raw = self._get_transport().send(self.config, request)
            self.usage.record(self.config, raw.usage, cached=False)
            response = LLMResponse(
                raw.text, raw.usage, self.config.provider, self.config.model
            )
            try:
                parsed = accept(raw.text) if accept else None
            except Exception as exc:  # the caller decides what "valid" means
                last_error = exc
                continue
            self.cache.store(key, request, response)
            return response, parsed
        raise LLMResponseError(
            f"answer rejected twice ({request.purpose or 'request'}): {last_error}"
        )

    def complete_json(
        self,
        request: LLMRequest,
        validate: Callable[[Any], T] | None = None,
    ) -> tuple[LLMResponse, T | Any]:
        """Like ``complete`` for JSON answers; ``validate`` receives the parsed value."""

        def accept(text: str) -> Any:
            value = parse_json_text(text)
            return validate(value) if validate else value

        response, parsed = self.complete(request, accept)
        return response, parsed
