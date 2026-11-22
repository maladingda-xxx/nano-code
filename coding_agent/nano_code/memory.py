"""Bounded project notes and session facts backed by existing local records."""

import json
import posixpath
import re
from pathlib import Path

from changes import changed_paths
from execution_store import execution_facts

from .context_pruning import mask_credentials

WORDS = re.compile(r"[a-z][a-z0-9]{2,}|[\u3400-\u9fff]+", re.I)
STALE = re.compile(r"\[(?:stale|obsolete|过期|废弃)\b[^\]]*\]", re.I)
METADATA = re.compile(r"^\s*(?:-\s+)?(description|paths|updated|描述|适用路径|更新日期)\s*[:：]\s*(.+)$", re.I)
PATH_PART = r"[\w.-]+"
PROMPT_PATH = re.compile(
    rf"(?:(?:[a-z]:)?[/\\])?{PATH_PART}(?:[/\\]{PATH_PART})+|[\w-]+(?:\.[\w-]+)+", re.I,
)


def _terms(text):
    terms = set()
    for word in WORDS.findall(text.lower()):
        if "\u3400" <= word[0] <= "\u9fff":
            terms.update(word[i:i + 2] for i in range(len(word) - 1))
        else:
            terms.add(word)
    return terms


def _normalize_path(path: str) -> str:
    return posixpath.normpath(path.strip().strip("`").replace("\\", "/")).casefold()


def _memory_header(section: str) -> tuple[str, list[str], str]:
    description, scopes = "", []
    body = section.lstrip("\n").splitlines()
    for index, line in enumerate(body):
        match = METADATA.fullmatch(line)
        if match is None:
            return description, scopes, "\n".join(body[index:])
        field, value = match[1].lower(), match[2]
        if field in {"description", "描述"}:
            description = value
        elif field in {"paths", "适用路径"}:
            scopes.extend(_normalize_path(part) for part in re.split(r"[,，]", value) if part.strip())
    return description, scopes, ""


def project_memory(root: Path, prompt: str) -> str:
    path = root / ".nano" / "MEMORY.md"
    try:
        if not path.resolve().is_relative_to(root) or not path.is_file():
            return ""
        with path.open(encoding="utf-8") as stream:
            text = stream.read(48000)
    except (OSError, UnicodeError, RuntimeError):
        return ""
    prompt = prompt[:8000]
    relevant = _terms(prompt)
    root_path = _normalize_path(root.as_posix()).rstrip("/") + "/"
    targets = {_normalize_path(value.rstrip(".")).removeprefix(root_path)
               for value in PROMPT_PATH.findall(prompt)}
    sections = re.split(r"(?m)(?=^#{1,6}[ \t]+)", text)
    scored = []
    for index, section in enumerate(sections):
        title, _, remainder = section.partition("\n")
        if STALE.search(title):
            continue
        description, scopes, body = _memory_header(remainder)
        scoped = any(target == scope or target.startswith(scope + "/") for scope in scopes for target in targets)
        title_terms = _terms(title)
        score = 2 * len(relevant & title_terms) + len(relevant & (title_terms | _terms(body)))
        score += 3 * len(relevant & _terms(description))
        if not targets:
            score += 2 * len(relevant & _terms(" ".join(scopes)))
        if scoped or score:
            scored.append((not scoped, -score, index, mask_credentials(section.strip())[:850]))
    return "\n\n".join(section for _, _, _, section in sorted(scored)[:3])


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
