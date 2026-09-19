"""Merge-order persistence and state transitions."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from . import db, rules
from .merge import MISSING, delete_at, merge3, parse_pointer, set_at
from .patch import generate_patch, sha256_hex
from .validation import check_result_schema, validate_result
from .rules import (
    InvalidPattern,
    InvalidRuleSet,
    RuleApplicationFailed,
    apply_plan,
    compile_rules,
    group_matches,
    plan_resolutions,
)

STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"

DRAFT_OPEN = "open"
DRAFT_COMMITTED = "committed"


class MergeNotFound(Exception):
    pass


class MergeNotResolved(Exception):
    """Raised when the deterministic patch is requested before resolution."""


class StaleRevision(Exception):
    def __init__(self, current_revision: int):
        self.current_revision = current_revision
        super().__init__(f"stale revision, current is {current_revision}")


class AlreadyResolved(Exception):
    pass


class UnknownConflictPointer(Exception):
    def __init__(self, pointer: str):
        self.pointer = pointer
        super().__init__(f"no open conflict at pointer {pointer!r}")


class InvalidResolution(Exception):
    pass


class DraftNotFound(Exception):
    pass


class DraftNotOpen(Exception):
    """The draft was already committed (or otherwise invalidated)."""


class DraftConflictSetChanged(Exception):
    """The merge's open conflicts no longer cover the draft's pointers."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return {
        "merge_id": row["id"],
        "status": row["status"],
        "revision": row["revision"],
        "document": json.loads(row["document"]),
        "conflicts": json.loads(row["conflicts"]),
        "conflict_count": len(json.loads(row["conflicts"])),
        "result": json.loads(row["result"]) if row["result"] is not None else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_merge(merge_id: str) -> dict:
    row = db.query_one("SELECT * FROM merges WHERE id = ?", (merge_id,))
    if row is None:
        raise MergeNotFound(merge_id)
    return _row_to_dict(row)


def get_merge_patch(merge_id: str) -> dict:
    """Return the RFC 6902 patch from base_doc to the immutable result.

    Raises MergeNotFound (404) for an unknown merge and MergeNotResolved
    (409) while the merge is still pending.
    """
    row = db.query_one(
        "SELECT status, base_doc, result FROM merges WHERE id = ?",
        (merge_id,),
    )
    if row is None:
        raise MergeNotFound(merge_id)
    if row["status"] != STATUS_RESOLVED or row["result"] is None:
        raise MergeNotResolved(merge_id)
    base = json.loads(row["base_doc"])
    result = json.loads(row["result"])
    return {
        "merge_id": merge_id,
        "status": STATUS_RESOLVED,
        "patch": generate_patch(base, result),
        "base_sha256": sha256_hex(base),
        "result_sha256": sha256_hex(result),
    }


