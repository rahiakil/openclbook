from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


def _load_dotenv_cascade(workspace: Path) -> None:
    """Load ``.env`` from workspace, its parents, and cwd (first wins for each file path)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    roots: list[Path] = [workspace.resolve(), Path.cwd().resolve()]
    try:
        roots.extend(list(workspace.resolve().parents)[:12])
    except (OSError, ValueError):
        pass
    seen: set[Path] = set()
    for base in roots:
        try:
            p = (base / ".env").resolve()
        except (OSError, ValueError):
            continue
        if p in seen or not p.is_file():
            continue
        seen.add(p)
        load_dotenv(p, override=False)


@dataclass
class Settings:
    workspace: Path
    ollama_base_url: str
    ollama_model: str
    openclaw_gateway_url: str | None
    openclaw_gateway_token: str | None
    llm_provider: str
    anthropic_model: str
    anthropic_api_key: str | None
    anthropic_base_url: str
    anthropic_thinking: str
    anthropic_thinking_budget: int
    memory_dir_name: str = ".memory"
    manuscript_dir: str = "manuscript"
    sections_dir: str = "sections"
    outputs_dir: str = "outputs"
    todo_file: str = "todo.file"
    supervisor_max_chunk_chars: int = 10000

    @property
    def memory_root(self) -> Path:
        return self.workspace / self.memory_dir_name

    @property
    def characters_dir(self) -> Path:
        return self.memory_root / "characters"

    @property
    def research_dir(self) -> Path:
        return self.memory_root / "research"

    @property
    def todo_path(self) -> Path:
        return self.workspace / self.todo_file


def load_settings(workspace: Path, config_path: Path | None = None) -> Settings:
    ws = workspace.resolve()
    _load_dotenv_cascade(ws)

    cfg: dict = {}
    if config_path and config_path.is_file():
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    ollama = os.environ.get("OLLAMA_BASE_URL", cfg.get("ollama_base_url", "http://127.0.0.1:11434"))
    model = os.environ.get("OLLAMA_MODEL", cfg.get("ollama_model", "gemma4:26b"))
    gw = os.environ.get("OPENCLAW_GATEWAY_URL", cfg.get("openclaw_gateway_url") or "") or None
    tok = os.environ.get("OPENCLAW_GATEWAY_TOKEN", cfg.get("openclaw_gateway_token") or "") or None
    if gw == "":
        gw = None
    if tok == "":
        tok = None

    max_chunk = int(cfg.get("supervisor_max_chunk_chars", 10000))
    if max_chunk < 2000:
        max_chunk = 2000

    llm_provider = str(os.environ.get("LLM_PROVIDER", cfg.get("llm_provider", "ollama"))).strip().lower()
    if llm_provider in ("claude",):
        llm_provider = "anthropic"

    anthropic_model = str(os.environ.get("ANTHROPIC_MODEL", cfg.get("anthropic_model", "claude-sonnet-4-20250514"))).strip()
    anthropic_key = (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTRHOPIC_API_KEY") or ""
    ).strip() or None
    anthropic_base = str(
        os.environ.get("ANTHROPIC_BASE_URL", cfg.get("anthropic_base_url", "https://api.anthropic.com"))
    ).rstrip("/")

    ath = str(os.environ.get("ANTHROPIC_THINKING", cfg.get("anthropic_thinking", ""))).strip().lower()
    if not ath:
        mlow = anthropic_model.lower()
        ath = "adaptive" if ("sonnet-4" in mlow or "opus-4" in mlow or "mythos" in mlow) else "off"
    if ath in ("false", "0", "none"):
        ath = "off"

    try:
        ath_budget = int(os.environ.get("ANTHROPIC_THINKING_BUDGET", cfg.get("anthropic_thinking_budget", 10_000)))
    except (TypeError, ValueError):
        ath_budget = 10_000
    ath_budget = max(1024, min(ath_budget, 60_000))

    return Settings(
        workspace=ws,
        ollama_base_url=ollama.rstrip("/"),
        ollama_model=model,
        openclaw_gateway_url=gw,
        openclaw_gateway_token=tok,
        llm_provider=llm_provider if llm_provider in ("ollama", "anthropic") else "ollama",
        anthropic_model=anthropic_model,
        anthropic_api_key=anthropic_key,
        anthropic_base_url=anthropic_base,
        anthropic_thinking=ath if ath in ("off", "adaptive", "enabled") else "off",
        anthropic_thinking_budget=ath_budget,
        memory_dir_name=str(cfg.get("memory_dir_name", ".memory")),
        manuscript_dir=str(cfg.get("manuscript_dir", "manuscript")),
        sections_dir=str(cfg.get("sections_dir", "sections")),
        outputs_dir=str(cfg.get("outputs_dir", "outputs")),
        todo_file=str(cfg.get("todo_file", "todo.file")),
        supervisor_max_chunk_chars=max_chunk,
    )
