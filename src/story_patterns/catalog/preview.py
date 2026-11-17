from __future__ import annotations

from story_patterns.catalog.intake import (
    ACCEPTED_ROW,
    ALREADY_PRESENT_ROW,
    classify_rows,
    rejected_projection,
    rows_from_objects,
    source_digest,
    summarize,
)


def preview_rows(rows: list[dict], batch_id: str | None = None, retry_of: str | None = None,
                 existing=None, recorded: dict | None = None, retry_status: dict | None = None) -> dict:
    """Summarize each row's outcome without writing to SQLite.

    With ``existing`` the preview can distinguish a row that would be stored
    from one whose revision is already present identically, and from one that
    would conflict.  Without it, the preview is purely structural.
    """
    entries = classify_rows(rows_from_objects(rows), existing)
    counts = summarize(entries)
    result = {
        "rows_seen": counts["rows_seen"],
        "valid_rows": counts["accepted_rows"],
        "error_count": counts["rejected_rows"],
        "ready": bool(rows) and counts["rejected_rows"] == 0,
        "state": counts["state"],
        "status_counts": counts["status_counts"],
        "errors": rejected_projection(entries),
        "normalized_stories": [entry["story"] for entry in entries
                               if entry["status"] in (ACCEPTED_ROW, ALREADY_PRESENT_ROW)],
    }
    if batch_id is not None:
        result["batch_id"] = batch_id
        result["source_digest"] = source_digest(batch_id, entries)
        result["retry_of"] = retry_of
        result["retry_assessment"] = _retry_assessment(
            result["source_digest"], recorded, retry_status, retry_of
        )
    if recorded is not None:
        result["batch_recorded"] = recorded
    return result


def _retry_assessment(submitted_digest: str, recorded: dict | None, retry_status: dict | None,
                      retry_of: str | None) -> dict:
    """State plainly whether a resubmission would be an idempotent retry."""
    if recorded is None or not recorded.get("recorded"):
        return {"kind": "new_batch",
                "idempotent": False,
                "description": "no committed batch uses this identifier; this submission would be recorded"}
    if recorded.get("source_digest") is None:
        return {"kind": "legacy_batch_unknown",
                "idempotent": None,
                "description": "the recorded batch predates source digests; only accepted content can be compared"}
    if recorded["source_digest"] == submitted_digest:
        return {"kind": "identical_retry",
                "idempotent": True,
                "description": "identical source content; nothing new would be written"}
    return {
        "kind": "different_content",
        "idempotent": False,
        "recorded_source_digest": recorded["source_digest"],
        "submitted_source_digest": submitted_digest,
        "description": "this identifier already records different source content; import would be refused",
    }
