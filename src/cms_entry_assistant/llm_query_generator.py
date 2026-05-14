from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

PROMPT_VERSION = "llm-query-v1"
ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
ANTHROPIC_API_KEY_ENV = ANTHROPIC_API_KEY

DEFAULT_MODEL = (
    os.getenv("CMS_ENTRY_ASSISTANT_ANTHROPIC_MODEL")
    or os.getenv("ANTHROPIC_MODEL")
    or "claude-3-5-haiku-latest"
)
DEFAULT_MAX_TOKENS = 1000
CACHE_SCHEMA_VERSION = 1

# codex Phase 3 must-fix: API timeout を必須にして長時間ブロックさせない。
# 1 案件あたり 4-7 slot × 1 リクエストになるので、20s × 6 = 最悪 2 分。
# DEFAULT_HTTP_TIMEOUT は anthropic SDK のリクエスト単位の timeout。
DEFAULT_HTTP_TIMEOUT = float(
    os.getenv("CMS_ENTRY_ASSISTANT_LLM_TIMEOUT") or "20.0"
)

CACHE_DIR_ENV = "CMS_ENTRY_ASSISTANT_LLM_CACHE_DIR"
CACHE_DISABLE_ENV = "CMS_ENTRY_ASSISTANT_DISABLE_LLM_CACHE"

_QUERY_KEYS = (
    "search_queries",
    "queries",
    "query",
    "search_query",
    "suggested_queries",
    "source_queries",
    "text",
    "value",
)
_JSON_BLOCK_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s*")

cache: MutableMapping[str, "QueryPlanResult"] = {}


class LlmUnavailableError(RuntimeError):
    """Raised when the LLM client cannot be used in this environment."""


@dataclass(frozen=True, init=False)
class LlmQueryPlan:
    search_queries: list[str]
    intent: str = ""
    keywords: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    rationale: str = ""
    confidence: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        search_queries: Sequence[str] | str | None = None,
        *,
        queries: Sequence[str] | str | None = None,
        intent: str = "",
        keywords: Sequence[str] | str | None = None,
        negative_keywords: Sequence[str] | str | None = None,
        rationale: str = "",
        confidence: float | str | None = None,
        metadata: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        if search_queries is None and queries is not None:
            search_queries = queries

        if not rationale and "reasoning" in extra:
            rationale = str(extra.pop("reasoning") or "")

        merged_metadata = _coerce_metadata(metadata)
        for key, value in extra.items():
            merged_metadata[str(key)] = _jsonable(value)

        object.__setattr__(self, "search_queries", _clean_queries(search_queries))
        object.__setattr__(self, "intent", str(intent or "").strip())
        object.__setattr__(self, "keywords", _clean_queries(keywords))
        object.__setattr__(self, "negative_keywords", _clean_queries(negative_keywords))
        object.__setattr__(self, "rationale", str(rationale or "").strip())
        object.__setattr__(self, "confidence", _coerce_confidence(confidence))
        object.__setattr__(self, "metadata", merged_metadata)

    @property
    def queries(self) -> list[str]:
        return list(self.search_queries)

    @property
    def reasoning(self) -> str:
        return self.rationale

    def to_dict(self) -> dict[str, Any]:
        return {
            "search_queries": list(self.search_queries),
            "intent": self.intent,
            "keywords": list(self.keywords),
            "negative_keywords": list(self.negative_keywords),
            "rationale": self.rationale,
            "confidence": self.confidence,
            "metadata": _jsonable(dict(self.metadata)),
        }


