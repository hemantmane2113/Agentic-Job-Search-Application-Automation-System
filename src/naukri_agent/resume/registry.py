"""
Resume registry: a catalog of the candidate's EXISTING, manually
created resume files (e.g. resumes/data_scientist.pdf), loaded from
config/resumes.yaml. Nothing in this module or resume/selector.py
generates, edits, or rewrites resume content — this is purely
"which of my real files applies to which kind of role", per the
revised Phase 6 architecture.
"""

from __future__ import annotations

import enum
import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_ALLOWED_EXTENSIONS = {".pdf", ".docx"}


class ResumeSelectionDecision(str, enum.Enum):
    SELECTED = "SELECTED"
    REVIEW = "REVIEW"


class ResumeMatchVia(str, enum.Enum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"


class ResumeRegistryEntry(BaseModel):
    id: str
    file: Path
    roles: list[str] = Field(default_factory=list)

    @field_validator("file")
    @classmethod
    def _extension_allowed(cls, v: Path) -> Path:
        if v.suffix.lower() not in _ALLOWED_EXTENSIONS:
            raise ValueError(
                f"Unsupported resume file type {v.suffix!r} for {v} "
                f"(allowed: {sorted(_ALLOWED_EXTENSIONS)})"
            )
        return v


class ResumeRegistry(BaseModel):
    resumes: list[ResumeRegistryEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicate_ids(self) -> "ResumeRegistry":
        seen: set[str] = set()
        for entry in self.resumes:
            if entry.id in seen:
                raise ValueError(f"Duplicate resume id in registry: {entry.id!r}")
            seen.add(entry.id)
        return self

    def get(self, resume_id: str) -> ResumeRegistryEntry | None:
        return next((e for e in self.resumes if e.id == resume_id), None)


def load_resume_registry(path: str | Path) -> ResumeRegistry:
    """
    Load and validate the resume registry from YAML. Raises
    FileNotFoundError with an actionable message if the file doesn't
    exist yet — the real registry is gitignored personal config, same
    pattern as candidate_profile.yaml / master_resume.yaml.

    Note this only validates the CONFIG (schema, duplicate ids,
    allowed extensions) — it does not check that the referenced files
    actually exist on disk. See check_registry_files for that, kept
    separate so config parsing doesn't require real files to be
    present (useful for tests, and correct: a config file is valid
    even if a referenced resume hasn't been placed yet).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Resume registry not found at {path}. Copy "
            "config/resumes.example.yaml to that path and list your "
            "own resume files."
        )
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return ResumeRegistry.model_validate(data)


def compute_file_hash(path: str | Path) -> str:
    """SHA-256 of a file's raw bytes, used to detect unexpected changes."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


class ResumeFileStatus(BaseModel):
    id: str
    file: Path
    exists: bool
    file_hash: str | None = None


def check_registry_files(registry: ResumeRegistry) -> list[ResumeFileStatus]:
    """
    Check every registry entry against the actual filesystem. This is
    the runtime counterpart to load_resume_registry's config-only
    validation — call this whenever you need to know whether a
    resume is actually usable right now (selection, doctor).
    """
    statuses = []
    for entry in registry.resumes:
        exists = entry.file.exists()
        file_hash = compute_file_hash(entry.file) if exists else None
        statuses.append(
            ResumeFileStatus(id=entry.id, file=entry.file, exists=exists, file_hash=file_hash)
        )
    return statuses
