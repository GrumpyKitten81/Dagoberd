"""Local classification and Comdirect job endpoints."""

from __future__ import annotations

import threading
from contextvars import copy_context
from dataclasses import asdict

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from dagoberd.accounts import AccountScope
from dagoberd.contracts import GUI_CONTRACT_VERSION, success
from dagoberd.gui.context import GuiContext
from dagoberd.gui.jobs import WebChallengeHandler
from dagoberd.ledger.pipeline import PipelineConfig
from dagoberd.providers.comdirect import ComdirectError
from dagoberd.services.comdirect_session import synchronize
from dagoberd.workspaces import workspace_operation

router = APIRouter(prefix="/api")


class SyncRequest(BaseModel):
    connection: str
    pin: str
    account_ids: list[str] | None = None
    booking_from: str | None = None
    booking_to: str | None = None
    resume: str | None = None


class TanRequest(BaseModel):
    tan: str


class RulesRequest(BaseModel):
    profile_code: str
    account_id: str | None = None
    limit: int = Field(default=100, ge=1, le=500)


class LLMPreviewRequest(RulesRequest):
    transaction_ids: list[str] | None = None


class LLMRunRequest(BaseModel):
    fingerprint: str


class ClassificationReviewRequest(BaseModel):
    decision: str
    expected_version: int = Field(ge=1)


class RuleRequest(BaseModel):
    profile_code: str
    name: str
    pattern: str
    category_code: str
    priority: int = 100
    active: bool = True
    account_id: str | None = None


class RuleUpdateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    name: str | None = None
    pattern: str | None = None
    category_code: str | None = None
    priority: int | None = None
    active: bool | None = None
    account_id: str | None = None


class HintRequest(BaseModel):
    profile_code: str
    text: str
    pattern: str | None = None
    category_code: str | None = None
    priority: int = 100
    active: bool = True
    account_id: str | None = None


class HintUpdateRequest(RuleUpdateRequest):
    text: str | None = None


class ExplainRequest(BaseModel):
    transaction_id: str
    profile_code: str


class RuleTestRequest(BaseModel):
    pattern: str
    profile_code: str
    account_id: str | None = None
    limit: int = Field(default=1000, ge=1, le=1000)


class ProfilesRequest(BaseModel):
    profile_codes: list[str]


class StageRequest(BaseModel):
    enabled: bool


def _scope(account_id: str | None) -> AccountScope:
    return AccountScope.selected([account_id]) if account_id else AccountScope.all()


def _context(request: Request) -> GuiContext:
    return request.app.state.context


@router.post("/sync/comdirect")
def start_sync(payload: SyncRequest, request: Request) -> dict:
    context = _context(request)
    connection = context.loaded.config.comdirect.connection(payload.connection)
    if not connection.enabled:
        raise ValueError(f"Comdirect connection '{payload.connection}' is not enabled")
    job_id, job = request.app.state.jobs.create()
    handler = WebChallengeHandler(job)

    def run() -> None:
        try:
            result = synchronize(
                context.repository(),
                payload.connection,
                connection,
                payload.pin,
                push_tan=handler.push_tan,
                photo_tan=handler.photo_tan,
                sms_tan=handler.sms_tan,
                account_ids=payload.account_ids,
                booking_from=payload.booking_from,
                booking_to=payload.booking_to,
                resume=payload.resume,
            )
            job.finish(asdict(result))
        except (KeyError, ValueError, ComdirectError) as error:
            job.fail(str(error))
        finally:
            job.settle()

    def run_locked() -> None:
        try:
            with workspace_operation(
                context.loaded, getattr(request.app.state, "workspace_registry", None)
            ):
                run()
        except (KeyError, ValueError) as error:
            job.fail(str(error))
        finally:
            job.settle()

    inherited = copy_context()
    threading.Thread(target=inherited.run, args=(run_locked,), daemon=True).start()
    return success({"job_id": job_id}, contract_version=GUI_CONTRACT_VERSION)


@router.get("/jobs/{job_id}")
def job(job_id: str, request: Request) -> dict:
    return success(request.app.state.jobs.status(job_id), contract_version=GUI_CONTRACT_VERSION)


@router.post("/jobs/{job_id}/tan")
def tan(job_id: str, payload: TanRequest, request: Request) -> dict:
    job = request.app.state.jobs.get(job_id)
    if job.status != "awaiting_tan":
        raise ValueError("Job is not waiting for a TAN")
    job.tan = payload.tan
    job.event.set()
    return success({"job_id": job_id, "accepted": True}, contract_version=GUI_CONTRACT_VERSION)