@dataclass(frozen=True, init=False)
class QueryPlanResult:
    slot_hash: str
    plan: LlmQueryPlan | None = None
    prompt_version: str = PROMPT_VERSION
    from_cache: bool = False
    raw_response: str | None = None
    error: str | None = None
    model: str | None = None
    cache_key: str | None = None

    def __init__(
        self,
        slot_hash: str = "",
        plan: LlmQueryPlan | Mapping[str, Any] | Sequence[str] | str | None = None,
        *,
        query_plan: LlmQueryPlan | Mapping[str, Any] | Sequence[str] | str | None = None,
        prompt_version: str = PROMPT_VERSION,
        from_cache: bool = False,
        raw_response: str | None = None,
        error: str | None = None,
        model: str | None = None,
        cache_key: str | None = None,
        **_: Any,
    ) -> None:
        if plan is None and query_plan is not None:
            plan = query_plan
        if plan is not None and not isinstance(plan, LlmQueryPlan):
            plan = parse_llm_response(plan)

        object.__setattr__(self, "slot_hash", str(slot_hash or ""))
        object.__setattr__(self, "plan", plan)
        object.__setattr__(self, "prompt_version", prompt_version)
        object.__setattr__(self, "from_cache", bool(from_cache))
        object.__setattr__(self, "raw_response", raw_response)
        object.__setattr__(self, "error", error)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "cache_key", cache_key)

    @property
    def query_plan(self) -> LlmQueryPlan | None:
        return self.plan

    @property
    def search_queries(self) -> list[str]:
        return self.plan.search_queries if self.plan else []

    @property
    def queries(self) -> list[str]:
        return self.search_queries

    @property
    def ok(self) -> bool:
        return self.plan is not None and self.error is None

    def copy(self, **changes: Any) -> "QueryPlanResult":
        values = {
            "slot_hash": self.slot_hash,
            "plan": self.plan,
            "prompt_version": self.prompt_version,
            "from_cache": self.from_cache,
            "raw_response": self.raw_response,
            "error": self.error,
            "model": self.model,
            "cache_key": self.cache_key,
        }
        values.update(changes)
        return QueryPlanResult(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_hash": self.slot_hash,
            "prompt_version": self.prompt_version,
            "from_cache": self.from_cache,
            "raw_response": self.raw_response,
            "error": self.error,
            "model": self.model,
            "cache_key": self.cache_key,
            "plan": self.plan.to_dict() if self.plan else None,
        }


def compute_slot_hash(slot: Any) -> str:
    payload = json.dumps(
        _jsonable(slot),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_llm_response(response: Any) -> LlmQueryPlan:
    data = response

    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8")

    if isinstance(data, LlmQueryPlan):
        return data

    if not isinstance(data, (str, Mapping, list, tuple)):
        data = _extract_response_text(data)

    if isinstance(data, str):
        data = _loads_json_from_text(data)

    if isinstance(data, Mapping):
        data = _unwrap_plan_mapping(data)

    if isinstance(data, Mapping):
        query_value = _first_present(data, _QUERY_KEYS)
        queries = _clean_queries(query_value)

        if not queries:
            for value in data.values():
                if isinstance(value, (Mapping, list, tuple)):
                    queries = _clean_queries(value)
                    if queries:
                        break

        if not queries:
            raise ValueError("LLM response did not contain search queries")

        known_keys = {
            *_QUERY_KEYS,
            "intent",
            "slot_intent",
            "objective",
            "summary",
            "keywords",
            "include_keywords",
            "positive_keywords",
            "negative_keywords",
            "exclude_keywords",
            "exclusions",
            "rationale",
            "reasoning",
            "explanation",
            "confidence",
        }

        return LlmQueryPlan(
            search_queries=queries,
            intent=str(
                _first_present(data, ("intent", "slot_intent", "objective", "summary"))
                or ""
            ).strip(),
            keywords=_clean_queries(
                _first_present(data, ("keywords", "include_keywords", "positive_keywords"))
            ),
            negative_keywords=_clean_queries(
                _first_present(data, ("negative_keywords", "exclude_keywords", "exclusions"))
            ),
            rationale=str(
                _first_present(data, ("rationale", "reasoning", "explanation")) or ""
            ).strip(),
            confidence=_coerce_confidence(data.get("confidence")),
            metadata={
                str(key): _jsonable(value)
                for key, value in data.items()
                if str(key) not in known_keys
            },
        )

    if isinstance(data, (list, tuple)):
        queries = _clean_queries(data)
        if not queries:
            raise ValueError("LLM response did not contain search queries")
        return LlmQueryPlan(search_queries=queries)

    raise ValueError("LLM response did not contain a JSON object or array")


def build_prompt(slot: Any, context: Any | None = None) -> str:
    payload: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION,
        "slot": _jsonable(slot),
    }
    if context is not None:
        payload["context"] = _jsonable(context)

    return (
        "Create a compact search-query plan for finding reliable source material "
        "for the CMS entry slot below.\n"
        "Return JSON only. Do not wrap the response in markdown.\n"
        "Treat slot_label / type_label / primary_query / rationale as the strongest "
        "signals. The article_title_hint applies to the whole article and should be "
        "used only as weak background context — never copy it into every query, or "
        "all slots will look identical.\n"
        "Use this schema:\n"
        "{\n"
        '  "intent": "short description of what must be found",\n'
        '  "keywords": ["important included terms"],\n'
        '  "negative_keywords": ["terms to avoid"],\n'
        '  "search_queries": ["query 1", "query 2"],\n'
        '  "rationale": "brief reason these queries should work",\n'
        '  "confidence": 0.0\n'
        "}\n\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)}"
    )


