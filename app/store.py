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
# Persistent resolution drafts
# ---------------------------------------------------------------------------

DRAFT_OPEN = "open"
DRAFT_COMMITTED = "committed"


class DraftNotFound(Exception):
    pass


class DraftAlreadyCommitted(Exception):
    """A committed draft is spent: it rejects any further use."""


class DraftConflictNotOpen(Exception):
    """At commit time a draft pointer no longer names an open conflict."""

    def __init__(self, pointer: str):
        self.pointer = pointer
        super().__init__(f"no open conflict at pointer {pointer!r}")


def _load_merge_row(merge_id: str):
    row = db.query_one("SELECT * FROM merges WHERE id = ?", (merge_id,))
    if row is None:
        raise MergeNotFound(merge_id)
    return row


def _load_draft_row(merge_id: str, draft_id: str):
    row = db.query_one("SELECT * FROM merge_drafts WHERE id = ?", (draft_id,))
    if row is None or row["merge_id"] != merge_id:
        # A draft is only visible through its own merge order.
        raise DraftNotFound(draft_id)
    return row


def _draft_view(row) -> dict:
    resolutions = json.loads(row["resolutions"])
    conflicts = json.loads(row["conflicts"])
    return {
        "draft_id": row["id"],
        "merge_id": row["merge_id"],
        "status": row["status"],
        "pinned_revision": row["pinned_revision"],
        "resolutions": resolutions,
        "remaining_conflicts": [
            c for c in conflicts if c["pointer"] not in resolutions
        ],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _stored_resolution_items(resolutions: dict) -> list[dict]:
    """Convert the draft's stored pointer -> choice/value map into batch items."""
    items = []
    for pointer, stored in resolutions.items():
        if "choice" in stored:
            items.append(
                {
                    "pointer": pointer,
                    "choice": stored["choice"],
                    "value": None,
                    "value_provided": False,
                }
            )
        else:
            items.append(
                {
                    "pointer": pointer,
                    "choice": None,
                    "value": stored["value"],
                    "value_provided": True,
                }
            )
    return items


def create_draft(merge_id: str) -> dict:
    """Open a persistent resolution draft on a pending merge order.

    The draft pins the merge's current revision and snapshots the working
    document and conflict list; later edits only touch the draft, never the
    merge order.
    """
    row = _load_merge_row(merge_id)
    if row["status"] == STATUS_RESOLVED:
        raise AlreadyResolved(merge_id)
    draft_id = uuid.uuid4().hex
    now = _now()
    db.execute(
        """
        INSERT INTO merge_drafts (
            id, merge_id, status, pinned_revision,
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
    return _draft_view(_load_draft_row(merge_id, draft_id))


def update_draft_resolutions(
    merge_id: str, draft_id: str, items: list[dict]
) -> dict:
    """Add or overwrite conflict values in a draft.

    Each item upserts the draft's stored resolution for its pointer, so
    pointers can be filled in over multiple batches. The merge order is
    never modified. The whole batch is validated against the draft's
    pinned conflict set before anything is written: a rejected batch
    leaves the draft untouched.
    """
    _load_merge_row(merge_id)
    row = _load_draft_row(merge_id, draft_id)
    if row["status"] != DRAFT_OPEN:
        raise DraftAlreadyCommitted(draft_id)

    open_pointers = {c["pointer"] for c in json.loads(row["conflicts"])}
    resolutions = json.loads(row["resolutions"])
    for item in items:
        pointer = item["pointer"]
        if pointer not in open_pointers:
            raise UnknownConflictPointer(pointer)
        choice = item.get("choice")
        value_provided = item.get("value_provided")
        if choice is not None and value_provided:
            raise InvalidResolution(
                f"resolution for {pointer!r} gives both 'choice' and 'value'"
            )
        if choice is None and not value_provided:
            raise InvalidResolution(
                f"resolution for {pointer!r} needs 'choice' or 'value'"
            )
        # Upsert: a repeated pointer overwrites the previous draft entry.
        resolutions[pointer] = (
            {"choice": choice} if choice is not None else {"value": item["value"]}
        )
    db.execute(
        "UPDATE merge_drafts SET resolutions = ?, updated_at = ? WHERE id = ?",
        (json.dumps(resolutions), _now(), draft_id),
    )
    return _draft_view(_load_draft_row(merge_id, draft_id))


def preview_draft(merge_id: str, draft_id: str) -> dict:
    """Dry-run a draft on its pinned snapshot; nothing is persisted.

    Applies the draft's stored resolutions to the snapshotted working
    document and reports the would-be document plus the remaining
    conflicts. When the draft covers every conflict, the deterministic
    base-to-result patch and its SHA-256 are included (same rules as the
    resolution-preview endpoint).
    """
    merge_row = _load_merge_row(merge_id)
    row = _load_draft_row(merge_id, draft_id)
    if row["status"] != DRAFT_OPEN:
        raise DraftAlreadyCommitted(draft_id)

    document, remaining = _apply_batch(
        json.loads(row["document"]),
        json.loads(row["conflicts"]),
        _stored_resolution_items(json.loads(row["resolutions"])),
    )
    would_resolve = not remaining
    preview = {
        "draft_id": draft_id,
        "merge_id": merge_id,
        "pinned_revision": row["pinned_revision"],
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
    """Commit a draft: apply all stored values to the merge order atomically.

    Inside a single transaction the merge order is re-fetched and every
    condition is re-validated: the merge must still be pending, its
    revision must still equal the pinned one, every draft pointer must
    still name an open conflict and, when the draft resolves the last
    conflict, the candidate result must satisfy the merge's result_schema.
    Only then are all values applied at once (exactly one revision bump)
    and the draft marked committed. Any failure rolls the transaction
    back: neither the merge order nor the draft content is partially
    written, and the draft stays open for a corrected retry.
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
            raise DraftAlreadyCommitted(draft_id)
        if merge_row["status"] == STATUS_RESOLVED:
            raise AlreadyResolved(merge_id)
        if merge_row["revision"] != draft_row["pinned_revision"]:
            raise StaleRevision(merge_row["revision"])

        resolutions = json.loads(draft_row["resolutions"])
        if not resolutions:
            raise InvalidResolution("draft has no resolutions to commit")
        conflicts = json.loads(merge_row["conflicts"])
        open_pointers = {c["pointer"] for c in conflicts}
        for pointer in resolutions:
            if pointer not in open_pointers:
                raise DraftConflictNotOpen(pointer)

        document, remaining = _apply_batch(
            json.loads(merge_row["document"]),
            conflicts,
            _stored_resolution_items(resolutions),
        )
        resolved = not remaining
        # Same gate as explicit resolutions: only the batch resolving the
        # last conflict is checked against the final-result schema.
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
        # The draft is single-use: a successful commit spends it.
        conn.execute(
            "UPDATE merge_drafts SET status = ?, updated_at = ? WHERE id = ?",
            (DRAFT_COMMITTED, now, draft_id),
        )
    view = get_merge(merge_id)
    view["draft_id"] = draft_id
    return view
