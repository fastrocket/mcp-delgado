"""Validated MCP inputs and durable job records."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    POLICY_FAILED = "policy_failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class DelegateTaskInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    task: str = Field(..., min_length=10, max_length=20_000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)
    workspace_path: str = Field(..., min_length=1, max_length=2_000)
    allowed_paths: list[str] = Field(..., min_length=1, max_length=100)
    required_commands: list[str] = Field(default_factory=list, max_length=20)
    provider: Optional[str] = Field(default=None, max_length=100)
    model: Optional[str] = Field(default=None, max_length=200)
    max_minutes: int = Field(default=30, ge=1, le=240)


class ReviewInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    request: str = Field(..., min_length=5, max_length=20_000)
    workspace_path: str = Field(..., min_length=1, max_length=2_000)
    provider: Optional[str] = Field(default=None, max_length=100)
    model: Optional[str] = Field(default=None, max_length=200)
    max_minutes: int = Field(default=10, ge=1, le=60)


class JobIdInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    job_id: str = Field(..., pattern=r"^[a-f0-9]{32}$")


class ReadDiffInput(JobIdInput):
    max_bytes: int = Field(default=60_000, ge=1_000, le=200_000)


class RepairTaskInput(JobIdInput):
    feedback: str = Field(..., min_length=5, max_length=20_000)
    max_minutes: int = Field(default=20, ge=1, le=120)


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    state: JobState
    task: str
    acceptance_criteria: list[str]
    workspace_path: str
    allowed_paths: list[str]
    required_commands: list[str]
    provider: str
    model: str
    max_minutes: int
    created_at: str = Field(default_factory=utc_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    summary: str = ""
    error: str = ""
    changed_paths: list[str] = Field(default_factory=list)
    policy_violations: list[str] = Field(default_factory=list)
    validation_results: list[dict] = Field(default_factory=list)
    parent_job_id: Optional[str] = None
