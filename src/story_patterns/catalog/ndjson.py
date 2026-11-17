from __future__ import annotations

import json
from pathlib import Path

from story_patterns.catalog.intake import SourceRow, unparseable_row
from story_patterns.errors import ValidationError


def read_story_lines(text: str) -> list[dict]:
    stories = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValidationError(f"NDJSON line {line_number}: {error.msg}") from error
        if not isinstance(value, dict):
            raise ValidationError(f"NDJSON line {line_number}: each row must be a story object")
        stories.append(value)
    if not stories:
        raise ValidationError("NDJSON input contains no story records")
    return stories


def source_rows_from_text(text: str, format_name: str) -> list[SourceRow]:
    """Parse rows without stopping at the first malformed one.

    Row numbers are the ordinal of each non-blank record so that a ledger is
    contiguous from one; the physical NDJSON line appears in the rejection
    reason.  A malformed line becomes a rejected ``SourceRow`` instead of
    aborting the whole submission.
    """
    if format_name == "ndjson":
        rows: list[SourceRow] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            row_number = len(rows) + 1
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                rows.append(SourceRow(
                    row_number=row_number,
                    parse_error=f"NDJSON line {line_number}: {error.msg}",
                    raw_text=line,
                ))
                continue
            if not isinstance(value, dict):
                rows.append(SourceRow(
                    row_number=row_number,
                    raw=value,
                    parse_error=f"NDJSON line {line_number}: each row must be a story object",
                    raw_text=line,
                ))
                continue
            rows.append(SourceRow(row_number=row_number, raw=value))
        if not rows:
            raise ValidationError("NDJSON input contains no story records")
        return rows
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValidationError(f"invalid JSON: {error.msg} at line {error.lineno}") from error
    if not isinstance(value, list):
        raise ValidationError("JSON story input must be an array")
    if not value:
        raise ValidationError("JSON story input contains no story records")
    return [SourceRow(row_number=index, raw=row) for index, row in enumerate(value, start=1)]


def read_story_file(path: str | Path, format_name: str) -> list[dict]:
    if format_name not in {"json", "ndjson"}:
        raise ValidationError("input format must be json or ndjson")
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValidationError(f"cannot read story file {path}: {error}") from error
    if format_name == "ndjson":
        return read_story_lines(text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValidationError(f"invalid JSON: {error.msg} at line {error.lineno}") from error
    if not isinstance(value, list):
        raise ValidationError("JSON story input must be an array")
    return value


def read_source_file(path: str | Path, format_name: str) -> list[SourceRow]:
    if format_name not in {"json", "ndjson"}:
        raise ValidationError("input format must be json or ndjson")
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValidationError(f"cannot read story file {path}: {error}") from error
    return source_rows_from_text(text, format_name)


def rows_to_payload(source_rows: list[SourceRow]) -> list:
    """Turn parsed rows back into the JSON values the import envelope expects."""
    return [row.raw if row.parse_error is None else unparseable_row(row.raw_text, row.parse_error)
            for row in source_rows]