@router.post("/classification/rules")
def apply_rules(payload: RulesRequest, request: Request) -> dict:
    repository = _context(request).repository()
    results = [
        repository.apply_rules(item["id"], payload.profile_code)
        for item in repository.list_transactions(
            limit=payload.limit, account_scope=_scope(payload.account_id)
        )
    ]
    return success(
        {"examined": len(results), "applied": sum(result is not None for result in results)},
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/classification/auto")
def classify_auto(payload: RulesRequest, request: Request) -> dict:
    return success(
        _context(request)
        .classification()
        .classify_open(
            payload.profile_code, limit=payload.limit, account_scope=_scope(payload.account_id)
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/classification/llm/preview")
def llm_preview(payload: LLMPreviewRequest, request: Request) -> dict:
    return success(
        _context(request)
        .llm_preview(with_provider=False)
        .create(
            payload.profile_code,
            transaction_ids=payload.transaction_ids,
            account_scope=_scope(payload.account_id),
            limit=payload.limit,
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/classification/llm/{preview_id}/run")
def llm_run(preview_id: str, payload: LLMRunRequest, request: Request) -> dict:
    return success(
        _context(request).llm_preview(with_provider=True).execute(preview_id, payload.fingerprint),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.get("/classifications")
def classifications(
    request: Request, status: str | None = None, profile_code: str | None = None, limit: int = 100
) -> dict:
    return success(
        _context(request)
        .repository()
        .list_classifications(status=status, profile_code=profile_code, limit=limit),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/classifications/{classification_id}/review")
def review_classification(
    classification_id: str, payload: ClassificationReviewRequest, request: Request
) -> dict:
    return success(
        _context(request)
        .repository()
        .review_classification(classification_id, payload.decision, payload.expected_version),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.get("/rules")
def rules(request: Request, profile_code: str | None = None, account_id: str | None = None) -> dict:
    return success(
        _context(request).repository().list_rules(profile_code=profile_code, account_id=account_id),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/rules")
def add_rule(payload: RuleRequest, request: Request) -> dict:
    repository = _context(request).repository()
    identifier = repository.add_rule(
        payload.profile_code,
        payload.name,
        payload.pattern,
        payload.category_code,
        payload.priority,
        payload.account_id,
        payload.active,
    )
    return success({"id": identifier}, contract_version=GUI_CONTRACT_VERSION)


@router.patch("/rules/{rule_id}")
def update_rule(rule_id: str, payload: RuleUpdateRequest, request: Request) -> dict:
    return success(
        _context(request)
        .repository()
        .update_rule(rule_id, **payload.model_dump(exclude_unset=True)),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: str, expected_version: int, request: Request) -> dict:
    _context(request).repository().delete_rule(rule_id, expected_version)
    return success({"id": rule_id, "deleted": True}, contract_version=GUI_CONTRACT_VERSION)


@router.get("/hints")
def hints(request: Request, profile_code: str | None = None, account_id: str | None = None) -> dict:
    return success(
        _context(request).repository().list_hints(profile_code=profile_code, account_id=account_id),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/hints")
def add_hint(payload: HintRequest, request: Request) -> dict:
    repository = _context(request).repository()
    identifier = repository.add_hint(
        payload.profile_code,
        payload.text,
        pattern=payload.pattern,
        category_code=payload.category_code,
        priority=payload.priority,
        account_id=payload.account_id,
        active=payload.active,
    )
    return success({"id": identifier}, contract_version=GUI_CONTRACT_VERSION)


@router.patch("/hints/{hint_id}")
def update_hint(hint_id: str, payload: HintUpdateRequest, request: Request) -> dict:
    return success(
        _context(request)
        .repository()
        .update_hint(hint_id, **payload.model_dump(exclude_unset=True)),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.delete("/hints/{hint_id}")
def delete_hint(hint_id: str, expected_version: int, request: Request) -> dict:
    _context(request).repository().delete_hint(hint_id, expected_version)
    return success({"id": hint_id, "deleted": True}, contract_version=GUI_CONTRACT_VERSION)


@router.post("/classification/explain")
def explain(payload: ExplainRequest, request: Request) -> dict:
    return success(
        _context(request)
        .classification_read_only()
        .explain(payload.transaction_id, payload.profile_code),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/classification/rules/test")
def test_rule(payload: RuleTestRequest, request: Request) -> dict:
    return success(
        _context(request).classification_read_only().test_rule_pattern(**payload.model_dump()),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.get("/accounts/{account_id}/profiles")
def account_profiles(account_id: str, request: Request) -> dict:
    return success(
        PipelineConfig(_context(request).repository()).account_profiles(account_id),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.put("/accounts/{account_id}/profiles")
def set_account_profiles(account_id: str, payload: ProfilesRequest, request: Request) -> dict:
    return success(
        PipelineConfig(_context(request).repository()).set_account_profiles(
            account_id, payload.profile_codes
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.get("/accounts/{account_id}/stages")
def account_stages(account_id: str, request: Request) -> dict:
    return success(
        PipelineConfig(_context(request).repository()).stages(account_id),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.put("/accounts/{account_id}/stages/{stage}")
def set_stage(account_id: str, stage: str, payload: StageRequest, request: Request) -> dict:
    return success(
        PipelineConfig(_context(request).repository()).set_stage(
            account_id, stage, payload.enabled
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.get("/classification/import-annotations")
def import_annotations(request: Request) -> dict:
    profiles = _context(request).loaded.config.imports.profiles
    return success(
        [
            {
                "name": name,
                "source": item.source,
                "category_column": item.annotations.category_column if item.annotations else None,
                "category_profile": item.annotations.category_profile if item.annotations else None,
                "dimensions": item.annotations.dimensions if item.annotations else {},
                "profile_columns": item.annotations.profile_columns if item.annotations else {},
                "tag_if_positive": item.annotations.tag_if_positive if item.annotations else {},
                "aliases": len(item.annotations.category_aliases) if item.annotations else 0,
            }
            for name, item in profiles.items()
        ],
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.get("/llm")
def llm_config(request: Request) -> dict:
    config = _context(request).loaded.config.llm
    return success(
        {
            "enabled": config.enabled,
            "mode": config.mode,
            "provider": config.provider,
            "base_url": config.base_url,
            "model": config.model,
            "allow_cloud": config.allow_cloud,
            "api_key_secret": config.api_key_secret,
        },
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/llm/test")
def test_llm(request: Request) -> dict:
    provider = _context(request).llm_provider()
    if provider is None:
        raise ValueError("LLM is disabled")
    return success(provider.test_connection(), contract_version=GUI_CONTRACT_VERSION)
