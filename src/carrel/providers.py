"""User-local provider and task-route configuration."""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

from carrel.store.paths import atomic_write_text

SUPPORTED_PROTOCOLS = ("messages", "chat-completions", "responses")
THINKING_OPTIONS = ("", "disabled", "low", "medium", "high")


def secret_header(name: str) -> bool:
    return bool(re.search(r"authorization|key|token|secret|cookie", name, re.I))


def mask_secret(value: str) -> str:
    return "****" + (value[-4:] if len(value) > 4 else "") if value else ""


@dataclass(slots=True)
class ProviderConfig:
    id: str
    label: str = ""
    protocol: str = "chat-completions"
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    pricing: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    model: str = ""
    thinking: str = "low"
    timeout_s: int = 150
    max_retries: int = 1
    max_concurrency: int = 12
    min_concurrency: int = 1

    def __post_init__(self) -> None:
        self.id = self.id.strip()
        self.label = self.label.strip() or self.id
        self.protocol = self.protocol.strip().lower() or "chat-completions"
        self.base_url = self.base_url.strip().rstrip("/")
        self.api_key_env = self.api_key_env.strip()
        self.api_key = self.api_key.strip()
        self.model = self.model.strip()
        self.thinking = self.thinking.strip() or "low"
        self.extra_headers = {str(k): str(v) for k, v in (self.extra_headers or {}).items()}
        self.pricing = dict(self.pricing or {})

    @property
    def effective_api_key(self) -> str:
        return os.environ[self.api_key_env] if self.api_key_env in os.environ else self.api_key

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.effective_api_key)

    @property
    def masked_api_key(self) -> str:
        key = self.effective_api_key
        if not key:
            return ""
        suffix = key[-4:] if len(key) > 4 else ""
        return ("sk-" if key.startswith("sk-") else "") + "****" + suffix

    def public_dict(self) -> dict[str, Any]:
        return {**self.to_dict(), "api_key": self.masked_api_key, "configured": self.configured,
                "extra_headers": {k: mask_secret(v) if secret_header(k) else v for k, v in self.extra_headers.items()}}

    def redact(self, message: str) -> str:
        for value in (self.api_key, self.effective_api_key, *[v for k, v in self.extra_headers.items() if secret_header(k)]):
            if value:
                message = message.replace(value, "[redacted]")
        return message

    def validate(self) -> None:
        if (type(self.min_concurrency) is not int or type(self.max_concurrency) is not int
                or not 1 <= self.min_concurrency <= self.max_concurrency <= 64):
            raise ValueError('并发范围须满足 1 ≤ min_concurrency ≤ max_concurrency ≤ 64')
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.id) or self.id == "legacy":
            raise ValueError("ID 只能包含小写字母、数字、短横线或下划线，且不能为 legacy")
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError("不支持的协议")
        url = urlsplit(self.base_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("API 地址须为 http(s) 地址，不应包含用户名、密钥、查询参数或片段")
        if self.api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("密钥环境变量名称无效")
        if not 1 <= self.timeout_s <= 600 or not 0 <= self.max_retries <= 5:
            raise ValueError("超时须为 1–600 秒，重试次数须为 0–5")
        if self.thinking not in THINKING_OPTIONS:
            raise ValueError("不支持的思考强度")
        for key, value in self.extra_headers.items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or any(c in value for c in "\r\n"):
                raise ValueError("自定义请求头格式无效")
        def rates(data):
            for key, value in data.items():
                if key == "per_model" and isinstance(value, dict):
                    for item in value.values():
                        if not isinstance(item, dict):
                            raise ValueError("模型价格须为对象")
                        rates(item)
                elif key not in ("input", "output", "cache_read", "cache_write") or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                    raise ValueError("价格须为非负数，单位为美元 / 百万 token")
        rates(self.pricing)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "protocol": self.protocol,
            "base_url": self.base_url, "api_key": self.api_key,
            "api_key_env": self.api_key_env, "extra_headers": dict(self.extra_headers),
            "pricing": dict(self.pricing), "notes": self.notes, "model": self.model,
            "thinking": self.thinking,
            "timeout_s": self.timeout_s, "max_retries": self.max_retries,
            "max_concurrency": self.max_concurrency, "min_concurrency": self.min_concurrency,
        }


@dataclass(slots=True)
class RouteConfig:
    provider: str = ""
    name: str = ""
    thinking: str = ""
    max_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "name": self.name,
                "thinking": self.thinking, "max_tokens": self.max_tokens}