def create_merge(
    base: Any,
    left: Any,
    right: Any,
    idempotency_key: str | None,
    result_schema: Any = None,
) -> tuple[dict, bool]:
    """Create a merge order. Returns (merge_view, created).

    A repeated idempotency key replays the existing merge order instead of
    creating a new one. When ``result_schema`` is given it must itself be a
    valid Draft 2020-12 JSON Schema, and a conflict-free (immediately
    resolved) merge must produce a document satisfying it; either failure
    aborts creation before any row is inserted.
    """
    if idempotency_key:
        row = db.query_one(
            "SELECT * FROM merges WHERE idempotency_key = ?", (idempotency_key,)
        )
        if row is not None:
            return _row_to_dict(row), False

    # Gate 1: the schema itself must be a valid Draft 2020-12 schema.
    if result_schema is not None:
        check_result_schema(result_schema)

    document, conflicts = merge3(base, left, right)
    now = _now()
    status = STATUS_RESOLVED if not conflicts else STATUS_PENDING
    # No conflicts: the result is final and immutable from the start, so it
    # must pass the result schema (gate 2) before anything is persisted.
    if not conflicts and result_schema is not None:
        validate_result(result_schema, document)
    result = document if not conflicts else None

    merge_id = uuid.uuid4().hex
    try:
        db.execute(
            """
            INSERT INTO merges (
                id, idempotency_key, status, revision,
                base_doc, left_doc, right_doc,
                document, conflicts, result, result_schema,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                merge_id,
                idempotency_key,
                status,
                1,
                json.dumps(base),
                json.dumps(left),
                json.dumps(right),
                json.dumps(document),
                json.dumps(conflicts),
                json.dumps(result) if result is not None else None,
                json.dumps(result_schema) if result_schema is not None else None,
                now,
                now,
            ),
        )
    except Exception:
        # Concurrent insert with the same idempotency key: replay wins.
        if idempotency_key:
            row = db.query_one(
                "SELECT * FROM merges WHERE idempotency_key = ?", (idempotency_key,)
            )
            if row is not None:
                return _row_to_dict(row), False
        raise
    return get_merge(merge_id), True


def _apply_batch(
    document: Any, conflicts: list[dict], resolutions: list[dict]
) -> tuple[Any, list[dict]]:
    """Validate and apply a batch of resolutions in memory.

    Returns (document, remaining_conflicts). Pure: the caller decides
    whether the outcome is persisted (apply_resolutions) or discarded
    (preview_resolutions). A repeated pointer fails because the first
    occurrence closes the conflict.
    """
    open_conflicts = {c["pointer"]: c for c in conflicts}
    for item in resolutions:
        pointer = item["pointer"]
        conflict = open_conflicts.pop(pointer, None)
        if conflict is None:
            raise UnknownConflictPointer(pointer)
        tokens = parse_pointer(pointer)

        if item.get("choice") is not None:
            side = item["choice"]
            if side in conflict["missing"]:
                # Chosen side deleted the key: honour the deletion.
                document = delete_at(document, tokens)
            else:
                document = set_at(document, tokens, conflict[side])
        elif item.get("value_provided"):
            document = set_at(document, tokens, item["value"])
        else:
            raise InvalidResolution(
                f"resolution for {pointer!r} needs 'choice' or 'value'"
            )

    remaining = [c for c in conflicts if c["pointer"] in open_conflicts]
    return document, remaining


def apply_resolutions(
    merge_id: str, expected_revision: int, resolutions: list[dict]
) -> dict:
    """Apply a batch of conflict resolutions.

    Raises StaleRevision (409) if expected_revision does not match the
    current revision, and AlreadyResolved (409) once the result is final.
    """
    row = db.query_one("SELECT * FROM merges WHERE id = ?", (merge_id,))
    if row is None:
        raise MergeNotFound(merge_id)
    if row["status"] == STATUS_RESOLVED:
        raise AlreadyResolved(merge_id)
    if row["revision"] != expected_revision:
        raise StaleRevision(row["revision"])

    document, remaining = _apply_batch(
        json.loads(row["document"]), json.loads(row["conflicts"]), resolutions
    )
    resolved = not remaining
    # Gate: only a batch that resolves the last conflict produces a final
    # document, so only that batch is validated. A partial batch still
    # carries conflict placeholders and is committed unchecked.
    if resolved and row["result_schema"] is not None:
        validate_result(json.loads(row["result_schema"]), document)
    db.execute(
        """
        UPDATE merges
        SET status = ?, revision = revision + 1, document = ?,
            conflicts = ?, result = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            STATUS_RESOLVED if resolved else STATUS_PENDING,
            json.dumps(document),
            json.dumps(remaining),
            # Once written the result is never updated again: immutable.
            json.dumps(document) if resolved else None,
            _now(),
            merge_id,
        ),
    )
    return get_merge(merge_id)


