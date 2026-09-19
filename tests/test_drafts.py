"""End-to-end tests for persistent resolution drafts on pending merges.

A draft pins the merge revision at creation time and snapshots the working
document/conflicts. Edits upsert pointer values into the draft only; the
preview dry-runs the draft on its snapshot; the commit re-validates the
revision, open conflicts and result_schema inside a single transaction and
applies every value at once, spending the draft.
"""

import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test_drafts.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # Context-manager form runs the lifespan, which creates the tables.
    with TestClient(app) as c:
        yield c


def create_merge(client, schema=None):
    """Two independent conflicts /a and /b."""
    payload = {
        "base": {"a": 1, "b": 1},
        "left": {"a": 2, "b": 2},
        "right": {"a": 3, "b": 3},
    }
    if schema is not None:
        payload["result_schema"] = schema
    resp = client.post("/merges", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_draft(client, merge_id):
    resp = client.post(f"/merges/{merge_id}/drafts")
    assert resp.status_code == 201, resp.text
    return resp.json()


def put_resolutions(client, merge_id, draft_id, resolutions):
    return client.put(
        f"/merges/{merge_id}/drafts/{draft_id}/resolutions",
        json={"resolutions": resolutions},
    )


# ---------------------------------------------------------------------------
# Creating a draft
# ---------------------------------------------------------------------------


def test_create_draft_pins_revision_and_leaves_merge_untouched(client):
    merge = create_merge(client)
    mid = merge["merge_id"]

    draft = create_draft(client, mid)
    assert draft["merge_id"] == mid
    assert draft["status"] == "open"
    assert draft["pinned_revision"] == 1
    assert draft["resolutions"] == {}
    assert sorted(c["pointer"] for c in draft["remaining_conflicts"]) == ["/a", "/b"]

    # The merge order itself is untouched by draft creation.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    assert fetched["conflict_count"] == 2


def test_create_draft_guards(client):
    # Unknown merge -> 404.
    assert client.post("/merges/does-not-exist/drafts").status_code == 404

    # Already resolved merge -> 409.
    resp = client.post(
        "/merges",
        json={"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 2}},
    )
    mid = resp.json()["merge_id"]
    assert resp.json()["status"] == "resolved"
    assert client.post(f"/merges/{mid}/drafts").status_code == 409


# ---------------------------------------------------------------------------
# Editing a draft (append / overwrite)
# ---------------------------------------------------------------------------


def test_draft_edit_appends_and_overwrites_in_batches(client):
    mid = create_merge(client)["merge_id"]
    did = create_draft(client, mid)["draft_id"]

    # First batch: one pointer.
    resp = put_resolutions(client, mid, did, [{"pointer": "/a", "choice": "left"}])
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    assert draft["resolutions"] == {"/a": {"choice": "left"}}
    assert [c["pointer"] for c in draft["remaining_conflicts"]] == ["/b"]

    # Second batch: another pointer; an explicit null counts as a value.
    resp = put_resolutions(client, mid, did, [{"pointer": "/b", "value": None}])
    assert resp.status_code == 200
    assert resp.json()["resolutions"] == {
        "/a": {"choice": "left"},
        "/b": {"value": None},
    }
    assert resp.json()["remaining_conflicts"] == []

    # A repeated pointer overwrites the previous entry.
    resp = put_resolutions(client, mid, did, [{"pointer": "/a", "value": 42}])
    assert resp.status_code == 200
    assert resp.json()["resolutions"] == {"/a": {"value": 42}, "/b": {"value": None}}

    # Editing the draft never touches the merge order.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    assert fetched["conflict_count"] == 2


def test_draft_edit_validation_reuses_resolution_errors(client):
    mid = create_merge(client)["merge_id"]
    did = create_draft(client, mid)["draft_id"]

    # Unknown pointer -> 400, same shape as the committing endpoint.
    resp = put_resolutions(client, mid, did, [{"pointer": "/nope", "value": 1}])
    assert resp.status_code == 400
    assert "no open conflict" in resp.json()["detail"]

    # Both choice and value -> 400 (an explicit null counts as value).
    resp = put_resolutions(
        client, mid, did, [{"pointer": "/a", "choice": "left", "value": None}]
    )
    assert resp.status_code == 400

    # Neither choice nor value -> 400.
    resp = put_resolutions(client, mid, did, [{"pointer": "/a"}])
    assert resp.status_code == 400

    # A batch is validated as a whole: the valid first item is not stored
    # when a later item fails.
    resp = put_resolutions(
        client,
        mid,
        did,
        [{"pointer": "/a", "choice": "left"}, {"pointer": "/nope", "value": 1}],
    )
    assert resp.status_code == 400
    resp = put_resolutions(client, mid, did, [{"pointer": "/b", "value": 9}])
    assert resp.status_code == 200
    assert resp.json()["resolutions"] == {"/b": {"value": 9}}


# ---------------------------------------------------------------------------
# Previewing a draft
# ---------------------------------------------------------------------------


def test_draft_preview_returns_remaining_conflicts(client):
    mid = create_merge(client)["merge_id"]
    did = create_draft(client, mid)["draft_id"]

    # Empty draft: every conflict still open, no patch/digest yet.
    resp = client.post(f"/merges/{mid}/drafts/{did}/preview")
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["draft_id"] == did
    assert preview["merge_id"] == mid
    assert preview["pinned_revision"] == 1
    assert preview["would_resolve"] is False
    assert sorted(c["pointer"] for c in preview["remaining_conflicts"]) == ["/a", "/b"]
    assert "patch" not in preview
    assert "result_sha256" not in preview

    # Partially filled draft: applied value visible, /b still open.
    put_resolutions(client, mid, did, [{"pointer": "/a", "choice": "right"}])
    preview = client.post(f"/merges/{mid}/drafts/{did}/preview").json()
    assert preview["document"] == {"a": 3, "b": {"__merge_conflict__": "/b"}}
    assert [c["pointer"] for c in preview["remaining_conflicts"]] == ["/b"]
    assert preview["would_resolve"] is False

    # Fully filled draft: would_resolve carries the deterministic patch.
    put_resolutions(client, mid, did, [{"pointer": "/b", "value": 7}])
    preview = client.post(f"/merges/{mid}/drafts/{did}/preview").json()
    assert preview["would_resolve"] is True
    assert preview["document"] == {"a": 3, "b": 7}
    assert preview["remaining_conflicts"] == []

    from app.patch import sha256_hex
    from tests.patch_apply import apply_patch

    assert preview["result_sha256"] == sha256_hex({"a": 3, "b": 7})
    assert apply_patch({"a": 1, "b": 1}, preview["patch"]) == {"a": 3, "b": 7}

    # Previewing is side-effect free on both the merge and the draft.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    again = client.post(f"/merges/{mid}/drafts/{did}/preview").json()
    assert again == preview


# ---------------------------------------------------------------------------
# Committing a draft
# ---------------------------------------------------------------------------


def test_draft_commit_applies_all_values_and_invalidates_draft(client):
    mid = create_merge(client)["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    put_resolutions(
        client,
        mid,
        did,
        [{"pointer": "/a", "choice": "left"}, {"pointer": "/b", "value": 9}],
    )

    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["draft_id"] == did
    assert body["status"] == "resolved"
    assert body["revision"] == 2
    assert body["conflicts"] == []
    assert body["result"] == {"a": 2, "b": 9}

    # The committed draft is spent: every further use is rejected with 409.
    assert (
        put_resolutions(client, mid, did, [{"pointer": "/a", "value": 1}]).status_code
        == 409
    )
    assert client.post(f"/merges/{mid}/drafts/{did}/preview").status_code == 409
    assert client.post(f"/merges/{mid}/drafts/{did}/commit").status_code == 409

    # A fresh draft cannot be opened on a resolved merge either.
    assert client.post(f"/merges/{mid}/drafts").status_code == 409


def test_draft_commit_partial_leaves_merge_pending(client):
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1, "c": 1},
            "left": {"a": 2, "b": 2, "c": 2},
            "right": {"a": 3, "b": 3, "c": 3},
        },
    )
    mid = resp.json()["merge_id"]
    did = create_draft(client, mid)["draft_id"]

    # Pointers are filled across batches, then committed in one go.
    put_resolutions(client, mid, did, [{"pointer": "/a", "choice": "left"}])
    put_resolutions(client, mid, did, [{"pointer": "/b", "choice": "right"}])
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["revision"] == 2
    assert [c["pointer"] for c in body["conflicts"]] == ["/c"]
    assert body["document"] == {"a": 2, "b": 3, "c": {"__merge_conflict__": "/c"}}

    # A new draft pins the new revision and can finish the merge.
    draft2 = create_draft(client, mid)
    assert draft2["pinned_revision"] == 2
    assert [c["pointer"] for c in draft2["remaining_conflicts"]] == ["/c"]
    put_resolutions(client, mid, draft2["draft_id"], [{"pointer": "/c", "value": 0}])
    resp = client.post(f"/merges/{mid}/drafts/{draft2['draft_id']}/commit")
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert resp.json()["revision"] == 3
    assert resp.json()["result"] == {"a": 2, "b": 3, "c": 0}


