"""Per-row intake classification shared by preview and partial import.

The intake layer separates two questions that an all-or-nothing importer
conflates:

* which source rows are acceptable story revisions, and
* which acceptable rows can be stored without overwriting an existing revision.

Every source row gets a status, and the batch state is derived from those
statuses.  Rejected rows are never written to ``stories`` (so they cannot enter
a report denominator), but they are recorded in the batch ledger so that the
submission stays reviewable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable

from story_patterns.catalog.validate import canonical_json, digest_json, normalize_story, _text
from story_patterns.errors import ValidationError

ACCEPTED_ROW = "accepted"
ALREADY_PRESENT_ROW = "already_present"
INVALID_ROW = "invalid"
DUPLICATE_ROW = "duplicate_in_batch"
CONFLICT_ROW = "conflict"

ACCEPTED_STATUSES = frozenset({ACCEPTED_ROW, ALREADY_PRESENT_ROW})
REJECTED_STATUSES = frozenset({INVALID_ROW, DUPLICATE_ROW, CONFLICT_ROW})

BATCH_ACCEPTED = "accepted"
BATCH_PARTIAL = "partial"
BATCH_REJECTED = "rejected"

# A malformed NDJSON line has no JSON value.  Its raw text is wrapped in this
# sentinel so the source digest still covers every physical line and the parser
# can round-trip the rejection through the ordinary row interface.
UNPARSEABLE_MARKER = "__unparseable__"
PARSE_ERROR_KEY = "__error__"


def unparseable_row(raw_text: str | None, error: str | None) -> dict:
    return {UNPARSEABLE_MARKER: raw_text, PARSE_ERROR_KEY: error}

ExistingLookup = Callable[[str, int], "str | None"]


@dataclass(frozen=True)
class SourceRow:
    """One physical source row, numbered from 1 in file order."""

    row_number: int
    raw: Any = None
    parse_error: str | None = None
    raw_text: str | None = None

    def payload(self) -> Any:
        if self.parse_error is not None and self.raw is None:
            return {UNPARSEABLE_MARKER: self.raw_text}
        return self.raw

    def candidate_id(self) -> str | None:
        if isinstance(self.raw, dict):
            value = self.raw.get("story_id")
            return value if isinstance(value, str) else None
        return None

    def candidate_revision(self) -> int | None:
        if isinstance(self.raw, dict):
            value = self.raw.get("revision")
            return value if type(value) is int else None
        return None


def rows_from_objects(rows: list[Any]) -> list[SourceRow]:
    """Wrap parsed rows, recovering malformed NDJSON lines from their marker.

    ``__unparseable__`` is the sentinel the file reader and CLI use for a line
    that could not be parsed at all; it must stay a rejected row rather than
    abort the submission.
    """
    wrapped: list[SourceRow] = []
    for index, raw in enumerate(rows, start=1):
        if isinstance(raw, dict) and UNPARSEABLE_MARKER in raw:
            wrapped.append(SourceRow(row_number=index, parse_error=raw.get(PARSE_ERROR_KEY),
                                     raw_text=raw.get(UNPARSEABLE_MARKER)))
        else:
            wrapped.append(SourceRow(row_number=index, raw=raw))
    return wrapped


def normalize_batch_envelope(raw: Any) -> tuple[str, list[SourceRow], str | None]:
    """Validate only the batch envelope, leaving row errors to classification."""
    if not isinstance(raw, dict):
        raise ValidationError("batch must be an object")
    batch_id = _text(raw.get("batch_id"), "batch_id", 120)
    stories = raw.get("stories")
    if not isinstance(stories, list) or not stories:
        raise ValidationError("stories must be a non-empty list")
    retry_of = raw.get("retry_of")
    if retry_of is not None:
        retry_of = _text(retry_of, "retry_of", 120)
        if retry_of == batch_id:
            raise ValidationError("retry_of must identify a different batch")
    return batch_id, rows_from_objects(stories), retry_of


def classify_rows(rows: list[SourceRow], existing: ExistingLookup | None = None) -> list[dict]:
    """Classify each row without writing anything.

    The order is deterministic:

    1. malformed JSON or failed story normalization -> ``invalid``
    2. the same (story_id, revision) appearing more than once -> every
       occurrence is ``duplicate_in_batch``; a repeated row is never resolved
       by silently picking one of the copies
    3. an existing revision with a different canonical document -> ``conflict``
    4. an existing revision with the identical document -> ``already_present``
    5. otherwise ``accepted``
    """
    entries: list[dict] = []
    for row in rows:
        payload = row.payload()
        entry = {
            "row": row.row_number,
            "story_id": row.candidate_id(),
            "revision": row.candidate_revision(),
            "status": None,
            "reason": None,
            "story": None,
            "payload": payload,
            "parse_error": row.parse_error,
            "row_digest": digest_json(payload),
            "story_digest": None,
        }
        if row.parse_error is not None:
            entry["status"] = INVALID_ROW
            entry["reason"] = row.parse_error
        else:
            try:
                story = normalize_story(row.raw)
            except ValidationError as error:
                entry["status"] = INVALID_ROW
                entry["reason"] = str(error)
            else:
                entry["story"] = story
                entry["story_id"] = story["story_id"]
                entry["revision"] = story["revision"]
                entry["story_digest"] = digest_json(story)
        entries.append(entry)

    identities = defaultdict(list)
    for entry in entries:
        if entry["story"] is not None:
            identities[(entry["story"]["story_id"], entry["story"]["revision"])].append(entry["row"])
    repeated_rows = {number for numbers in identities.values() if len(numbers) > 1 for number in numbers}

    for entry in entries:
        if entry["status"] is not None:
            continue
        identity = (entry["story"]["story_id"], entry["story"]["revision"])
        if entry["row"] in repeated_rows:
            entry["status"] = DUPLICATE_ROW
            entry["reason"] = "story revision is repeated in this batch"
            continue
        stored = existing(*identity) if existing is not None else None
        if stored is not None:
            if stored != canonical_json(entry["story"]):
                entry["status"] = CONFLICT_ROW
                entry["reason"] = (
                    f"story {identity[0]} revision {identity[1]} already exists with different content; "
                    "use a new revision number or a corrected source record"
                )
            else:
                entry["status"] = ALREADY_PRESENT_ROW
            continue
        entry["status"] = ACCEPTED_ROW
    return entries


def batch_state(entries: list[dict]) -> str:
    accepted = sum(entry["status"] in ACCEPTED_STATUSES for entry in entries)
    if accepted == 0:
        return BATCH_REJECTED
    if accepted == len(entries):
        return BATCH_ACCEPTED
    return BATCH_PARTIAL


def source_digest(batch_id: str, entries: list[dict]) -> str:
    """Digest the whole submission in file order, rejected rows included.

    This answers "is this the same source content?" and deliberately excludes
    metadata such as ``retry_of``: annotating a correction must not make the
    confirmed source digest look like different content.  The batch identifier
    is included so a digest cannot be transplanted to another batch.
    """
    payload = {
        "batch_id": batch_id,
        "rows": [{"row": entry["row"], "row_digest": entry["row_digest"]} for entry in entries],
    }
    return digest_json(payload)


def accepted_digest(batch_id: str, entries: list[dict]) -> str:
    """Digest only the rows this batch actually linked into the catalog.

    The payload shape matches the legacy batch digest so that databases written
    by the all-or-nothing importer can still be audited after upgrade.
    """
    stories = [
        entry["story"]
        for entry in sorted(
            (item for item in entries if item["status"] in ACCEPTED_STATUSES),
            key=lambda item: (item["story"]["story_id"], item["story"]["revision"]),
        )
    ]
    return digest_json({"batch_id": batch_id, "stories": stories})


def summarize(entries: list[dict]) -> dict:
    accepted = [entry for entry in entries if entry["status"] in ACCEPTED_STATUSES]
    counts = {status: 0 for status in (ACCEPTED_ROW, ALREADY_PRESENT_ROW, INVALID_ROW,
                                       DUPLICATE_ROW, CONFLICT_ROW)}
    for entry in entries:
        counts[entry["status"]] += 1
    return {
        "rows_seen": len(entries),
        "accepted_rows": len(accepted),
        "rejected_rows": len(entries) - len(accepted),
        "state": batch_state(entries),
        "status_counts": counts,
    }


def rejected_projection(entries: list[dict]) -> list[dict]:
    return [
        {
            "row": entry["row"],
            "story_id": entry["story_id"],
            "revision": entry["revision"],
            "status": entry["status"],
            "reason": entry["reason"],
            "payload": entry["payload"],
        }
        for entry in entries
        if entry["status"] in REJECTED_STATUSES
    ]