def preview_resolutions(
    merge_id: str, expected_revision: int, resolutions: list[dict]
) -> dict:
    """Dry-run a resolution batch on an in-memory copy of the merge.

    Same revision/status guards and per-item validation as
    apply_resolutions, but stricter: an item carrying both 'choice' and
    'value' is rejected. Nothing is persisted — no write, no revision
    bump, no status change.
    """
    row = db.query_one("SELECT * FROM merges WHERE id = ?", (merge_id,))
    if row is None:
        raise MergeNotFound(merge_id)
    if row["status"] == STATUS_RESOLVED:
        raise AlreadyResolved(merge_id)
    if row["revision"] != expected_revision:
        raise StaleRevision(row["revision"])

    for item in resolutions:
        if item.get("choice") is not None and item.get("value_provided"):
            raise InvalidResolution(
                f"resolution for {item['pointer']!r} gives both 'choice' and 'value'"
            )

    document, remaining = _apply_batch(
        json.loads(row["document"]), json.loads(row["conflicts"]), resolutions
    )
    would_resolve = not remaining
    preview = {
        "merge_id": merge_id,
        "revision": row["revision"],
        "document": document,
        "remaining_conflicts": remaining,
        "would_resolve": would_resolve,
    }
    if would_resolve:
        # Same deterministic patch/digest the resolved merge would yield.
        base = json.loads(row["base_doc"])
        preview["patch"] = generate_patch(base, document)
        preview["result_sha256"] = sha256_hex(document)
    return preview


def apply_rule_resolutions(
    merge_id: str, expected_revision: int, rule_items: list[dict]
) -> dict:
    """Resolve open conflicts with ordered path-pattern rules.

    Every open conflict is matched against the ordered rules and the unique
    best match (more literal segments, then fewer '*', then declaration
    order) supplies its side. All hits are validated before any write: an
    invalid pattern, a rule without any pattern or a failure applying a
    hit rejects the whole request with no partial modification. The
    revision is bumped at most once, inside the single UPDATE transaction.
    """
    row = db.query_one("SELECT * FROM merges WHERE id = ?", (merge_id,))
    if row is None:
        raise MergeNotFound(merge_id)
    if row["status"] == STATUS_RESOLVED:
        raise AlreadyResolved(merge_id)
    if row["revision"] != expected_revision:
        raise StaleRevision(row["revision"])

    # Compiling and planning happens entirely in memory; nothing is touched
    # until every selected conflict has been proven applicable.
    compiled_rules = compile_rules(rule_items)
    conflicts = json.loads(row["conflicts"])
    winners = plan_resolutions(conflicts, compiled_rules)
    rule_matches = group_matches(winners)
    unmatched = [c["pointer"] for c in conflicts if c["pointer"] not in {w.pointer for w in winners}]

    if not winners:
        # Nothing matched: nothing to validate or apply, no state change and
        # no revision bump; the unmatched conflicts are reported back.
        view = get_merge(merge_id)
        view["rule_matches"] = []
        view["unmatched_conflicts"] = unmatched
        view["applied"] = False
        return view

    document, remaining = apply_plan(json.loads(row["document"]), conflicts, winners)
    resolved = not remaining
    # Same gate as explicit resolutions: only the batch that resolves the
    # very last conflict is checked against the final-result schema.
    if resolved and row["result_schema"] is not None:
        validate_result(json.loads(row["result_schema"]), document)

    # Single atomic update: revision increments exactly once.
    db.execute(
        """
        UPDATE merges
        SET status = ?, revision = revision + 1, document = ?,
            conflicts = ?, result = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            STATUS_RESOLVED if resolved else STATUS_PENDING,
            json.dumps(document),
            json.dumps(remaining),
            json.dumps(document) if resolved else None,
            _now(),
            merge_id,
        ),
    )
    view = get_merge(merge_id)
    view["rule_matches"] = rule_matches
    view["unmatched_conflicts"] = unmatched
    view["applied"] = True
    return view


# ---------------------------------------------------------------------------
# Resolution drafts: a persistent, editable set of conflict resolutions that
# pins the merge revision at creation time and commits atomically.
# ---------------------------------------------------------------------------


def _draft_row_to_dict(row) -> dict:
    stored = json.loads(row["resolutions"])
    resolutions = []
    for pointer, entry in stored.items():
        item = {"pointer": pointer}
        if "choice" in entry:
            item["choice"] = entry["choice"]
        else:
            item["value"] = entry["value"]
        resolutions.append(item)
    return {
        "draft_id": row["id"],
        "merge_id": row["merge_id"],
        "status": row["status"],
        "base_revision": row["base_revision"],
        "resolutions": resolutions,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _stored_to_batch(stored: dict) -> list[dict]:
    """Convert the draft's stored resolutions into _apply_batch items.

    ``stored`` maps pointer -> {"choice": side} or {"value": value}; a
    ``value`` key is always explicit (it may hold a JSON null), mirroring
    the ``value_provided`` semantics of the resolution endpoints.
    """
    return [
        {
            "pointer": pointer,
            "choice": entry.get("choice"),
            "value": entry.get("value"),
            "value_provided": "value" in entry,
        }
        for pointer, entry in stored.items()
    ]


def _get_draft_rows(merge_id: str, draft_id: str):
    """Fetch (merge_row, draft_row) or raise MergeNotFound / DraftNotFound."""
    merge_row = db.query_one("SELECT * FROM merges WHERE id = ?", (merge_id,))
    if merge_row is None:
        raise MergeNotFound(merge_id)
    draft_row = db.query_one(
        "SELECT * FROM merge_drafts WHERE id = ?", (draft_id,)
    )
    if draft_row is None or draft_row["merge_id"] != merge_id:
        raise DraftNotFound(draft_id)
    return merge_row, draft_row


def create_draft(merge_id: str) -> dict:
    """Open a resolution draft pinned to the merge's current revision.

    The draft snapshots the working document and the open conflicts, so
    later edits and previews are stable even if the merge moves on. Only a
    pending merge accepts drafts; a resolved one yields AlreadyResolved.
    """
    row = db.query_one("SELECT * FROM merges WHERE id = ?", (merge_id,))
    if row is None:
        raise MergeNotFound(merge_id)
    if row["status"] == STATUS_RESOLVED:
        raise AlreadyResolved(merge_id)
    draft_id = uuid.uuid4().hex
    now = _now()
    db.execute(
        """
        INSERT INTO merge_drafts (
            id, merge_id, status, base_revision,
            document, conflicts, resolutions, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            draft_id,
            merge_id,
            DRAFT_OPEN,
            row["revision"],
            row["document"],
            row["conflicts"],
            "{}",
            now,
            now,
        ),
    )
    return get_draft(merge_id, draft_id)