def generate_query_plan(
    slot: Any,
    context: Any | None = None,
    *,
    model: str | None = None,
    client: Any | None = None,
    api_key: str | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    use_cache: bool = True,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    raise_on_error: bool = False,
) -> QueryPlanResult:
    model = model or DEFAULT_MODEL
    slot_hash = compute_slot_hash(slot)
    cache_key = _cache_key(slot_hash, model=model, prompt_version=PROMPT_VERSION)

    if use_cache and not _cache_disabled():
        cached = load_cached_query_plan(
            slot_hash,
            model=model,
            cache_dir=cache_dir,
            prompt_version=PROMPT_VERSION,
        )
        if cached is not None:
            return cached

    raw_response: str | None = None

    try:
        prompt = build_prompt(slot, context=context)
        raw_response = request_llm_response(
            prompt,
            model=model,
            client=client,
            api_key=api_key,
            max_tokens=max_tokens,
        )
        plan = parse_llm_response(raw_response)
        result = QueryPlanResult(
            slot_hash=slot_hash,
            plan=plan,
            prompt_version=PROMPT_VERSION,
            from_cache=False,
            raw_response=raw_response,
            model=model,
            cache_key=cache_key,
        )
        if use_cache and not _cache_disabled():
            save_cached_query_plan(result, cache_dir=cache_dir)
        return result

    except LlmUnavailableError as exc:
        if raise_on_error:
            raise
        return QueryPlanResult(
            slot_hash=slot_hash,
            prompt_version=PROMPT_VERSION,
            raw_response=raw_response,
            error=str(exc),
            model=model,
            cache_key=cache_key,
        )

    except Exception as exc:
        if raise_on_error:
            raise
        return QueryPlanResult(
            slot_hash=slot_hash,
            prompt_version=PROMPT_VERSION,
            raw_response=raw_response,
            error=str(exc),
            model=model,
            cache_key=cache_key,
        )


def request_llm_query_plan(
    slot: Any,
    context: Any | None = None,
    *,
    model: str | None = None,
    client: Any | None = None,
    api_key: str | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    use_cache: bool = True,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> LlmQueryPlan:
    result = generate_query_plan(
        slot,
        context=context,
        model=model,
        client=client,
        api_key=api_key,
        cache_dir=cache_dir,
        use_cache=use_cache,
        max_tokens=max_tokens,
        raise_on_error=True,
    )
    if result.plan is None:
        raise LlmUnavailableError(result.error or "LLM query plan is unavailable")
    return result.plan


def request_llm_response(
    prompt: str,
    *,
    model: str | None = None,
    client: Any | None = None,
    api_key: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
) -> str:
    """codex Phase 3 must-fix: 明示的 timeout を必ず付与。

    `timeout` は anthropic SDK の per-request timeout (秒)。SDK が timeout kwarg
    を受け取らない古いバージョンの場合は TypeError で握って system 等と一緒に
    再試行する (fallback)。
    """
    model = model or DEFAULT_MODEL

    if client is None:
        client = _create_anthropic_client(api_key=api_key)

    messages_api = getattr(client, "messages", None)
    create = getattr(messages_api, "create", None)
    if create is None:
        raise LlmUnavailableError("Anthropic client does not expose messages.create")

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "timeout": timeout,
        "system": (
            "You generate precise, compact search query plans for CMS entry "
            "research. Return valid JSON only."
        ),
        "messages": [{"role": "user", "content": prompt}],
    }

    # codex Phase 4 監査: TypeError fallback は一気に落とさず段階的に。
    # timeout → system → temperature の順で 1 つずつ外して再試行する。
    # こうすると、たとえば SDK が timeout だけ受け取らないバージョンの場合に
    # system / temperature の signal を保ったまま継続できる。
    fallback_keys = ("timeout", "system", "temperature")
    response = None
    last_error: TypeError | None = None
    for attempt in range(len(fallback_keys) + 1):
        try:
            response = create(**kwargs)
            break
        except TypeError as exc:
            last_error = exc
            if attempt < len(fallback_keys):
                kwargs.pop(fallback_keys[attempt], None)
            else:
                raise
    if response is None and last_error is not None:
        raise last_error

    return _extract_response_text(response)


