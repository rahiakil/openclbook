"""Remove Project Gutenberg header/footer boilerplate so rewrites skip license blocks."""

from __future__ import annotations


def strip_project_gutenberg_boilerplate(text: str) -> str:
    """
    Drop PG wrapper text: everything through the ``*** START OF … GUTENBERG EBOOK`` line,
    and everything from ``*** END OF … GUTENBERG EBOOK`` through EOF.

    Safe if markers are missing (returns stripped original). Only matches the standard
    ``*** … ***`` sentinel lines so story prose mentioning “Gutenberg” is untouched.
    """
    t = (text or "").replace("\ufeff", "")
    if not t.strip():
        return t

    lines = t.splitlines(keepends=True)
    start_cut = 0
    for i, ln in enumerate(lines):
        u = ln.upper()
        if "***" in u and "START OF" in u and "PROJECT GUTENBERG" in u and "EBOOK" in u:
            start_cut = i + 1
            break

    body = "".join(lines[start_cut:])
    lines2 = body.splitlines(keepends=True)
    end_cut = len(lines2)
    for i, ln in enumerate(lines2):
        u = ln.upper()
        if "***" in u and "END OF" in u and "PROJECT GUTENBERG" in u and "EBOOK" in u:
            end_cut = i
            break

    return "".join(lines2[:end_cut]).strip()
