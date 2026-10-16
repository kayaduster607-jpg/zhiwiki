"""Versioned boundary schemas: no caller-supplied identity, URL or SQL."""

from typing import List, Literal, Optional
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Filters(Input):
    document_ids: List[str] = Field(default_factory=list, max_length=20)
    language: Optional[Literal["zh", "en"]] = None
    tags: List[str] = Field(default_factory=list, max_length=10)
    effective_at: Optional[date] = None


class Caller(Input):
    agent_run_id: str = Field(default="", max_length=128)
    tool_call_id: str = Field(default="", max_length=128)


class RetrievalInput(Input):
    query: str = Field(min_length=1, max_length=4000)
    knowledge_base_ids: List[str] = Field(min_length=1, max_length=5)
    top_k: int = Field(default=5, ge=1, le=20, strict=True)
    context_char_budget: int = Field(default=12000, ge=500, le=20000, strict=True)
    filters: Filters = Field(default_factory=Filters)
    caller: Caller = Field(default_factory=Caller)

    @field_validator("query")
    @classmethod
    def no_nul(cls, value):
        if "\x00" in value:
            raise ValueError("NUL is forbidden")
        return value

    @field_validator("knowledge_base_ids")
    @classmethod
    def unique_ids(cls, value):
        if len(set(value)) != len(value) or any(not part or len(part) > 100 for part in value):
            raise ValueError("Use unique bounded knowledge base IDs")
        return value


class ReadInput(Input):
    retrieval_ids: List[str] = Field(min_length=1, max_length=6)
    evidence_ids: List[str] = Field(min_length=1, max_length=20)
    context_char_budget: int = Field(default=12000, ge=500, le=20000, strict=True)


class Citation(Input):
    evidence_id: str = Field(min_length=1, max_length=100)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ValidateInput(Input):
    retrieval_ids: List[str] = Field(min_length=1, max_length=6)
    citations: List[Citation] = Field(min_length=1, max_length=30)


class LoginInput(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class UserInput(LoginInput):
    password: str = Field(min_length=12, max_length=1024)
    admin: bool = False


class TokenInput(Input):
    knowledge_base_ids: List[str] = Field(min_length=1, max_length=20)
    scopes: List[Literal["retrieve", "read", "validate", "download"]] = Field(default_factory=lambda: ["retrieve", "read", "validate"], min_length=1, max_length=4)
    expires_in: int = Field(default=604800, ge=60, le=604800, strict=True)


class KnowledgeBaseInput(Input):
    title: str = Field(min_length=1, max_length=200)
    id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{2,100}$")


class GrantInput(Input):
    role: Literal["reader", "editor", "publisher", "admin"]


class ReviewInput(Input):
    note: str = Field(min_length=5, max_length=1000)


class PageCorrectionInput(ReviewInput):
    text: str = Field(max_length=500000)
    expected_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MetadataInput(Input):
    language: Optional[Literal["zh", "en"]] = None
    tags: List[str] = Field(default_factory=list, max_length=10)
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None


class WikiDraftInput(Input):
    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=200)
    evidence_ids: List[str] = Field(min_length=1, max_length=20)
    expected_version: Optional[int] = Field(default=None, ge=1, strict=True)


class Output(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal["1.0"]
    request_id: str
    elapsed_ms: float


class EvidenceOutput(BaseModel):
    model_config = ConfigDict(extra="allow")
    evidence_id: str
    knowledge_base_id: str
    document_id: str
    revision_id: str
    text: str
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator: dict
    quality_flags: List[str]
    untrusted_content: Literal[True]


class RetrievalOutput(Output):
    retrieval_id: str
    receipt_expires_at: str
    status: Literal["ok", "empty"]
    checked_at: str
    knowledge_snapshots: List[dict]
    evidence: List[EvidenceOutput]
    returned_chars: int
    truncated: bool
    retrieval: dict
    warnings: List[str]
    usage: dict


class ValidationOutput(Output):
    valid: Literal[True]
    checked_at: str
    knowledge_snapshots: List[dict]
    semantic_support: Literal["not_checked"]


class ErrorOutput(BaseModel):
    schema_version: Literal["1.0"]
    request_id: str
    error: dict
