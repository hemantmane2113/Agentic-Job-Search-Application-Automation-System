import hashlib
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from naukri_agent.resume.registry import (
    ResumeRegistry,
    check_registry_files,
    compute_file_hash,
    load_resume_registry,
)


def _write_registry(tmp_path, resumes: list[dict]) -> Path:
    path = tmp_path / "resumes.yaml"
    path.write_text(yaml.dump({"resumes": resumes}))
    return path


def _make_file(tmp_path, name: str, content: bytes = b"dummy pdf content") -> str:
    file_path = tmp_path / name
    file_path.write_bytes(content)
    return str(file_path)


# --- Loading ---


def test_load_valid_registry(tmp_path):
    file1 = _make_file(tmp_path, "data_scientist.pdf")
    path = _write_registry(
        tmp_path,
        [{"id": "data_scientist", "file": file1, "roles": ["Data Scientist"]}],
    )
    registry = load_resume_registry(path)
    assert len(registry.resumes) == 1
    assert registry.get("data_scientist").roles == ["Data Scientist"]


def test_missing_registry_file_raises_actionable_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="resumes.example.yaml"):
        load_resume_registry(tmp_path / "does_not_exist.yaml")


def test_registry_loads_even_if_referenced_resume_files_dont_exist(tmp_path):
    """
    Config parsing succeeds even for a not-yet-placed file -- file
    existence is a separate, runtime concern (check_registry_files).
    """
    path = _write_registry(
        tmp_path,
        [{"id": "data_scientist", "file": "resumes/not_placed_yet.pdf", "roles": ["Data Scientist"]}],
    )
    registry = load_resume_registry(path)
    assert len(registry.resumes) == 1


# --- Invalid configuration ---


def test_duplicate_resume_ids_rejected(tmp_path):
    file1 = _make_file(tmp_path, "a.pdf")
    file2 = _make_file(tmp_path, "b.pdf")
    path = _write_registry(
        tmp_path,
        [
            {"id": "data_scientist", "file": file1, "roles": ["Data Scientist"]},
            {"id": "data_scientist", "file": file2, "roles": ["ML Engineer"]},
        ],
    )
    with pytest.raises(ValidationError, match="Duplicate resume id"):
        load_resume_registry(path)


def test_unsupported_file_type_rejected(tmp_path):
    path = _write_registry(
        tmp_path,
        [{"id": "bad", "file": "resumes/data_scientist.txt", "roles": ["Data Scientist"]}],
    )
    with pytest.raises(ValidationError, match="Unsupported resume file type"):
        load_resume_registry(path)


def test_docx_is_a_supported_type(tmp_path):
    file1 = _make_file(tmp_path, "resume.docx")
    path = _write_registry(tmp_path, [{"id": "a", "file": file1, "roles": ["Analyst"]}])
    registry = load_resume_registry(path)
    assert registry.resumes[0].file.suffix == ".docx"


def test_empty_registry_is_valid(tmp_path):
    path = _write_registry(tmp_path, [])
    registry = load_resume_registry(path)
    assert registry.resumes == []


# --- File hash / integrity ---


def test_compute_file_hash_matches_manual_sha256(tmp_path):
    file_path = tmp_path / "a.pdf"
    file_path.write_bytes(b"hello world")
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert compute_file_hash(file_path) == expected


def test_compute_file_hash_changes_with_content(tmp_path):
    file_a = tmp_path / "a.pdf"
    file_a.write_bytes(b"version 1")
    hash_a = compute_file_hash(file_a)
    file_a.write_bytes(b"version 2")
    hash_b = compute_file_hash(file_a)
    assert hash_a != hash_b


# --- check_registry_files ---


def test_check_registry_files_reports_existing_and_missing(tmp_path):
    existing = _make_file(tmp_path, "exists.pdf")
    path = _write_registry(
        tmp_path,
        [
            {"id": "present", "file": existing, "roles": ["Data Scientist"]},
            {"id": "absent", "file": str(tmp_path / "missing.pdf"), "roles": ["Data Analyst"]},
        ],
    )
    registry = load_resume_registry(path)
    statuses = check_registry_files(registry)

    present = next(s for s in statuses if s.id == "present")
    absent = next(s for s in statuses if s.id == "absent")

    assert present.exists is True
    assert present.file_hash is not None
    assert absent.exists is False
    assert absent.file_hash is None