@dataclass(slots=True)
class ProviderRegistry:
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    routes: dict[str, RouteConfig] = field(default_factory=dict)
    default_provider: str = ""
    path: Path | None = None
    error: str = ""

    def public_providers(self) -> list[dict[str, Any]]:
        return [self.providers[key].public_dict() for key in sorted(self.providers)]

    def route_dict(self) -> dict[str, dict[str, Any]]:
        return {key: self.routes[key].to_dict() for key in sorted(self.routes)}

    def to_toml(self) -> str:
        lines = ["# User-local Carrel providers and task routes.", ""]
        for provider in self.providers.values():
            lines.append("[[provider]]")
            for key in ("id", "label", "protocol", "base_url", "api_key", "api_key_env",
                        "notes", "model", "thinking"):
                value = getattr(provider, key)
                if value:
                    lines.append(f"{key} = {_toml(value)}")
            lines.append(f"timeout_s = {int(provider.timeout_s)}")
            lines.append(f"max_retries = {int(provider.max_retries)}")
            lines.append(f"max_concurrency = {int(provider.max_concurrency)}")
            lines.append(f"min_concurrency = {int(provider.min_concurrency)}")
            if provider.extra_headers:
                lines.append(f"extra_headers = {_toml(provider.extra_headers)}")
            if provider.pricing:
                lines.append(f"pricing = {_toml(provider.pricing)}")
            lines.append("")
        if self.default_provider:
            lines += ["[defaults]", f"provider = {_toml(self.default_provider)}", ""]
        if self.routes:
            lines.append("[task]")
            lines.extend(f"{task} = {_toml(route.to_dict())}" for task, route in self.routes.items())
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def save(self, path: Path | str | None = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("provider registry path is not set")
        atomic_write_text(target, self.to_toml())
        self.path = target
        return target


def _toml(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml(str(k))} = {_toml(v)}"
                                 for k, v in value.items()) + " }"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def load_provider_registry(path: Path | str) -> ProviderRegistry:
    try:
        return _load_provider_registry(path)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
        # Keep the broken file intact; do not expose file values (possibly secrets).
        logging.getLogger(__name__).warning("Cannot load providers.toml (%s); using legacy model", type(exc).__name__)
        return ProviderRegistry(path=Path(path), error="providers.toml 格式有误，已回退旧模型配置；请检查该文件。")


def _load_provider_registry(path: Path | str) -> ProviderRegistry:
    path = Path(path)
    if not path.exists():
        atomic_write_text(path, (
            "# User-local Carrel providers and task routes.\n"
            "# Add provider entries below; API keys stay in this data directory.\n"
            "# Example:\n"
            "# [[provider]]\n"
            "# id = \"relay-gpt\"\n"
            "# label = \"GPT relay\"\n"
            "# protocol = \"responses\"\n"
            "# base_url = \"https://relay.example.com/v1\"\n"
            "# api_key_env = \"CARREL_RELAY_API_KEY\"\n"
        ))
    if tomllib is None or not path.is_file():
        return ProviderRegistry(path=path)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    providers = {}
    for item in raw.get("provider", []) or []:
        if not isinstance(item, dict) or not str(item.get("id", "")).strip():
            raise ValueError("invalid provider entry")
        p = ProviderConfig(
            id=str(item["id"]), label=str(item.get("label") or ""),
            protocol=str(item.get("protocol") or "chat-completions"),
            base_url=str(item.get("base_url") or ""), api_key=str(item.get("api_key") or ""),
            api_key_env=str(item.get("api_key_env") or ""),
            extra_headers=item.get("extra_headers") if isinstance(item.get("extra_headers"), dict) else {},
            pricing=item.get("pricing") if isinstance(item.get("pricing"), dict) else {},
            notes=str(item.get("notes") or ""), model=str(item.get("model") or ""),
            thinking=str(item.get("thinking", "low")),
            timeout_s=int(item.get("timeout_s", 150)), max_retries=int(item.get("max_retries", 1)),
            max_concurrency=int(item.get("max_concurrency", 12)), min_concurrency=int(item.get("min_concurrency", 1)),
        )
        p.validate()
        if p.id in providers:
            raise ValueError("duplicate provider id")
        providers[p.id] = p
    routes = {}
    for task, item in (raw.get("task") or {}).items():
        if not isinstance(item, dict):
            raise ValueError("invalid task entry")
        if isinstance(item, dict):
            routes[str(task)] = RouteConfig(
                provider=str(item.get("provider") or ""), name=str(item.get("name") or ""),
                thinking=str(item.get("thinking") or ""), max_tokens=int(item.get("max_tokens") or 0),
            )
    defaults = raw.get("defaults") or {}
    registry = ProviderRegistry(providers, routes,
                            str(defaults.get("provider") or "") if isinstance(defaults, dict) else "",
                            path)
    if registry.default_provider and registry.default_provider not in providers:
        raise ValueError("unknown default provider")
    from carrel.config import MODEL_TASKS
    for task, route in routes.items():
        if task not in MODEL_TASKS or (route.provider and route.provider not in providers) or route.thinking not in THINKING_OPTIONS or not 0 <= route.max_tokens <= 1000000:
            raise ValueError("invalid task route")
    return registry


load_providers = load_provider_registry


def save_provider_registry(registry: ProviderRegistry, path: Path | str | None = None) -> Path:
    return registry.save(path)