def get_draft(merge_id: str, draft_id: str) -> dict:
    _, draft_row = _get_draft_rows(merge_id, draft_id)
    return _draft_row_to_dict(draft_row)


def update_draft(merge_id: str, draft_id: str, items: list[dict]) -> dict:
    """Append or overwrite conflict resolutions on the draft.

    Each item follows the same choice/value semantics as a resolution
    batch: exactly one of 'choice' / 'value' (an explicit null counts as
    'value'). A pointer already present in the draft is overwritten in
    place; a new one is appended. Only the draft row is written — the
    merge order is never touched by draft edits.
    """
    _, draft_row = _get_draft_rows(merge_id, draft_id)
    if draft_row["status"] != DRAFT_OPEN:
        raise DraftNotOpen(draft_id)
    open_pointers = {c["pointer"] for c in json.loads(draft_row["conflicts"])}
    stored = json.loads(draft_row["resolutions"])
    for item in items:
        pointer = item["pointer"]
        if pointer not in open_pointers:
            raise UnknownConflictPointer(pointer)
        if item.get("choice") is not None and item.get("value_provided"):
            raise InvalidResolution(
                f"resolution for {pointer!r} gives both 'choice' and 'value'"
            )
        if item.get("choice") is None and not item.get("value_provided"):
            raise InvalidResolution(
                f"resolution for {pointer!r} needs 'choice' or 'value'"
            )
        if item.get("choice") is not None:
            stored[pointer] = {"choice": item["choice"]}
        else:
            stored[pointer] = {"value": item["value"]}
    db.execute(
        "UPDATE merge_drafts SET resolutions = ?, updated_at = ? WHERE id = ?",
        (json.dumps(stored), _now(), draft_id),
    )
    return get_draft(merge_id, draft_id)


