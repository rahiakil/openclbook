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
    supervisor_parallel_workers: int = 1
    ollama_num_ctx: int | None = None
    supervisor_edit_context_budget_chars: int | None = None
    supervisor_divide_sample_chars: int | None = None
    supervisor_verify_passes: int = 2
    # httpx read timeout for Ollama /api/chat (seconds). Large ollama_num_ctx + big models need more wall time.
    ollama_http_timeout_seconds: float = 600.0
    # full = all .memory/**/*.md in prompts; digest = truncated digest; none = omit memory text
    supervisor_memory_context: str = "digest"
    supervisor_memory_digest_max_chars: int = 12_000
    supervisor_memory_digest_per_file: int = 2_000
    # Optional: --prep-gate / --prep-resume (strategy + human Q&A + character/arc memory passes).
    # When True, --plan-gate requires a completed prep phase one (same --thread-id) unless --skip-prep-requirement.
    supervisor_enable_prep_passes: bool = False

    def edit_context_budget_chars(self) -> int:
        """Upper bound on edit user-message payload (memory + plan + chunk); drives LangGraph resplit."""
        if self.supervisor_edit_context_budget_chars is not None:
            return max(8000, int(self.supervisor_edit_context_budget_chars))
        n = int(self.ollama_num_ctx or 8192)
        return max(12_000, int(n * 3.2) - 14_000)

    def divide_llm_sample_chars(self) -> int:
        """How much of the manuscript the division-of-work LLM sees (full text is always split in Python)."""
        if self.supervisor_divide_sample_chars is not None:
            return max(4000, int(self.supervisor_divide_sample_chars))
        n = int(self.ollama_num_ctx or 8192)
        return max(10_000, int(n * 2) - 4000)

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

    def _opt_int(key: str, env_key: str) -> int | None:
        v = os.environ.get(env_key, "").strip()
        if v:
            try:
                return int(v)
            except ValueError:
                pass
        raw = cfg.get(key)
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    ollama_num_ctx = _opt_int("ollama_num_ctx", "OLLAMA_NUM_CTX")
    edit_budget = _opt_int("supervisor_edit_context_budget_chars", "SUPERVISOR_EDIT_CONTEXT_BUDGET_CHARS")
    divide_sample = _opt_int("supervisor_divide_sample_chars", "SUPERVISOR_DIVIDE_SAMPLE_CHARS")
    try:
        vpass = int(os.environ.get("SUPERVISOR_VERIFY_PASSES", cfg.get("supervisor_verify_passes", 2)))
    except (TypeError, ValueError):
        vpass = 2
    vpass = max(1, min(vpass, 4))

    raw_par = (os.environ.get("SUPERVISOR_PARALLEL_WORKERS") or "").strip()
    if raw_par:
        try:
            par = int(raw_par)
        except ValueError:
            par = 1
    else:
        try:
            par = int(cfg.get("supervisor_parallel_workers", 1))
        except (TypeError, ValueError):
            par = 1
    par = max(1, min(par, 16))

    def _tiered_ollama_http_timeout(ctx: int | None) -> float:
        n = int(ctx or 8192)
        if n <= 8192:
            return 600.0
        if n <= 32768:
            return 1800.0
        if n <= 65536:
            return 3600.0
        if n <= 131072:
            return 7200.0
        return 14_400.0

    def _parse_ollama_http_timeout() -> float | None:
        for key in ("BOOK_OLLAMA_HTTP_TIMEOUT", "OLLAMA_HTTP_TIMEOUT"):
            raw = (os.environ.get(key) or "").strip()
            if not raw:
                continue
            try:
                v = float(raw)
            except ValueError:
                continue
            return max(60.0, min(172_800.0, v))
        raw_c = cfg.get("ollama_http_timeout_seconds")
        if raw_c is None or raw_c == "":
            return None
        try:
            v = float(raw_c)
        except (TypeError, ValueError):
            return None
        return max(60.0, min(172_800.0, v))

    ollama_http_timeout = _parse_ollama_http_timeout() or _tiered_ollama_http_timeout(ollama_num_ctx)

    mem_ctx = str(cfg.get("supervisor_memory_context", "digest") or "digest").strip().lower()
    if mem_ctx not in ("full", "digest", "none", "off", "false", "0"):
        mem_ctx = "digest"
    if mem_ctx in ("off", "false", "0"):
        mem_ctx = "none"
    try:
        mem_digest_max = int(cfg.get("supervisor_memory_digest_max_chars", 12_000))
    except (TypeError, ValueError):
        mem_digest_max = 12_000
    mem_digest_max = max(2_000, min(mem_digest_max, 200_000))
    try:
        mem_digest_per = int(cfg.get("supervisor_memory_digest_per_file", 2_000))
    except (TypeError, ValueError):
        mem_digest_per = 2_000
    mem_digest_per = max(200, min(mem_digest_per, 50_000))

    raw_prep = cfg.get("supervisor_enable_prep_passes", False)
    if isinstance(raw_prep, str):
        enable_prep = raw_prep.strip().lower() in ("1", "true", "yes", "on")
    else:
        enable_prep = bool(raw_prep)

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
        supervisor_parallel_workers=par,
        ollama_num_ctx=ollama_num_ctx,
        supervisor_edit_context_budget_chars=edit_budget,
        supervisor_divide_sample_chars=divide_sample,
        supervisor_verify_passes=vpass,
        ollama_http_timeout_seconds=ollama_http_timeout,
        supervisor_memory_context=mem_ctx,
        supervisor_memory_digest_max_chars=mem_digest_max,
        supervisor_memory_digest_per_file=mem_digest_per,
        supervisor_enable_prep_passes=enable_prep,
    )
