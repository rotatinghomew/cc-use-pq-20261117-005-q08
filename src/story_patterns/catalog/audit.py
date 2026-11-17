from __future__ import annotations

import json
from collections import defaultdict

from story_patterns.catalog.intake import (
    ACCEPTED_ROW,
    ALREADY_PRESENT_ROW,
    batch_state,
    source_digest,
)
from story_patterns.catalog.validate import digest_json, normalize_story
from story_patterns.errors import ValidationError
from story_patterns.review.adjudication import normalize_adjudication
from story_patterns.review.validate import normalize_decision

STORED_STATUSES = frozenset({ACCEPTED_ROW, ALREADY_PRESENT_ROW})
KNOWN_STATUSES = frozenset({ACCEPTED_ROW, ALREADY_PRESENT_ROW, "invalid", "duplicate_in_batch", "conflict"})


def audit_catalog(snapshot: dict) -> dict:
    issues = []
    stories = {}
    for row in snapshot["stories"]:
        identity = (row["story_id"], row["revision"])
        try:
            document = json.loads(row["document"])
            normalized = normalize_story(document)
        except (json.JSONDecodeError, ValidationError, TypeError) as error:
            issues.append(_issue("story", identity, f"invalid story document: {error}"))
            continue
        if normalized != document or (normalized["title"], normalized["tradition"], normalized["source"]["kind"]) != (
                row["title"], row["tradition"], row["source_kind"]):
            issues.append(_issue("story", identity, "document and indexed columns disagree"))
        stories[identity] = normalized
    batch_rows = {row["batch_id"]: row for row in snapshot["batches"]}
    ledger_by_batch = defaultdict(list)
    for row in snapshot.get("batch_rows", []):
        ledger_by_batch[row["batch_id"]].append(row)
    grouped = defaultdict(list)
    for relation in snapshot["batch_stories"]:
        identity = (relation["story_id"], relation["revision"])
        grouped[relation["batch_id"]].append(identity)
        if identity not in stories:
            issues.append(_issue("batch", relation["batch_id"], f"references missing story revision {identity}"))
    for batch_id, row in batch_rows.items():
        identities = sorted(grouped.get(batch_id, []))
        if row["story_count"] != len(identities):
            issues.append(_issue("batch", batch_id, "declared story count differs from linked records"))
        ledger = ledger_by_batch.get(batch_id, [])
        if row.get("state") in (None, "legacy") and not ledger:
            # Pre-ledger batch: only accepted content can be verified.
            if identities and all(identity in stories for identity in identities):
                payload = {"batch_id": batch_id,
                           "stories": [stories[identity] for identity in identities]}
                if digest_json(payload) != row["digest"]:
                    issues.append(_issue("batch", batch_id, "content digest differs from linked story revisions"))
            continue
        issues.extend(_check_ledger(batch_id, row, ledger, identities, batch_rows, stories))
        if not identities or any(identity not in stories for identity in identities):
            continue
        payload = {"batch_id": batch_id, "stories": [stories[identity] for identity in identities]}
        if digest_json(payload) != row["digest"]:
            issues.append(_issue("batch", batch_id, "content digest differs from linked story revisions"))
    origins_by_identity = grouped_by_identity(grouped)
    for identity in stories:
        if not origins_by_identity.get(identity):
            issues.append(_issue("story", identity, "story revision has no import-batch provenance"))
    for row in snapshot["decisions"]:
        identity = (row["story_id"], row["story_revision"])
        story = stories.get(identity)
        if story is None:
            issues.append(_issue("decision", row["decision_id"], "references missing story revision"))
            continue
        try:
            payload = {key: row[key] for key in ("decision_id", "story_id", "story_revision", "annotator", "sequence",
                                                  "pattern", "youngest_outcome", "note")}
            payload["evidence_event_ids"] = json.loads(row["evidence_event_ids"])
            normalized = normalize_decision(payload, story)
            if digest_json(normalized) != row["digest"]:
                issues.append(_issue("decision", row["decision_id"], "stored decision digest does not match content"))
        except (json.JSONDecodeError, ValidationError, TypeError) as error:
            issues.append(_issue("decision", row["decision_id"], f"invalid decision: {error}"))
    for row in snapshot["adjudications"]:
        identity = (row["story_id"], row["story_revision"])
        story = stories.get(identity)
        if story is None:
            issues.append(_issue("adjudication", row["adjudication_id"], "references missing story revision"))
            continue
        try:
            payload = {key: row[key] for key in ("adjudication_id", "story_id", "story_revision", "moderator",
                                                  "sequence", "pattern", "youngest_outcome", "rationale")}
            payload["evidence_event_ids"] = json.loads(row["evidence_event_ids"])
            normalized = normalize_adjudication(payload, story)
            if digest_json(normalized) != row["digest"]:
                issues.append(_issue("adjudication", row["adjudication_id"], "stored digest does not match content"))
        except (json.JSONDecodeError, ValidationError, TypeError) as error:
            issues.append(_issue("adjudication", row["adjudication_id"], f"invalid adjudication: {error}"))
    story_keys = {f"{story_id}@{revision}" for story_id, revision in stories}
    decision_ids = {row["decision_id"] for row in snapshot["decisions"]}
    adjudication_ids = {row["adjudication_id"] for row in snapshot["adjudications"]}
    for row in snapshot["reports"]:
        try:
            document = json.loads(row["document"])
            body = {key: value for key, value in document.items() if key != "report_id"}
            if document.get("report_id") != row["report_id"] or digest_json(body)[:24] != row["report_id"]:
                issues.append(_issue("report", row["report_id"], "report content does not match its identifier"))
                continue
            provenance = document["provenance"]
            if not set(provenance["story_revisions"]).issubset(story_keys):
                issues.append(_issue("report", row["report_id"], "references missing story revisions"))
            if not set(provenance["decision_ids"]).issubset(decision_ids):
                issues.append(_issue("report", row["report_id"], "references missing reader decisions"))
            if not set(provenance["adjudication_ids"]).issubset(adjudication_ids):
                issues.append(_issue("report", row["report_id"], "references missing adjudications"))
            for identity, batch_ids in provenance.get("batch_ids_by_story", {}).items():
                if identity not in story_keys or not set(batch_ids).issubset(batch_rows):
                    issues.append(_issue("report", row["report_id"], "references missing batch provenance"))
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            issues.append(_issue("report", row["report_id"], f"invalid report document: {error}"))
    issues.sort(key=lambda item: (item["scope"], str(item["identity"]), item["problem"]))
    return {"ok": not issues, "story_revisions_checked": len(snapshot["stories"]),
            "batches_checked": len(snapshot["batches"]), "decisions_checked": len(snapshot["decisions"]),
            "batch_links_checked": len(snapshot["batch_stories"]),
            "batch_rows_checked": len(snapshot.get("batch_rows", [])),
            "adjudications_checked": len(snapshot["adjudications"]), "reports_checked": len(snapshot["reports"]),
            "issue_count": len(issues), "issues": issues}


