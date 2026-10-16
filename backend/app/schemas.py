from datetime import datetime, timezone
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def strip_identifiers(cls, value, info):
        if info.field_name in {"username", "name", "base_url", "model", "arxiv"} and isinstance(value, str):
            return value.strip()
        return value

class UserOut(Schema):
    id: str
    username: str
    is_admin: bool
class LoginIn(Schema):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)
class UserIn(LoginIn):
    password: str = Field(min_length=10, max_length=1024)
    is_admin: bool = False
class Ok(Schema):
    ok: bool = True
class WorkspaceIn(Schema):
    name: str = Field(min_length=1, max_length=200)
class WorkspaceOut(WorkspaceIn):
    id: str
    role: str
class MemberIn(Schema):
    user_id: str
    role: Literal["admin", "member"] = "member"
class MemberOut(MemberIn):
    username: str
class SkillOut(Schema):
    id: str
    name: str
    description: str
    version: str
    available: bool
    unavailable_reason: str | None = None
class ModelIn(Schema):
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: str = Field(min_length=1, max_length=4096)
    protocol: Literal["openai-completions", "anthropic-messages"] = "openai-completions"
    thinking_format: Literal["default", "qwen-chat-template"] = "default"
    vision: bool = False
    context_window: int = Field(default=128000, ge=4096, le=2000000)
    max_tokens: int = Field(default=8192, ge=256, le=100000)
class ModelPatch(Schema):
    @model_validator(mode="before")
    @classmethod
    def no_explicit_null(cls, value):
        if isinstance(value, dict) and any(field is None for field in value.values()):
            raise ValueError("Omit unchanged fields; explicit null is not supported")
        return value

    name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=4096)
    protocol: Literal["openai-completions", "anthropic-messages"] | None = None
    thinking_format: Literal["default", "qwen-chat-template"] | None = None
    vision: bool | None = None
    context_window: int | None = Field(default=None, ge=4096, le=2000000)
    max_tokens: int | None = Field(default=None, ge=256, le=100000)
class ModelOut(Schema):
    id: str
    name: str
    base_url: str
    model: str
    protocol: str
    thinking_format: Literal["default", "qwen-chat-template"]
    vision: bool
    context_window: int
    max_tokens: int
    has_api_key: bool
class ModelTest(Schema):
    ok: bool
    message: str
class TemplateIn(Schema):
    name: str = Field(min_length=1, max_length=200)
    brief_markdown: str = Field(min_length=1, max_length=30000)
    full_markdown: str = Field(min_length=1, max_length=100000)
    instructions: str = Field(default="", max_length=30000)
class TemplateVersionOut(Schema):
    id: str
    number: int
    brief_markdown: str
    full_markdown: str
    instructions: str
    created_at: datetime
class TemplateOut(Schema):
    id: str
    name: str
    is_builtin: bool
    owner_id: str | None
    current_version: TemplateVersionOut
class ArxivIn(Schema):
    arxiv: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=1000)
class PaperOut(Schema):
    id: str
    workspace_id: str
    title: str
    source_type: str
    source_url: str | None
    sha256: str | None
    metadata_json: dict
    created_at: datetime
class ReadingIn(Schema):
    workflow_mode: Literal["guided", "native"] = "guided"
    paper_id: str
    skill_id: str
    model_connection_id: str
    template_id: str
class ReadingCreated(Schema):
    reading_id: str
    job_id: str
class JobCreated(Schema):
    job_id: str
class RetryIn(Schema):
    pass
class RevisionIn(Schema):
    instruction: str = Field(min_length=1, max_length=10000)
    base_revision_id: str
class AgentTaskOut(Schema):
    goal: str
    current_action: str
    accepted_work: list[str]
    issue: str | None
    coverage: dict
    remaining_work: list[str]
    elapsed_seconds: float
    remaining_seconds: float
    tool_calls: int
    remaining_tool_calls: int
    can_continue: bool

class JobOut(Schema):
    execution_engine: Literal["legacy", "pi-agent-v1"] = "legacy"
    agent_task: AgentTaskOut | None = None
    id: str
    workspace_id: str
    reading_id: str
    kind: str
    status: str
    stage: str
    stage_records: list[dict]
    progress: int
    attempt: int
    cancel_requested: bool
    error: str | None
    usage: dict
    created_at: datetime
    updated_at: datetime
class RevisionOut(Schema):
    id: str
    number: int
    parent_revision_id: str | None
    instruction: str
    brief_markdown: str
    full_markdown: str
    evidence_index: list
    reading_packet: dict
    verification: dict
    provenance: dict
    artifacts: list
    created_at: datetime
class ReadingListOut(Schema):
    id: str
    workflow_mode: Literal["guided", "native"]
    paper_id: str
    paper_title: str
    skill_id: str
    created_at: datetime
    current_revision_id: str | None
    latest_job: JobOut | None
class ReadingDetail(Schema):
    id: str
    workflow_mode: Literal["guided", "native"]
    paper_id: str
    paper_title: str
    skill_id: str
    current_revision_id: str | None
    revisions: list[RevisionOut]
    jobs: list[JobOut]


class ConversationIn(Schema):
    revision_id: str
    title: str = Field(default="新对话", min_length=1, max_length=200)

class ChatDatedOut(Schema):
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_timestamps(cls, value):
        # PostgreSQL's session zone may differ from newly created Python values.
        # A replayed idempotent response must retain the same representation.
        return value.astimezone(timezone.utc)

class ConversationOut(ChatDatedOut):
    id: str
    reading_id: str
    revision_id: str
    title: str
    created_at: datetime
    updated_at: datetime

class ChatMessageIn(Schema):
    content: str = Field(min_length=1, max_length=10000)
    client_request_id: UUID

    @field_validator("content")
    @classmethod
    def nonempty_content(cls, value):
        if not value.strip():
            raise ValueError("Question must contain text")
        return value

class ChatMessageOut(ChatDatedOut):
    execution_engine: Literal["legacy", "pi-agent-v1"] = "legacy"
    agent_task: AgentTaskOut | None = None
    id: str
    conversation_id: str
    role: Literal["user", "assistant"]
    sequence: int
    content: str
    status: Literal["queued", "running", "waiting", "succeeded", "partial", "failed", "cancelled"]
    evidence_index: list[dict]
    verification: dict
    usage: dict
    error: str | None
    job_id: str | None
    created_at: datetime
    updated_at: datetime

class ConversationDetail(Schema):
    conversation: ConversationOut
    messages: list[ChatMessageOut]
    next_before: int | None

class ChatMessageCreated(Schema):
    user_message: ChatMessageOut
    assistant_message: ChatMessageOut
    job_id: str

class ChatRetryCreated(Schema):
    assistant_message: ChatMessageOut
    job_id: str
