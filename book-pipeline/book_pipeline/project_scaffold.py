from __future__ import annotations

import re
from pathlib import Path

from book_pipeline.config import load_settings


def _slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:80] or "project"


def default_config_template() -> str:
    """Baseline config for a new studio project (Ollama + dirs)."""
    template = Path(__file__).resolve().parent.parent / "workspace" / "config.yaml"
    if template.is_file():
        return template.read_text(encoding="utf-8")
    return (
        "llm_provider: ollama\n"
        "# anthropic_model: claude-sonnet-4-20250514\n"
        "# anthropic_thinking: adaptive\n"
        "ollama_base_url: http://127.0.0.1:11434\n"
        "ollama_model: gemma4:26b\n"
        "memory_dir_name: .memory\n"
        "manuscript_dir: manuscript\n"
        "sections_dir: sections\n"
        "outputs_dir: outputs\n"
        "todo_file: todo.file\n"
    )


def create_project_workspace(base: Path) -> Path:
    """
    Create on-disk book workspace with config, dirs, and empty memory tree.

    ``base`` is the project directory (e.g. BOOK_STUDIO_PROJECTS_ROOT / slug); it must not exist yet.
    """
    root = base.resolve()
    if root.exists():
        raise FileExistsError(f"project path already exists: {root}")
    root.mkdir(parents=True, exist_ok=True)
    cfg = root / "config.yaml"
    cfg.write_text(default_config_template(), encoding="utf-8")

    for sub in (
        "manuscript",
        "sections",
        "outputs",
        ".memory/characters",
        ".memory/research",
        ".memory/agentic",
        ".pipeline",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)

    learn = root / ".memory" / "agentic" / "learnings.md"
    if not learn.is_file():
        learn.write_text(
            "# Agentic learnings\n\n"
            "Add durable notes the pipeline should treat as memory: tone rules, "
            "character facts, Netflix voice, what *not* to change between passes.\n",
            encoding="utf-8",
        )

    char_readme = root / ".memory" / "characters" / "README.md"
    if not char_readme.is_file():
        char_readme.write_text(
            "# Characters\n\nOne markdown file per character or group; loaded into every Ollama call.\n",
            encoding="utf-8",
        )

    _ = load_settings(root, cfg)  # validate yaml early
    return root


def allocate_slug_dir(projects_root: Path, name: str) -> tuple[str, Path]:
    """Return (slug, path) with numeric suffix if needed to avoid collisions."""
    projects_root.mkdir(parents=True, exist_ok=True)
    base_slug = _slugify(name)
    slug = base_slug
    n = 2
    while (projects_root / slug).exists():
        slug = f"{base_slug}-{n}"
        n += 1
    return slug, projects_root / slug