def grouped_by_identity(grouped: dict) -> dict:
    result = defaultdict(list)
    for batch_id, identities in grouped.items():
        for identity in identities:
            result[identity].append(batch_id)
    return result


def _check_ledger(batch_id: str, batch: dict, ledger: list[dict], identities: list[tuple[str, int]],
                  batch_rows: dict, stories: dict) -> list[dict]:
    """Cross-check a batch's row ledger against its link table and digests."""
    issues = []
    row_numbers = [row["row_number"] for row in ledger]
    if row_numbers != list(range(1, len(ledger) + 1)):
        issues.append(_issue("batch", batch_id, "row ledger numbers are not contiguous from one"))
    if batch.get("source_row_count") not in (None, len(ledger)):
        issues.append(_issue("batch", batch_id, "source row count differs from ledger length"))
    linked = set(identities)
    entries = []
    for row in ledger:
        entries.append({"row": row["row_number"], "row_digest": row["row_digest"]})
        status = row["status"]
        if status not in KNOWN_STATUSES:
            issues.append(_issue("batch", batch_id, f"row {row['row_number']} has unknown status {status!r}"))
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            issues.append(_issue("batch", batch_id, f"row {row['row_number']} payload is not valid JSON"))
            continue
        if digest_json(payload) != row["row_digest"]:
            issues.append(_issue("batch", batch_id, f"row {row['row_number']} payload digest differs"))
        identity = (row["story_id"], row["revision"])
        if status in STORED_STATUSES:
            if identity not in linked:
                issues.append(_issue(
                    "batch", batch_id,
                    f"row {row['row_number']} is {status} but is not linked to a story revision"))
            if row["story_digest"] is None or story_digest_of(payload) != row["story_digest"]:
                issues.append(_issue("batch", batch_id, f"row {row['row_number']} story digest differs"))
        else:
            if identity in linked:
                issues.append(_issue(
                    "batch", batch_id,
                    f"row {row['row_number']} is {status} but its story revision was linked"))
    if batch.get("source_digest") is not None and source_digest(batch_id, entries) != batch["source_digest"]:
        issues.append(_issue("batch", batch_id, "source digest differs from row ledger"))
    if batch.get("state") in {"accepted", "partial", "rejected"}:
        derived = batch_state([{"status": row["status"]} for row in ledger])
        if derived != batch["state"]:
            issues.append(_issue("batch", batch_id, "recorded state differs from row statuses"))
        elif derived == "rejected" and identities:
            issues.append(_issue("batch", batch_id, "rejected batch still links stored revisions"))
    if batch.get("retry_of") is not None and batch["retry_of"] not in batch_rows:
        issues.append(_issue("batch", batch_id, f"retry_of batch {batch['retry_of']} was not recorded"))
    return issues


def story_digest_of(document) -> str | None:
    try:
        return digest_json(normalize_story(document))
    except (ValidationError, TypeError):
        return None


def _issue(scope: str, identity, problem: str) -> dict:
    return {"scope": scope, "identity": identity, "problem": problem}