def test_draft_commit_stale_revision_returns_409_and_writes_nothing(client):
    mid = create_merge(client)["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    put_resolutions(
        client,
        mid,
        did,
        [{"pointer": "/a", "choice": "left"}, {"pointer": "/b", "choice": "left"}],
    )

    # The merge moves on independently: the pinned revision is now stale.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "value": 100}]},
    )
    assert resp.status_code == 200

    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 2

    # Neither the merge order nor the draft content was partially written.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 2
    assert fetched["status"] == "pending"
    assert fetched["document"]["a"] == 100
    assert [c["pointer"] for c in fetched["conflicts"]] == ["/b"]

    # The draft is still open with its values intact (preview reflects the
    # pinned snapshot, not the moved merge).
    preview = client.post(f"/merges/{mid}/drafts/{did}/preview").json()
    assert preview["would_resolve"] is True
    assert preview["document"] == {"a": 2, "b": 2}


def test_draft_commit_on_resolved_merge_returns_409(client):
    mid = create_merge(client)["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    put_resolutions(client, mid, did, [{"pointer": "/a", "choice": "left"}])

    # Resolve the merge directly; the draft commit must then fail.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 1,
            "resolutions": [
                {"pointer": "/a", "value": "x"},
                {"pointer": "/b", "value": "y"},
            ],
        },
    )
    assert resp.status_code == 200
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 409


