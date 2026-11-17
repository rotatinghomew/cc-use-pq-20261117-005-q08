from __future__ import annotations

import json
from datetime import datetime, timezone

from story_patterns.catalog.intake import (
    ACCEPTED_ROW,
    ALREADY_PRESENT_ROW,
    BATCH_REJECTED,
    CONFLICT_ROW,
    DUPLICATE_ROW,
    INVALID_ROW,
    accepted_digest,
    batch_state,
    classify_rows,
    normalize_batch_envelope,
    rejected_projection,
    rows_from_objects,
    source_digest,
    summarize,
)
from story_patterns.catalog.validate import canonical_json, normalize_batch
from story_patterns.errors import BatchSourceConflictError, ConflictError, NotFoundError, ValidationError

STORED_STATUSES = (ACCEPTED_ROW, ALREADY_PRESENT_ROW)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CatalogService:
    def __init__(self, store):
        self.store = store

    # -- import -----------------------------------------------------------

    def import_batch(self, raw: dict, partial: bool = False) -> dict:
        """Import a batch.

        ``partial=False`` keeps the original all-or-nothing contract: the first
        rejected row aborts the whole submission and nothing is written.
        ``partial=True`` records every row in the batch ledger but stores only
        the rows that pass validation and identity checks.
        """
        return self._import_partial(raw) if partial else self._import_strict(raw)

    def _import_strict(self, raw: dict) -> dict:
        batch_id, stories, _digest = normalize_batch(raw)
        retry_of = raw.get("retry_of")
        if retry_of is not None:
            retry_of = normalize_batch_envelope({"batch_id": batch_id, "stories": raw["stories"],
                                                 "retry_of": retry_of})[2]
        entries = classify_rows(rows_from_objects(raw["stories"]), self._existing_lookup())
        for entry in entries:
            if entry["status"] == CONFLICT_ROW:
                raise ConflictError(entry["reason"])
            if entry["status"] in (INVALID_ROW, DUPLICATE_ROW):
                raise ValidationError(entry["reason"])
        with self.store.transaction() as connection:
            previous = self._previous_batch(connection, batch_id)
            if previous is not None:
                self._verify_replay(previous, batch_id, source_digest(batch_id, entries), entries, retry_of)
                return {"batch_id": batch_id, "status": "already_imported",
                        "story_count": previous["story_count"]}
            if retry_of is not None and self._previous_batch(connection, retry_of) is None:
                raise NotFoundError(f"retry_of batch {retry_of} was not recorded")
            self._write_batch(connection, batch_id, entries, retry_of=retry_of)
        return {"batch_id": batch_id, "status": "imported", "story_count": len(stories)}

    def _import_partial(self, raw: dict) -> dict:
        batch_id, source_rows, retry_of = normalize_batch_envelope(raw)
        entries = classify_rows(source_rows, self._existing_lookup())
        submitted = source_digest(batch_id, entries)
        with self.store.transaction() as connection:
            previous = self._previous_batch(connection, batch_id)
            if previous is not None:
                self._verify_replay(previous, batch_id, submitted, entries, retry_of)
                return self._recorded_result(previous)
            if retry_of is not None and self._previous_batch(connection, retry_of) is None:
                raise NotFoundError(f"retry_of batch {retry_of} was not recorded")
            self._write_batch(connection, batch_id, entries, retry_of=retry_of,
                              source_digest_value=submitted)
        return self._batch_result(batch_id, entries, retry_of=retry_of)

    def _existing_lookup(self):
        cached: dict[tuple[str, int], str | None] = {}

        def lookup(story_id: str, revision: int) -> str | None:
            key = (story_id, revision)
            if key not in cached:
                document = self.store.story_document(story_id, revision)
                cached[key] = canonical_json(document) if document is not None else None
            return cached[key]

        return lookup

    @staticmethod
    def _previous_batch(connection, batch_id: str):
        return connection.execute("SELECT * FROM import_batches WHERE batch_id=?", (batch_id,)).fetchone()

    def _verify_replay(self, previous, batch_id: str, submitted_digest: str, entries: list[dict],
                       retry_of: str | None = None) -> None:
        recorded = previous["source_digest"]
        if recorded is None:
            # Written before the ledger existed: only accepted content is known.
            recorded = previous["digest"]
            submitted_digest = accepted_digest(batch_id, entries)
        if recorded != submitted_digest:
            raise BatchSourceConflictError(batch_id, recorded, submitted_digest)
        if previous["retry_of"] != retry_of:
            raise ConflictError(
                f"batch {batch_id} is already recorded as retry_of {previous['retry_of']!r}; "
                f"a resubmission claiming retry_of {retry_of!r} is not the same declaration"
            )

    def _write_batch(self, connection, batch_id, entries, retry_of=None, source_digest_value=None) -> None:
        stored = [entry for entry in entries if entry["status"] in STORED_STATUSES]
        digest = accepted_digest(batch_id, entries)
        source = source_digest_value or source_digest(batch_id, entries)
        connection.execute(
            "INSERT INTO import_batches(batch_id, digest, story_count, source_digest, source_row_count, "
            "state, retry_of, recorded_at) VALUES(?,?,?,?,?,?,?,?)",
            (batch_id, digest, len(stored), source, len(entries),
             batch_state(entries), retry_of, _now()),
        )
        for entry in entries:
            connection.execute(
                "INSERT INTO import_batch_rows(batch_id, row_number, story_id, revision, status, reason, "
                "row_digest, story_digest, payload, parse_error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (batch_id, entry["row"], entry["story_id"], entry["revision"], entry["status"],
                 entry["reason"], entry["row_digest"], entry["story_digest"],
                 canonical_json(entry["payload"]), entry["parse_error"]),
            )
        for entry in stored:
            story = entry["story"]
            document = canonical_json(story)
            existing = connection.execute(
                "SELECT document FROM stories WHERE story_id = ? AND revision = ?",
                (story["story_id"], story["revision"]),
            ).fetchone()
            if existing and existing["document"] != document:
                # A revision written between classification and this transaction
                # must abort the batch rather than be overwritten.
                raise ConflictError(f"story {story['story_id']} revision {story['revision']} conflicts")
            connection.execute(
                "INSERT OR IGNORE INTO stories(story_id, revision, title, tradition, source_kind, document) "
                "VALUES(?,?,?,?,?,?)",
                (story["story_id"], story["revision"], story["title"], story["tradition"],
                 story["source"]["kind"], document),
            )
            connection.execute(
                "INSERT INTO batch_stories(batch_id, story_id, revision) VALUES(?,?,?)",
                (batch_id, story["story_id"], story["revision"]),
            )

    @staticmethod
    def _batch_result(batch_id: str, entries: list[dict], retry_of: str | None = None) -> dict:
        counts = summarize(entries)
        return {
            "batch_id": batch_id,
            "status": "rejected" if counts["state"] == BATCH_REJECTED else "imported",
            "state": counts["state"],
            "story_count": counts["accepted_rows"],
            "rows_seen": counts["rows_seen"],
            "rejected_rows": counts["rejected_rows"],
            "status_counts": counts["status_counts"],
            "source_digest": source_digest(batch_id, entries),
            "accepted_digest": accepted_digest(batch_id, entries),
            "retry_of": retry_of,
            "rejected": rejected_projection(entries),
        }

    def _recorded_result(self, row) -> dict:
        ledger = self.store.batch_ledger(row["batch_id"]) or {"rows": []}
        rejected = [
            {"row": item["row_number"], "story_id": item["story_id"], "revision": item["revision"],
             "status": item["status"], "reason": item["reason"], "payload": _payload(item)}
            for item in ledger["rows"] if item["status"] not in STORED_STATUSES
        ]
        return {
            "batch_id": row["batch_id"],
            "status": "already_imported",
            "state": row["state"],
            "story_count": row["story_count"],
            "rows_seen": row["source_row_count"],
            "rejected_rows": (row["source_row_count"] or 0) - row["story_count"],
            "source_digest": row["source_digest"],
            "accepted_digest": row["digest"],
            "retry_of": row["retry_of"],
            "rejected": rejected,
        }

    # -- inspection -------------------------------------------------------

    def batch_status(self, batch_id: str) -> dict:
        ledger = self.store.batch_ledger(batch_id)
        if ledger is None:
            return {"batch_id": batch_id, "recorded": False,
                    "recovery": "no committed batch exists; resubmitting the same content is a fresh import"}
        batch = ledger["batch"]
        counts = {status: 0 for status in (ACCEPTED_ROW, ALREADY_PRESENT_ROW, INVALID_ROW,
                                           DUPLICATE_ROW, CONFLICT_ROW)}
        for item in ledger["rows"]:
            counts[item["status"]] += 1
        return {
            "batch_id": batch_id,
            "recorded": True,
            "state": batch["state"],
            "retry_of": batch["retry_of"],
            "source_digest": batch["source_digest"],
            "accepted_digest": batch["digest"],
            "source_row_count": batch["source_row_count"],
            "accepted_rows": batch["story_count"],
            "status_counts": counts,
            "recorded_at": batch["recorded_at"],
            "recovery": (
                "batch committed atomically; a retry with identical source content is idempotent"
                if batch["state"] != "legacy" else
                "legacy batch without a row ledger; only accepted content is recorded"
            ),
            "rows": [
                {"row": item["row_number"], "story_id": item["story_id"], "revision": item["revision"],
                 "status": item["status"], "reason": item["reason"],
                 "story_digest": item["story_digest"]}
                for item in ledger["rows"]
            ],
        }

    def batches(self) -> list[dict]:
        return self.store.recorded_batches()

    # -- read-only preview ------------------------------------------------

    def preview(self, rows: list[dict], batch_id: str | None = None,
                retry_of: str | None = None) -> dict:
        from story_patterns.catalog.preview import preview_rows

        if retry_of is not None and batch_id is None:
            raise ValidationError("retry_of requires a batch identifier for comparison")
        return preview_rows(rows, batch_id=batch_id, retry_of=retry_of,
                            existing=self._existing_lookup() if batch_id else None,
                            recorded=self.batch_status(batch_id) if batch_id else None,
                            retry_status=self.batch_status(retry_of) if retry_of else None)

    # -- read-only catalog ------------------------------------------------

    def get_story(self, story_id: str, revision: int) -> dict | None:
        return self.store.story_document(story_id, revision)

    def list_stories(self, tradition: str | None = None) -> list[dict]:
        return self.store.list_stories(tradition)

    def lineage(self) -> list[dict]:
        from story_patterns.catalog.history import revision_lineage

        return revision_lineage(self.store.list_stories())

    def manifest(self) -> dict:
        from story_patterns.catalog.manifest import build_manifest

        batches, stories = self.store.catalog_manifest()
        return build_manifest(batches, stories)

    def search(self, **criteria) -> list[dict]:
        from story_patterns.catalog.query import search_stories

        return search_stories(self.store.list_stories(), **criteria)

    def audit(self) -> dict:
        from story_patterns.catalog.audit import audit_catalog

        result = audit_catalog(self.store.catalog_audit_snapshot())
        database_issues = self.store.integrity_issues()
        result["database_issues"] = database_issues
        result["issues"].extend({"scope": "sqlite", "identity": "database", "problem": issue}
                                for issue in database_issues)
        result["issue_count"] = len(result["issues"])
        result["ok"] = result["issue_count"] == 0
        return result


def _payload(row: dict):
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return row["payload"]