def preview_draft(merge_id: str, draft_id: str) -> dict:
    """Dry-run the draft's resolutions against its creation-time snapshot.

    Nothing is written. Returns the would-be document and the conflicts
    still open after the draft (pointers may be filled in over several
    edits, so a partial draft simply reports the rest); when the draft
    would resolve everything, the deterministic base-to-result patch and
    its SHA-256 are included, mirroring the resolution-preview endpoint.
    """
    merge_row, draft_row = _get_draft_rows(merge_id, draft_id)
    if draft_row["status"] != DRAFT_OPEN:
        raise DraftNotOpen(draft_id)
    document, remaining = _apply_batch(
        json.loads(draft_row["document"]),
        json.loads(draft_row["conflicts"]),
        _stored_to_batch(json.loads(draft_row["resolutions"])),
    )
    would_resolve = not remaining
    preview = {
        "merge_id": merge_id,
        "draft_id": draft_id,
        "base_revision": draft_row["base_revision"],
        "current_revision": merge_row["revision"],
        "document": document,
        "remaining_conflicts": remaining,
        "would_resolve": would_resolve,
    }
    if would_resolve:
        base = json.loads(merge_row["base_doc"])
        preview["patch"] = generate_patch(base, document)
        preview["result_sha256"] = sha256_hex(document)
    return preview


def commit_draft(merge_id: str, draft_id: str) -> dict:
    """Apply the whole draft to the merge in a single transaction.

    Inside the transaction the merge row is re-read and re-validated: the
    draft must still be open, the merge still pending, the revision still
    equal to the draft's pinned base_revision, every drafted pointer still
    an open conflict, and — when the draft resolves the last conflict —
    the candidate result must satisfy result_schema. Any changed condition
    aborts with 409 (a schema violation with 422) and neither the merge
    nor the draft is partially written. On success all resolutions are
    applied with exactly one revision bump and the draft is invalidated.
    """
    with db.transaction() as conn:
        merge_row = conn.execute(
            "SELECT * FROM merges WHERE id = ?", (merge_id,)
        ).fetchone()
        if merge_row is None:
            raise MergeNotFound(merge_id)
        draft_row = conn.execute(
            "SELECT * FROM merge_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if draft_row is None or draft_row["merge_id"] != merge_id:
            raise DraftNotFound(draft_id)
        if draft_row["status"] != DRAFT_OPEN:
            raise DraftNotOpen(draft_id)
        if merge_row["status"] == STATUS_RESOLVED:
            raise AlreadyResolved(merge_id)
        if merge_row["revision"] != draft_row["base_revision"]:
            raise StaleRevision(merge_row["revision"])

        conflicts = json.loads(merge_row["conflicts"])
        open_pointers = {c["pointer"] for c in conflicts}
        stored = json.loads(draft_row["resolutions"])
        if not stored:
            raise InvalidResolution("draft has no resolutions to commit")
        if any(pointer not in open_pointers for pointer in stored):
            raise DraftConflictSetChanged(
                "the merge's open conflicts no longer cover the draft"
            )

        document, remaining = _apply_batch(
            json.loads(merge_row["document"]), conflicts, _stored_to_batch(stored)
        )
        resolved = not remaining
        # Same gate as resolution batches: only the commit that resolves
        # the last conflict is checked against the final-result schema.
        if resolved and merge_row["result_schema"] is not None:
            validate_result(json.loads(merge_row["result_schema"]), document)

        now = _now()
        conn.execute(
            """
            UPDATE merges
            SET status = ?, revision = revision + 1, document = ?,
                conflicts = ?, result = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                STATUS_RESOLVED if resolved else STATUS_PENDING,
                json.dumps(document),
                json.dumps(remaining),
                # Once written the result is never updated again: immutable.
                json.dumps(document) if resolved else None,
                now,
                merge_id,
            ),
        )
        conn.execute(
            "UPDATE merge_drafts SET status = ?, updated_at = ? WHERE id = ?",
            (DRAFT_COMMITTED, now, draft_id),
        )
    view = get_merge(merge_id)
    view["draft_id"] = draft_id
    return view