def load_cached_query_plan(
    slot_hash: str,
    *,
    model: str | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    prompt_version: str = PROMPT_VERSION,
) -> QueryPlanResult | None:
    model = model or DEFAULT_MODEL
    key = _cache_key(slot_hash, model=model, prompt_version=prompt_version)

    if key in cache:
        return cache[key].copy(from_cache=True)

    path = _cache_file_path(key, cache_dir=cache_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    if payload.get("slot_hash") != slot_hash:
        return None
    if payload.get("prompt_version") != prompt_version:
        return None

    plan_payload = payload.get("plan")
    if not plan_payload:
        return None

    try:
        plan = (
            LlmQueryPlan(**plan_payload)
            if isinstance(plan_payload, Mapping)
            else parse_llm_response(plan_payload)
        )
    except Exception:
        return None

    result = QueryPlanResult(
        slot_hash=slot_hash,
        plan=plan,
        prompt_version=prompt_version,
        from_cache=True,
        raw_response=payload.get("raw_response"),
        model=payload.get("model") or model,
        cache_key=key,
    )
    cache[key] = result.copy(from_cache=False)
    return result


def save_cached_query_plan(
    result: QueryPlanResult,
    *,
    cache_dir: str | os.PathLike[str] | None = None,
) -> None:
    if result.plan is None or result.error or _cache_disabled():
        return

    model = result.model or DEFAULT_MODEL
    key = result.cache_key or _cache_key(
        result.slot_hash,
        model=model,
        prompt_version=result.prompt_version,
    )

    cache[key] = result.copy(from_cache=False, cache_key=key)

    path = _cache_file_path(key, cache_dir=cache_dir)
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "prompt_version": result.prompt_version,
        "slot_hash": result.slot_hash,
        "model": model,
        "raw_response": result.raw_response,
        "plan": result.plan.to_dict(),
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(path)
    except OSError:
        return


def clear_cache() -> None:
    cache.clear()


def _create_anthropic_client(*, api_key: str | None = None) -> Any:
    key = api_key or os.getenv(ANTHROPIC_API_KEY_ENV)
    if not key:
        raise LlmUnavailableError("ANTHROPIC_API_KEY is not set")

    anthropic = _import_anthropic()
    try:
        return anthropic.Anthropic(api_key=key)
    except Exception as exc:
        raise LlmUnavailableError(f"Failed to create Anthropic client: {exc}") from exc


def _import_anthropic() -> Any:
    try:
        import anthropic  # type: ignore
    except ImportError as exc:
        raise LlmUnavailableError("The anthropic package is not installed") from exc
    return anthropic


def _extract_response_text(response: Any) -> str:
    if isinstance(response, str):
        return response

    if isinstance(response, Mapping):
        for key in ("content", "text", "output_text", "raw_response"):
            if key in response and response[key] is not None:
                return _extract_response_text(response[key])

    content = getattr(response, "content", None)
    if content is not None:
        if isinstance(content, str):
            return content
        if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                    continue
                if isinstance(block, Mapping):
                    text = block.get("text")
                    if text is not None:
                        parts.append(str(text))
                    continue
                text = getattr(block, "text", None)
                if text is not None:
                    parts.append(str(text))
            if parts:
                return "\n".join(parts)

    for attr in ("text", "output_text", "raw_response"):
        text = getattr(response, attr, None)
        if text is not None:
            return str(text)

    raise ValueError("LLM response did not contain text content")


def _loads_json_from_text(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Empty LLM response")

    candidates = [stripped]
    candidates.extend(match.group(1).strip() for match in _JSON_BLOCK_RE.finditer(text))

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (Mapping, list)):
            return value

    raise ValueError("LLM response did not contain valid JSON")


def _unwrap_plan_mapping(data: Mapping[str, Any]) -> Mapping[str, Any] | list[Any]:
    for key in ("query_plan", "plan", "result", "data"):
        nested = data.get(key)
        if isinstance(nested, str):
            try:
                nested = _loads_json_from_text(nested)
            except ValueError:
                continue

        if isinstance(nested, Mapping):
            merged = {str(k): v for k, v in data.items() if k != key}
            merged.update({str(k): v for k, v in nested.items()})
            return merged

        if isinstance(nested, list) and not _has_query_field(data):
            return nested

    return data


def _has_query_field(data: Mapping[str, Any]) -> bool:
    return any(key in data for key in _QUERY_KEYS)


def _first_present(data: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key not in data:
            continue
        value = data[key]
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            if len(value) == 0:
                continue
        return value
    return None


def _iter_query_strings(value: Any) -> Sequence[str]:
    if value is None:
        return []

    if isinstance(value, str):
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if len(lines) > 1:
            return [_BULLET_RE.sub("", line).strip() for line in lines]
        return [value]

    if isinstance(value, Mapping):
        for key in _QUERY_KEYS:
            if key in value:
                return list(_iter_query_strings(value[key]))
        return []

    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items: list[str] = []
        for item in value:
            items.extend(_iter_query_strings(item))
        return items

    return [str(value)]


def _clean_queries(value: Any) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []

    for item in _iter_query_strings(value):
        query = _BULLET_RE.sub("", str(item)).strip().strip("\"'")
        query = re.sub(r"\s+", " ", query).strip()
        if not query:
            continue

        normalized = query.casefold()
        if normalized in seen:
            continue

        seen.add(normalized)
        cleaned.append(query)

    return cleaned


def _coerce_confidence(value: float | int | str | None) -> float | None:
    if value is None:
        return None

    try:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.endswith("%"):
                score = float(text[:-1].strip()) / 100.0
            else:
                score = float(text)
        else:
            score = float(value)
    except (TypeError, ValueError):
        return None

    if 1.0 < score <= 100.0:
        score = score / 100.0

    return max(0.0, min(1.0, score))


def _coerce_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    return {str(key): _jsonable(value) for key, value in metadata.items()}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Path):
        return str(value)

    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))

    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(value[key])
            for key in sorted(value.keys(), key=lambda item: str(item))
        }

    if isinstance(value, (set, frozenset)):
        return [_jsonable(item) for item in sorted(value, key=repr)]

    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_jsonable(item) for item in value]

    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _jsonable(method())
            except TypeError:
                pass

    if hasattr(value, "__dict__"):
        public_items = {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
        if public_items:
            return _jsonable(public_items)

    return repr(value)


def _cache_key(
    slot_hash: str,
    *,
    model: str | None = None,
    prompt_version: str = PROMPT_VERSION,
) -> str:
    payload = f"{prompt_version}\n{model or ''}\n{slot_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_file_path(
    key: str,
    *,
    cache_dir: str | os.PathLike[str] | None = None,
) -> Path:
    return _default_cache_dir(cache_dir) / f"{key}.json"


def _default_cache_dir(cache_dir: str | os.PathLike[str] | None = None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir).expanduser()

    explicit = os.getenv(CACHE_DIR_ENV)
    if explicit:
        return Path(explicit).expanduser()

    xdg_cache_home = os.getenv("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser() / "cms_entry_assistant" / "llm_query_generator"

    return Path.home() / ".cache" / "cms_entry_assistant" / "llm_query_generator"


def _cache_disabled() -> bool:
    value = os.getenv(CACHE_DISABLE_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


generate_llm_query_plan = generate_query_plan
get_llm_query_plan = generate_query_plan
get_query_plan = generate_query_plan

__all__ = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY_ENV",
    "PROMPT_VERSION",
    "LlmUnavailableError",
    "LlmQueryPlan",
    "QueryPlanResult",
    "cache",
    "build_prompt",
    "clear_cache",
    "compute_slot_hash",
    "generate_llm_query_plan",
    "generate_query_plan",
    "get_llm_query_plan",
    "get_query_plan",
    "load_cached_query_plan",
    "parse_llm_response",
    "request_llm_query_plan",
    "request_llm_response",
    "save_cached_query_plan",
]

