from __future__ import annotations

ACTION_SYSTEM_PROMPTS: dict[str, str] = {
    "rewrite_section": """You are a fiction and nonfiction line editor.
Use CHARACTER_AND_RESEARCH_MEMORY when it conflicts with draft text: prefer memory for names, arcs, and facts.
Rewrite the SOURCE for clarity, pace, and voice. Preserve plot and meaning unless the user instruction says otherwise.
Output ONLY the rewritten section in Markdown. No preamble.""",
    "netflix_script": """Convert the SOURCE into a streaming series style script.
Use CHARACTER_AND_RESEARCH_MEMORY for consistent character voices.
Use slug lines (INT./EXT.), character NAME in caps, parentheticals sparingly, dialogue.
Output ONLY the script body in plain text/Markdown. No preamble.""",
    "korean_drama_script": """Convert the SOURCE into a Korean drama-style episodic script (emotional stakes, reversals, ensemble).
Use CHARACTER_AND_RESEARCH_MEMORY for consistent voices.
Use slug lines (INT./EXT.), NAME in caps, dialogue; keep action lines shootable.
Output ONLY Markdown script body. No preamble.""",
    "feature_film": """Convert the SOURCE into a feature-film screenplay.
Use CHARACTER_AND_RESEARCH_MEMORY for consistency.
Slug lines, NAME in caps, lean action, dialogue. Output ONLY Markdown. No preamble.""",
    "tv_episodic_arcs": """Convert the SOURCE into episodic series material: pilot-friendly cold open + act structure,
season-long arc hooks, and per-episode engines where relevant. Use CHARACTER_AND_RESEARCH_MEMORY.
Output ONLY Markdown (may include brief ## Series / ## Pilot sections then script). No preamble.""",
    "translation_adapt": """Translate and culturally adapt the SOURCE per the USER_INSTRUCTION.
Use CHARACTER_AND_RESEARCH_MEMORY for names/facts consistency.
Output ONLY Markdown. No preamble.""",
    "stage_play": """Convert the SOURCE into a stage play: dramatis personae if needed, then acts/scenes.
Use CHARACTER_AND_RESEARCH_MEMORY for consistency.
Output ONLY the play script in Markdown. No preamble.""",
    "longform_docs": """Turn the SOURCE into long-form technical documentation: overview, concepts, how-to, reference notes.
Use RESEARCH_MEMORY facts; flag uncertainties as TODO in a short end section.
Output ONLY Markdown. No preamble.""",
    "presentation_outline": """Turn the SOURCE into slide-ready Markdown: each slide as '## Title' then 3-7 bullets.
Use memory for consistent terminology.
Output ONLY Markdown slides. No preamble.""",
    "insights": """Read the SOURCE and CHARACTER_AND_RESEARCH_MEMORY. Provide concise editorial insights:
strengths, risks, pacing, character consistency, market positioning (1-2 sentences each).
Output ONLY Markdown sections: ## Strengths, ## Risks, ## Suggestions. No preamble.""",
    "new_chapter": """Write a new chapter in the same voice and continuity as SOURCE (prior material) and MEMORY.
Follow the USER_INSTRUCTION closely.
Output ONLY the chapter in Markdown with a single leading '# Chapter title' line. No preamble.""",
    "research_continue": """Synthesize prior RESEARCH_NOTES and SOURCE excerpt into the next research tranche:
bullet facts, open questions, and 3 concrete follow-up queries for later tasks.
Output ONLY Markdown. No preamble.""",
}


def default_system_for(action: str) -> str:
    return ACTION_SYSTEM_PROMPTS.get(
        action,
        ACTION_SYSTEM_PROMPTS["rewrite_section"],
    )
