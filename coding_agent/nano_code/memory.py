"""Bounded project notes and session facts backed by existing local records."""

import json
import re
from pathlib import Path

from changes import changed_paths
from execution_store import execution_facts

from .context_pruning import mask_credentials

WORDS = re.compile(r"[a-z][a-z0-9]{2,}|[\u3400-\u9fff]+", re.I)
STALE = re.compile(r"\[(?:stale|obsolete|过期|废弃)\b[^\]]*\]", re.I)


def _terms(text):
    terms = set()
    for word in WORDS.findall(text.lower()):
        if "\u3400" <= word[0] <= "\u9fff":
            terms.update(word[i:i + 2] for i in range(len(word) - 1))
        else:
            terms.add(word)
    return terms


def project_memory(root: Path, prompt: str) -> str:
    path = root / ".nano" / "MEMORY.md"
    try:
        if not path.resolve().is_relative_to(root) or not path.is_file():
            return ""
        with path.open(encoding="utf-8") as stream:
            text = stream.read(48000)
    except (OSError, UnicodeError, RuntimeError):
        return ""
    relevant = _terms(prompt[:8000])
    sections = re.split(r"(?m)(?=^#{1,6}\s+)", text)
    scored = []
    for index, section in enumerate(sections):
        title = section.split("\n", 1)[0]
        if STALE.search(title):
            continue
        score = 2 * len(relevant & _terms(title)) + len(relevant & _terms(section))
        if score:
            scored.append((-score, index, mask_credentials(section.strip())[:850]))
    return "\n\n".join(section for _, _, section in sorted(scored)[:3])


def working_memory(uid: str, tasks: str) -> str:
    paths = changed_paths(uid)
    verification, uncertain = execution_facts(uid)
    facts = {}
    if paths:
        facts["recorded_edit_paths"] = paths
    if verification:
        facts["latest_recorded_verification"] = verification
    if uncertain:
        facts["operations_to_recheck"] = uncertain
    if tasks and tasks != "暂无任务。":
        facts["agent_declared_checklist"] = tasks[:1200]
    return mask_credentials(json.dumps(facts, ensure_ascii=False)) if facts else ""
