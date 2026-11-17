import json
import tempfile
import unittest
from pathlib import Path

from story_patterns.catalog.intake import rows_from_objects, unparseable_row
from story_patterns.catalog.ndjson import (
    read_source_file,
    read_story_file,
    read_story_lines,
    rows_to_payload,
    source_rows_from_text,
)
from story_patterns.errors import ValidationError
from tests.support import story


class NdjsonReaderTests(unittest.TestCase):
    def test_rows_are_parsed_in_original_order_and_blank_lines_are_ignored(self):
        first = story("first")
        second = story("second")
        content = "\n" + json.dumps(first) + "\n\n" + json.dumps(second) + "\n"
        rows = read_story_lines(content)
        self.assertEqual([row["story_id"] for row in rows], ["first", "second"])

    def test_malformed_line_reports_its_physical_line_number(self):
        content = json.dumps(story("first")) + "\nnot-json\n"
        with self.assertRaisesRegex(ValidationError, "line 2"):
            read_story_lines(content)

    def test_row_must_be_an_object_not_a_scalar_or_array(self):
        for line in ["[]", "3", "null", '"text"']:
            with self.subTest(line=line):
                with self.assertRaisesRegex(ValidationError, "object"):
                    read_story_lines(line)

    def test_blank_only_stream_is_not_a_successful_empty_batch(self):
        with self.assertRaisesRegex(ValidationError, "no story records"):
            read_story_lines("\n  \n")

    def test_json_array_loader_preserves_rows_for_domain_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stories.json"
            path.write_text(json.dumps([story("a"), story("b")]), encoding="utf-8")
            rows = read_story_file(path, "json")
        self.assertEqual([row["story_id"] for row in rows], ["a", "b"])

    def test_json_loader_rejects_batch_object_to_avoid_format_ambiguity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.json"
            path.write_text(json.dumps({"batch_id": "id", "stories": []}), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "array"):
                read_story_file(path, "json")

    def test_unknown_format_fails_before_trying_to_parse_contents(self):
        with self.assertRaisesRegex(ValidationError, "format"):
            read_story_file("unneeded.json", "yaml")

    def test_file_reader_reports_missing_file_as_domain_error(self):
        with self.assertRaisesRegex(ValidationError, "cannot read"):
            read_story_file("does-not-exist.ndjson", "ndjson")

    def test_utf8_file_reader_rejects_invalid_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.ndjson"
            path.write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(ValidationError, "cannot read"):
                read_story_file(path, "ndjson")


class SourceRowReaderTests(unittest.TestCase):
    def test_malformed_line_becomes_a_rejected_row_instead_of_aborting(self):
        content = json.dumps(story("first")) + "\nnot-json\n" + json.dumps(story("second")) + "\n"
        rows = source_rows_from_text(content, "ndjson")
        self.assertEqual([row.row_number for row in rows], [1, 2, 3])
        self.assertIsNotNone(rows[1].parse_error)
        self.assertIn("line 2", rows[1].parse_error)
        self.assertIsNone(rows[1].raw)

    def test_row_numbers_count_non_blank_records_so_the_ledger_is_contiguous(self):
        content = "\n" + json.dumps(story("a")) + "\n\n\n" + json.dumps(story("b")) + "\n"
        rows = source_rows_from_text(content, "ndjson")
        self.assertEqual([row.row_number for row in rows], [1, 2])

    def test_non_object_json_line_is_a_row_error_with_its_line_number(self):
        rows = source_rows_from_text("[]\n" + json.dumps(story("a")) + "\n", "ndjson")
        self.assertIn("line 1", rows[0].parse_error)
        self.assertEqual(rows[1].candidate_id(), "a")

    def test_unparseable_marker_round_trips_through_the_envelope_layer(self):
        rows = source_rows_from_text("not-json\n", "ndjson")
        payload = rows_to_payload(rows)
        self.assertEqual(payload, [unparseable_row("not-json", rows[0].parse_error)])
        rewrapped = rows_from_objects(payload)[0]
        self.assertEqual(rewrapped.parse_error, rows[0].parse_error)
        self.assertEqual(rewrapped.raw_text, "not-json")
        self.assertIsNone(rewrapped.candidate_id())

    def test_json_array_rows_keep_their_index_as_row_number(self):
        rows = source_rows_from_text(json.dumps([story("a"), story("b")]), "json")
        self.assertEqual([row.row_number for row in rows], [1, 2])
        self.assertEqual([row.candidate_id() for row in rows], ["a", "b"])

    def test_empty_inputs_are_envelope_errors_not_empty_batches(self):
        for text, format_name in [("", "ndjson"), ("\n \n", "ndjson"), ("[]", "json")]:
            with self.subTest(text=text, format=format_name):
                with self.assertRaisesRegex(ValidationError, "no story records"):
                    source_rows_from_text(text, format_name)

    def test_file_reader_wraps_io_errors(self):
        with self.assertRaisesRegex(ValidationError, "format"):
            read_source_file("unneeded.json", "yaml")
        with self.assertRaisesRegex(ValidationError, "cannot read"):
            read_source_file("does-not-exist.ndjson", "ndjson")


if __name__ == "__main__":
    unittest.main()
