"""Task-aware provider routing for model calls."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from carrel.config import DEFAULT_MAX_TOKENS, ModelConfig, TaskModelConfig
from carrel.model.base import Capabilities, ModelError, ModelSchemaError, ModelReply, build_backend, _fold_spend
from carrel.providers import ProviderConfig, RouteConfig, secret_header


class ProviderRouter:
    """Dispatch calls by task and atomically replace routes at runtime."""

    protocol = "router"

    def __init__(
        self,
        providers: dict[str, ProviderConfig] | None = None,
        routes: dict[str, RouteConfig] | None = None,
        *,
        fallback_config: ModelConfig | None = None,
        caps_path: Path | None = None,
        default_provider: str = "",
        backend_factory: Callable[..., Any] = build_backend,
    ) -> None:
        self._lock = threading.RLock()
        self._runtime = None
        self._caps_path = caps_path
        self._fallback_config = fallback_config or ModelConfig()
        self._backend_factory = backend_factory
        self._providers: dict[str, ProviderConfig] = {}
        self._routes: dict[str, RouteConfig] = {}
        self._backends: dict[str, Any] = {}
        self._default_provider = ""
        self.update(providers or {}, routes or {}, default_provider=default_provider)

    @classmethod
    def from_config(cls, config, *, caps_path: Path | None = None) -> "ProviderRouter":
        return cls(
            config.providers,
            config.task_routes,
            fallback_config=config.model,
            caps_path=caps_path,
            default_provider=getattr(config, "default_provider", ""),
        )

    @property
    def name(self) -> str:
        return self.name_for("")

    def name_for(self, task: str = "") -> str:
        backend, provider_id = self._for(task)
        model = self._model_for(backend, task)
        return f"{provider_id}:{model}"

    def _model_for(self, backend, task):
        resolved = getattr(backend, "by_task", {}).get(task)
        return (resolved[0] if resolved else "") or getattr(backend, "model", self._fallback_config.name)

    def provider_for(self, task: str = "") -> str:
        with self._lock:
            route = self._routes.get(task) or (self._routes.get('preview') if task == 'title' else None)
            return (route.provider if route else "") or self._default_provider or "legacy"

    @property
    def providers(self) -> dict[str, ProviderConfig]:
        with self._lock:
            return deepcopy(self._providers)

    @property
    def routes(self) -> dict[str, RouteConfig]:
        with self._lock:
            return deepcopy(self._routes)

    @property
    def default_provider(self) -> str:
        with self._lock:
            return self._default_provider

    def update(
        self,
        providers: dict[str, ProviderConfig],
        routes: dict[str, RouteConfig],
        *,
        default_provider: str = "",
    ) -> None:
        new_providers = deepcopy(providers)
        new_routes = deepcopy(routes)
        new_default = default_provider
        new_backends = self._build_backends(new_providers, new_routes, new_default)
        with self._lock:
            self._providers = new_providers
            self._routes = new_routes
            self._default_provider = new_default
            self._backends = new_backends
            if self._runtime:
                self.configure_runtime(self._runtime)

    def _build_backends(
        self,
        providers: dict[str, ProviderConfig],
        routes: dict[str, RouteConfig],
        default_provider: str,
    ) -> dict[str, Any]:
        routes = dict(routes)
        if 'title' not in routes and 'preview' in routes:
            routes['title'] = routes['preview']
        result: dict[str, Any] = {
            "legacy": self._backend_factory(
                replace(self._fallback_config, by_task={
                    **self._fallback_config.by_task,
                    **{task: TaskModelConfig(name=r.name or self._fallback_config.for_task(task)[0],
                                            thinking=r.thinking or self._fallback_config.for_task(task)[1],
                                            max_tokens=r.max_tokens or self._fallback_config.for_task(task)[2])
                       for task, r in routes.items() if not r.provider and not default_provider},
                }), caps_path=self._caps_path,
            )
        }
        for provider_id, provider in providers.items():
            by_task = {
                task: TaskModelConfig(
                    name=route.name,
                    thinking=route.thinking,
                    max_tokens=route.max_tokens,
                )
                for task, route in routes.items()
                if (route.provider or default_provider) == provider_id
            }
            cfg = ModelConfig(
                protocol=provider.protocol,
                base_url=provider.base_url,
                resolved_api_key=provider.effective_api_key,
                name=provider.model or self._fallback_config.name,
                thinking=provider.thinking or self._fallback_config.thinking,
                timeout_s=provider.timeout_s or self._fallback_config.timeout_s,
                max_retries=provider.max_retries,
                pricing=provider.pricing,
                by_task=by_task,
                extra_headers=dict(provider.extra_headers),
            )
            # Separate caches avoid concurrent providers overwriting each other's probes.
            caps = self._caps_path.with_name(f"model_caps_{provider_id}.json") if self._caps_path else None
            result[provider_id] = self._backend_factory(cfg, caps_path=caps)
        return result

    def configure_runtime(self, runtime):
        with self._lock:
            self._runtime = runtime
            for name, backend in self._backends.items():
                provider = self._providers.get(name)
                backend.resource = runtime.pool('llm:' + name,
                    provider.max_concurrency if provider else 12,
                    minimum=provider.min_concurrency if provider else 1, adaptive=True)
                backend.resource.configure(provider.max_concurrency if provider else 12,
                                           provider.min_concurrency if provider else 1)
                backend.retry_policy = runtime.policy

    def adopt(self, prepared: "ProviderRouter") -> None:
        """Publish an already-built snapshot after its durable config was saved."""
        with self._lock:
            self._providers = prepared._providers
            self._routes = prepared._routes
            self._backends = prepared._backends
            self._default_provider = prepared._default_provider
            if self._runtime:
                self.configure_runtime(self._runtime)

    def _for(self, task: str = "") -> tuple[Any, str]:
        with self._lock:
            route = self._routes.get(task) or (self._routes.get('preview') if task == 'title' else None)
            provider_id = (route.provider if route else "") or self._default_provider or "legacy"
            if provider_id != "legacy" and (provider_id not in self._providers or not self._providers[provider_id].configured):
                backend = self._backends.get(provider_id)
                reply = ModelReply(text="", provider=provider_id, task=task,
                                   requested_model=self._model_for(backend, task) if backend else (route.name if route else ""))
                exc = ModelError(f"Provider {provider_id} 未配置可用的地址或密钥，请到设置页 /settings 检查。")
                exc.reply = reply
                raise exc
            return self._backends[provider_id], provider_id

    def backend_for_provider(self, provider_id: str):
        with self._lock:
            return self._backends[provider_id]

    def task_settings(self, task: str):
        backend, _ = self._for(task)
        return getattr(backend, "by_task", {}).get(task) or (
            self._model_for(backend, task), getattr(backend, "thinking", ""), DEFAULT_MAX_TOKENS.get(task, 0))

    def pin(self, task: str):
        """Keep a call_json retry and its accounting on one routing snapshot."""
        backend, provider_id = self._for(task)
        router = self

        class BoundBackend:
            def raw_structured(self, **kwargs):
                return router._invoke(backend, provider_id, "raw_structured", kwargs)

            def structured(self, **kwargs):
                return router._invoke(backend, provider_id, "structured", kwargs)

        return BoundBackend()

    @staticmethod
    def _annotate(reply: ModelReply, provider_id: str) -> ModelReply:
        reply.provider = provider_id
        return reply

    def _dispatch(self, method_name: str, kwargs: dict[str, Any]) -> ModelReply:
        backend, provider_id = self._for(kwargs.get("task", ""))
        return self._invoke(backend, provider_id, method_name, kwargs)

    def _invoke(self, backend, provider_id, method_name, kwargs):
        method = getattr(backend, method_name, None)
        if method is None and method_name in {"raw_structured", "structured_with_fallback"}:
            method = backend.structured
        try:
            return self._annotate(method(**kwargs), provider_id)
        except ModelError as exc:
            reply = getattr(exc, "reply", None)
            if reply is None:
                reply = ModelReply(text="", task=kwargs.get("task", ""),
                                   requested_model=self._model_for(backend, kwargs.get("task", "")))
                exc.reply = reply
            self._annotate(reply, provider_id)
            message = str(exc)
            secrets = [getattr(backend, "api_key", ""), *[v for k, v in getattr(backend, "_extra_headers", {}).items() if secret_header(k)]]
            for value in secrets:
                if value:
                    message = message.replace(value, "[redacted]")
            exc.args = (f"{message} [Provider: {provider_id}；检查设置页 /settings]",)
            raise

    def structured(self, **kwargs: Any) -> ModelReply:
        return self._dispatch("structured", kwargs)

    def raw_structured(self, **kwargs: Any) -> ModelReply:
        return self._dispatch("raw_structured", kwargs)

    def structured_with_fallback(self, **kwargs: Any) -> ModelReply:
        # Keep both attempts on one immutable routing snapshot during hot updates.
        backend, provider_id = self._for(kwargs.get("task", ""))
        try:
            return self._invoke(backend, provider_id, "structured", kwargs)
        except ModelSchemaError as first:
            wasted = getattr(first, "reply", None)
            strengthened = {**kwargs, "system": kwargs.get("system", "") +
                            "\nReturn only a JSON object matching this schema:\n" +
                            json.dumps(kwargs["schema"], ensure_ascii=False)}
            try:
                reply = self._invoke(backend, provider_id, "structured", strengthened)
            except ModelError as second:
                if wasted is not None:
                    _fold_spend(second.reply, wasted)
                raise
            if wasted is not None:
                _fold_spend(reply, wasted)
            return reply

    def text(self, **kwargs: Any) -> ModelReply:
        return self._dispatch("text", kwargs)

    def capabilities(
        self,
        *,
        task: str = "",
        model: str = "",
        thinking: str = "",
    ) -> Capabilities:
        backend, _ = self._for(task)
        resolved = getattr(backend, "by_task", {}).get(task)
        return backend.capabilities(model=model or self._model_for(backend, task),
                                    thinking=thinking or (resolved[1] if resolved else ""))