def test_draft_commit_result_schema_gate_rolls_everything_back(client):
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    }
    mid = create_merge(client, schema=schema)["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    put_resolutions(
        client,
        mid,
        did,
        [{"pointer": "/a", "choice": "left"}, {"pointer": "/b", "choice": "right"}],
    )

    # The draft resolves the last conflict, so the candidate result must
    # satisfy the schema: integers fail with the usual 422 shape.
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "final result does not satisfy result_schema"
    assert [e["instance_path"] for e in resp.json()["errors"]] == ["/a", "/b"]

    # Nothing written: the merge is untouched and the draft stays open
    # with its content intact.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    assert fetched["conflict_count"] == 2
    preview = client.post(f"/merges/{mid}/drafts/{did}/preview").json()
    assert preview["would_resolve"] is True

    # Fix the draft in place and commit again: the gate passes.
    put_resolutions(
        client,
        mid,
        did,
        [{"pointer": "/a", "value": "x"}, {"pointer": "/b", "value": "y"}],
    )
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"
    assert resp.json()["result"] == {"a": "x", "b": "y"}


def test_draft_commit_without_resolutions_is_rejected(client):
    mid = create_merge(client)["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 400

    # The failed commit did not spend the draft.
    assert client.post(f"/merges/{mid}/drafts/{did}/preview").status_code == 200
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1


def test_draft_commit_explicit_null_value(client):
    mid = create_merge(client)["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    put_resolutions(
        client,
        mid,
        did,
        [{"pointer": "/a", "value": None}, {"pointer": "/b", "choice": "right"}],
    )
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == {"a": None, "b": 3}


def test_draft_choice_honours_missing_side_deletion(client):
    # Left deletes /a and modifies /b; right modifies both.
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"b": 2},
            "right": {"a": 3, "b": 3},
        },
    )
    mid = resp.json()["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    put_resolutions(
        client,
        mid,
        did,
        [{"pointer": "/a", "choice": "left"}, {"pointer": "/b", "choice": "left"}],
    )
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 200, resp.text
    # /a deleted via the missing-side semantics, /b takes the left value.
    assert resp.json()["result"] == {"b": 2}


# ---------------------------------------------------------------------------
# Draft addressing
# ---------------------------------------------------------------------------


def test_draft_not_found_and_cross_merge_access(client):
    mid = create_merge(client)["merge_id"]
    other = create_merge(client)["merge_id"]
    did = create_draft(client, mid)["draft_id"]

    # Unknown draft id -> 404 on every draft endpoint.
    assert client.post(f"/merges/{mid}/drafts/nope/preview").status_code == 404
    assert client.post(f"/merges/{mid}/drafts/nope/commit").status_code == 404
    assert (
        put_resolutions(client, mid, "nope", [{"pointer": "/a", "value": 1}]).status_code
        == 404
    )

    # A draft is not visible through another merge order.
    assert client.post(f"/merges/{other}/drafts/{did}/preview").status_code == 404
    assert client.post(f"/merges/{other}/drafts/{did}/commit").status_code == 404

    # Unknown merge wins over unknown draft.
    assert client.post("/merges/does-not-exist/drafts/x/commit").status_code == 404
    assert (
        put_resolutions(client, "does-not-exist", "x", [{"pointer": "/a", "value": 1}]).status_code
        == 404
    )
