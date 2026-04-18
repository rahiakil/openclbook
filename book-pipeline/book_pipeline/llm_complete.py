from __future__ import annotations

from pathlib import Path
from typing import Any

from book_pipeline.anthropic_client import anthropic_messages
from book_pipeline.config import Settings
from book_pipeline.ollama_client import ollama_chat


def resolve_llm_provider(settings: Settings, override: str | None) -> str:
    raw = (override or settings.llm_provider or "ollama").strip().lower()
    if raw in ("anthropic", "claude"):
        return "anthropic"
    return "ollama"


def complete_chat(
    settings: Settings,
    *,
    llm_provider_override: str | None,
    messages: list[dict[str, str]],
    temperature: float,
    workspace: Path,
    tag: str,
    ollama_num_ctx: int | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """
    Unified chat completion for supervisor / lab.

    Returns ``(assistant_text, thinking_markdown, usage_meta)``.
    ``thinking_markdown`` is empty for Ollama.
    """
    provider = resolve_llm_provider(settings, llm_provider_override)
    usage_dir = workspace / ".pipeline"
    usage_dir.mkdir(parents=True, exist_ok=True)

    if provider == "anthropic":
        key = (settings.anthropic_api_key or "").strip()
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set (add to .env or environment). "
                "See workspace config anthropic_model / llm_provider."
            )
        budget = max(1024, int(settings.anthropic_thinking_budget))
        if settings.anthropic_thinking.lower() != "off":
            max_tokens = max(16_000, budget + 8192)
        else:
            max_tokens = 16_000
        path = usage_dir / "anthropic_usage.jsonl"
        text, thinking, usage = anthropic_messages(
            api_key=key,
            base_url=settings.anthropic_base_url,
            model=settings.anthropic_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking_mode=settings.anthropic_thinking,
            thinking_budget=budget,
            usage_log_path=path,
        )
        meta = {"provider": "anthropic", "tag": tag, **usage}
        return text, thinking, meta

    path = usage_dir / "ollama_usage.jsonl"
    text = ollama_chat(
        settings.ollama_base_url,
        settings.ollama_model,
        messages,
        temperature=temperature,
        num_ctx=ollama_num_ctx,
        usage_log_path=path,
    )
    return text, "", {"provider": "ollama", "tag": tag}
