"""FastAPI application: three-way configuration merge service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import db, store
from .models import (
    DraftUpdateRequest,
    MergeCreate,
    ResolveRequest,
    RuleResolveRequest,
)
from .validation import InvalidResultSchema, ResultValidationFailed


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dependencies are installed at image build; tables are created here,
    # so a single `docker compose up` brings up a ready-to-use service.
    db.init_schema()
    yield


app = FastAPI(
    title="Config Three-Way Merge API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(store.MergeNotFound)
async def not_found_handler(request: Request, exc: store.MergeNotFound):
    return JSONResponse(status_code=404, content={"detail": "merge not found"})


@app.exception_handler(store.StaleRevision)
async def stale_revision_handler(request: Request, exc: store.StaleRevision):
    return JSONResponse(
        status_code=409,
        content={
            "detail": "stale revision: the merge was updated by another request",
            "current_revision": exc.current_revision,
        },
    )


@app.exception_handler(store.AlreadyResolved)
async def already_resolved_handler(request: Request, exc: store.AlreadyResolved):
    return JSONResponse(
        status_code=409,
        content={"detail": "merge already resolved: result is immutable"},
    )


@app.exception_handler(store.MergeNotResolved)
async def not_resolved_handler(request: Request, exc: store.MergeNotResolved):
    return JSONResponse(
        status_code=409,
        content={"detail": "merge is pending: no result patch is available yet"},
    )


@app.exception_handler(store.UnknownConflictPointer)
async def unknown_pointer_handler(request: Request, exc: store.UnknownConflictPointer):
    return JSONResponse(
        status_code=400,
        content={"detail": f"no open conflict at pointer {exc.pointer!r}"},
    )


@app.exception_handler(store.InvalidResolution)
async def invalid_resolution_handler(request: Request, exc: store.InvalidResolution):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(store.InvalidPattern)
async def invalid_pattern_handler(request: Request, exc: store.InvalidPattern):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(store.DraftNotFound)
async def draft_not_found_handler(request: Request, exc: store.DraftNotFound):
    return JSONResponse(status_code=404, content={"detail": "draft not found"})


@app.exception_handler(store.DraftNotOpen)
async def draft_not_open_handler(request: Request, exc: store.DraftNotOpen):
    return JSONResponse(
        status_code=409,
        content={
            "detail": "draft is no longer open: it was already committed or invalidated"
        },
    )


@app.exception_handler(store.DraftConflictSetChanged)
async def draft_conflicts_changed_handler(
    request: Request, exc: store.DraftConflictSetChanged
):
    return JSONResponse(
        status_code=409,
        content={
            "detail": "draft is stale: the merge's open conflicts no longer "
            "cover the draft's pointers"
        },
    )


@app.exception_handler(store.InvalidRuleSet)
async def invalid_ruleset_handler(request: Request, exc: store.InvalidRuleSet):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(store.RuleApplicationFailed)
async def rule_apply_failed_handler(request: Request, exc: store.RuleApplicationFailed):
    return JSONResponse(
        status_code=400,
        content={
            "detail": str(exc),
            "pointer": exc.pointer,
            "reason": exc.reason,
        },
    )


@app.exception_handler(InvalidResultSchema)
async def invalid_result_schema_handler(request: Request, exc: InvalidResultSchema):
    # The result_schema supplied to POST /merges is not itself a valid
    # Draft 2020-12 JSON Schema: 422, and no merge record was created.
    return JSONResponse(
        status_code=422,
        content={
            "detail": "invalid result_schema: not a valid JSON Schema (Draft 2020-12)",
            "errors": exc.errors,
        },
    )


@app.exception_handler(ResultValidationFailed)
async def result_validation_failed_handler(request: Request, exc: ResultValidationFailed):
    # The candidate final document violates the merge's result_schema:
    # 422, the whole batch was rolled back and the revision is unchanged.
    return JSONResponse(
        status_code=422,
        content={
            "detail": "final result does not satisfy result_schema",
            "errors": exc.errors,
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/merges", status_code=201)
def create_merge(
    body: MergeCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None),
):
    """Submit base/left/right and start a merge order.

    Idempotent: a repeated idempotency key returns the existing merge
    order (HTTP 200) instead of creating a new one.
    """
    key = body.idempotency_key or idempotency_key
    view, created = store.create_merge(
        body.base, body.left, body.right, key, body.result_schema
    )
    if not created:
        response.status_code = 200
    return view


@app.get("/merges/{merge_id}")
def get_merge(merge_id: str):
    """Status query: state, revision, open conflicts, final result."""
    return store.get_merge(merge_id)


@app.get("/merges/{merge_id}/patch")
def get_merge_patch(merge_id: str):
    """RFC 6902 patch from the original base_doc to the final result.

    Available once the merge is resolved: 409 while pending, 404 if unknown.
    The response also carries SHA-256 digests of base and result (canonical
    JSON: sorted keys, no whitespace).
    """
    return store.get_merge_patch(merge_id)


def _resolution_items(body: ResolveRequest | DraftUpdateRequest) -> list[dict]:
    return [
        {
            "pointer": r.pointer,
            "choice": r.choice,
            "value": r.value,
            "value_provided": r.value_provided,
        }
        for r in body.resolutions
    ]


@app.post("/merges/{merge_id}/resolutions")
def resolve_merge(merge_id: str, body: ResolveRequest):
    """Submit a batch of conflict resolutions against the current revision.

    Returns 409 on a stale revision or once the merge is resolved.
    When the last conflict is resolved the result becomes immutable.
    """
    return store.apply_resolutions(merge_id, body.revision, _resolution_items(body))


@app.post("/merges/{merge_id}/resolution-preview")
def preview_resolution(merge_id: str, body: ResolveRequest):
    """Dry-run a resolution batch against the current revision.

    Same request shape as the committing endpoint, but the batch is
    validated and applied on an in-memory copy only: nothing is written,
    the revision is not bumped and the status never changes. Returns the
    would-be document and remaining conflicts; when the batch would
    resolve everything, the deterministic base-to-result patch and its
    SHA-256 are included.
    """
    return store.preview_resolutions(merge_id, body.revision, _resolution_items(body))


@app.post("/merges/{merge_id}/rule-resolutions")
def resolve_by_rules(merge_id: str, body: RuleResolveRequest):
    """Resolve conflicts in bulk via ordered RFC 6901-style path patterns.

    Each rule may give a ``left`` and/or ``right`` pattern (single-segment
    ``*`` and trailing ``**`` supported). Every open conflict is assigned
    its unique best match — more literal segments first, then fewer ``*``,
    then declaration order. All hits are validated first; only when every
    one is applicable do they commit in a single transaction with exactly
    one revision bump. A missing chosen side honours the deletion. Stale
    revision or a resolved merge yields 409; an invalid pattern or failed
    application yields 400 with no partial modification.
    """
    rule_items = [
        {"left": rule.left, "right": rule.right} for rule in body.rules
    ]
    return store.apply_rule_resolutions(merge_id, body.revision, rule_items)


# ---------------------------------------------------------------------------
# Resolution drafts
# ---------------------------------------------------------------------------


@app.post("/merges/{merge_id}/drafts", status_code=201)
def create_draft(merge_id: str):
    """Open a persistent resolution draft for a pending merge.

    The draft pins the merge revision at creation time and snapshots the
    working document and open conflicts, so edits and previews stay stable
    while other clients keep working on the merge. A resolved merge yields
    409, an unknown one 404.
    """
    return store.create_draft(merge_id)


@app.get("/merges/{merge_id}/drafts/{draft_id}")
def get_draft(merge_id: str, draft_id: str):
    """Read a draft: status, pinned base_revision and accumulated values."""
    return store.get_draft(merge_id, draft_id)


@app.post("/merges/{merge_id}/drafts/{draft_id}/resolutions")
def update_draft(merge_id: str, draft_id: str, body: DraftUpdateRequest):
    """Append or overwrite conflict values on the draft.

    Same choice/value semantics as the resolution endpoints; a pointer
    already present is overwritten, a new one is appended, and pointers
    may be filled in over several calls. Only the draft is written — the
    merge order (revision, conflicts, document) is never touched. A draft
    that was already committed yields 409.
    """
    return store.update_draft(merge_id, draft_id, _resolution_items(body))


@app.get("/merges/{merge_id}/drafts/{draft_id}/preview")
def preview_draft(merge_id: str, draft_id: str):
    """Dry-run the draft against its creation-time snapshot.

    Nothing is written. Returns the would-be document and the conflicts
    still open after the draft; when the draft would resolve everything,
    the deterministic base-to-result patch and its SHA-256 are included.
    """
    return store.preview_draft(merge_id, draft_id)


@app.post("/merges/{merge_id}/drafts/{draft_id}/commit")
def commit_draft(merge_id: str, draft_id: str):
    """Commit the draft: apply all of its values in a single transaction.

    The transaction re-reads and re-validates the merge revision (must
    still equal the draft's pinned base_revision), the open conflicts
    (must still cover every drafted pointer) and the result_schema gate
    (when the draft resolves the last conflict). On success every value
    is applied at once, the revision bumps exactly once and the draft is
    invalidated. Any changed condition yields 409 (a result_schema
    violation 422) and neither the merge nor the draft is partially
    written.
    """
    return store.commit_draft(merge_id, draft_id)
