import json
import unittest

from story_patterns.catalog.intake import (
    BATCH_ACCEPTED,
    BATCH_PARTIAL,
    BATCH_REJECTED,
    classify_rows,
    rows_from_objects,
    source_digest,
)
from story_patterns.catalog.validate import canonical_json
from story_patterns.errors import BatchSourceConflictError, ConflictError, NotFoundError, ValidationError
from tests.support import AppCase, batch, story


class PartialImportTests(AppCase, unittest.TestCase):
    def test_bad_row_is_quarantined_while_acceptable_rows_are_stored(self):
        malformed = story("tale-b")
        malformed["events"][0]["actor_id"] = "ghost"
        result = self.app.import_batch(batch([story("tale-a"), malformed, story("tale-c")], "mixed"),
                                       partial=True)
        self.assertEqual(result["state"], BATCH_PARTIAL)
        self.assertEqual(result["story_count"], 2)
        self.assertEqual(result["rows_seen"], 3)
        self.assertEqual(result["rejected_rows"], 1)
        self.assertEqual([row["story_id"] for row in result["rejected"]], ["tale-b"])
        self.assertEqual([item["story_id"] for item in self.app.catalog.list_stories()],
                         ["tale-a", "tale-c"])

    def test_rejected_rows_never_enter_sample_statistics(self):
        malformed = story("tale-b")
        malformed["events"][0]["actor_id"] = "ghost"
        self.app.import_batch(batch([story("tale-a"), malformed], "mixed"), partial=True)
        report = self.app.create_report()
        self.assertEqual(report["population"]["story_revisions"], 1)
        self.assertEqual([row["story_id"] for row in report["story_results"]], ["tale-a"])
        self.assertEqual(report["provenance"]["batch_ids_by_story"], {"tale-a@1": ["mixed"]})

    def test_duplicate_rows_are_all_quarantined_rather_than_one_silently_won(self):
        result = self.app.import_batch(batch([story("tale-a"), story("tale-a")], "repeat"), partial=True)
        self.assertEqual(result["state"], BATCH_REJECTED)
        self.assertEqual(result["story_count"], 0)
        self.assertEqual(result["status_counts"]["duplicate_in_batch"], 2)
        self.assertEqual(self.app.catalog.list_stories(), [])
        self.assertEqual({row["row"] for row in result["rejected"]}, {1, 2})

    def test_conflicting_revision_keeps_stored_content_and_reports_the_row(self):
        self.import_story(story(), "first")
        changed = story()
        changed["title"] = "Rewritten telling"
        result = self.app.import_batch(batch([story("tale-b"), changed], "second"), partial=True)
        self.assertEqual(result["status_counts"]["accepted"], 1)
        self.assertEqual(result["status_counts"]["conflict"], 1)
        self.assertEqual(result["rejected"][0]["row"], 2)
        self.assertEqual(self.app.catalog.get_story("tale-a", 1)["title"], "Local tale tale-a")

    def test_identical_revision_in_new_batch_is_already_present_not_duplicated(self):
        self.import_story(story(), "first")
        result = self.app.import_batch(batch([story()], "second"), partial=True)
        self.assertEqual(result["state"], BATCH_ACCEPTED)
        self.assertEqual(result["status_counts"]["already_present"], 1)
        self.assertEqual(len(self.app.catalog.list_stories()), 1)
        self.assertEqual(self.app.catalog_manifest()["batches"][1]["linked_story_count"], 1)

    def test_fully_rejected_batch_is_recorded_for_audit_without_linking_stories(self):
        result = self.app.import_batch({"batch_id": "empty-result", "stories": [{"story_id": "x"}]},
                                       partial=True)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["state"], BATCH_REJECTED)
        status = self.app.batch_status("empty-result")
        self.assertTrue(status["recorded"])
        self.assertEqual(status["accepted_rows"], 0)
        self.assertEqual(status["rows"][0]["status"], "invalid")
        self.assertTrue(self.app.audit()["ok"])

    def test_strict_import_remains_all_or_nothing_by_default(self):
        malformed = story("tale-b")
        malformed["events"][0]["actor_id"] = "ghost"
        with self.assertRaises(ValidationError):
            self.app.import_batch(batch([story("tale-a"), malformed], "strict"))
        self.assertEqual(self.app.catalog.list_stories(), [])
        self.assertFalse(self.app.batch_status("strict")["recorded"])

    def test_empty_envelope_is_rejected_before_any_write(self):
        for payload in [{"batch_id": "b", "stories": []}, {"batch_id": "b", "stories": {}}]:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    self.app.import_batch(payload, partial=True)
        self.assertFalse(self.app.batch_status("b")["recorded"])


class RetryAndRepairTests(AppCase, unittest.TestCase):
    def _mixed_batch(self):
        malformed = story("tale-b")
        malformed["events"][0]["actor_id"] = "ghost"
        return [story("tale-a"), malformed], malformed

    def test_identical_source_retry_is_idempotent_and_digest_is_stable(self):
        rows, _ = self._mixed_batch()
        first = self.app.import_batch(batch(rows, "batch-x"), partial=True)
        second = self.app.import_batch(batch(rows, "batch-x"), partial=True)
        self.assertEqual(second["status"], "already_imported")
        self.assertEqual(first["source_digest"], second["source_digest"])
        self.assertEqual(len(self.app.catalog.list_stories()), 1)

    def test_identical_retry_does_not_rewrite_the_ledger(self):
        self.app.import_batch(batch([story()], "batch-x"), partial=True)
        before = self.app.batch_status("batch-x")
        self.app.import_batch(batch([story()], "batch-x"), partial=True)
        after = self.app.batch_status("batch-x")
        self.assertEqual(before["recorded_at"], after["recorded_at"])
        self.assertEqual(before["source_digest"], after["source_digest"])
        self.assertEqual(len(after["rows"]), 1)

    def test_changed_content_under_same_batch_id_is_refused_with_both_digests(self):
        rows, _ = self._mixed_batch()
        first = self.app.import_batch(batch(rows, "batch-x"), partial=True)
        with self.assertRaises(BatchSourceConflictError) as context:
            self.app.import_batch(batch([story("tale-a")], "batch-x"), partial=True)
        details = context.exception.details
        self.assertFalse(details["idempotent"])
        self.assertEqual(details["recorded_source_digest"], first["source_digest"])
        self.assertNotEqual(details["submitted_source_digest"], first["source_digest"])
        self.assertEqual(self.app.batch_status("batch-x")["source_digest"], first["source_digest"])

    def test_repair_resubmission_uses_a_new_id_and_points_at_the_original(self):
        rows, malformed = self._mixed_batch()
        first = self.app.import_batch(batch(rows, "batch-x"), partial=True)
        repaired = story("tale-b")
        second = self.app.import_batch({"batch_id": "batch-x-fix", "retry_of": "batch-x",
                                        "stories": [repaired]}, partial=True)
        self.assertEqual(second["retry_of"], "batch-x")
        self.assertEqual(second["state"], BATCH_ACCEPTED)
        self.assertNotEqual(second["source_digest"], first["source_digest"])
        self.assertEqual([item["story_id"] for item in self.app.catalog.list_stories()],
                         ["tale-a", "tale-b"])
        status = self.app.batch_status("batch-x-fix")
        self.assertEqual(status["retry_of"], "batch-x")

    def test_retry_of_unknown_batch_is_refused_without_partial_write(self):
        with self.assertRaises(NotFoundError):
            self.app.import_batch({"batch_id": "fix", "retry_of": "missing", "stories": [story()]},
                                  partial=True)
        self.assertFalse(self.app.batch_status("fix")["recorded"])
        self.assertEqual(self.app.catalog.list_stories(), [])

    def test_identical_retry_must_repeat_the_same_retry_declaration(self):
        self.app.import_batch(batch([story("tale-a")], "source"), partial=True)
        first = self.app.import_batch({"batch_id": "fix", "retry_of": "source",
                                       "stories": [story("tale-a")]}, partial=True)
        same = self.app.import_batch({"batch_id": "fix", "retry_of": "source",
                                      "stories": [story("tale-a")]}, partial=True)
        self.assertEqual(same["status"], "already_imported")
        self.assertEqual(same["source_digest"], first["source_digest"])
        with self.assertRaises(ConflictError):
            self.app.import_batch({"batch_id": "fix", "stories": [story("tale-a")]}, partial=True)

    def test_retry_of_cannot_point_at_itself(self):
        with self.assertRaises(ValidationError):
            self.app.import_batch({"batch_id": "loop", "retry_of": "loop", "stories": [story()]},
                                  partial=True)

    def test_interrupted_attempt_leaves_no_batch_marker_and_retry_succeeds(self):
        # A conflict raised mid-transaction must roll the marker and rows back.
        self.import_story()
        changed = story("tale-b")
        conflicting = story()
        conflicting["title"] = "Conflict"
        with self.assertRaises(ConflictError):
            self.app.import_batch(batch([changed, conflicting], "crash-batch"))
        self.assertFalse(self.app.batch_status("crash-batch")["recorded"])
        retry = self.app.import_batch(batch([changed], "crash-batch"))
        self.assertEqual(retry["status"], "imported")
        self.assertEqual(retry["story_count"], 1)

    def test_interrupted_partial_attempt_can_be_retried_with_same_content(self):
        from story_patterns.catalog.service import CatalogService

        original = CatalogService._write_batch

        def explode(self, *args, **kwargs):
            raise RuntimeError("disk interrupted")

        CatalogService._write_batch = explode
        try:
            with self.assertRaises(RuntimeError):
                self.app.import_batch(batch([story()], "interrupted"), partial=True)
        finally:
            CatalogService._write_batch = original
        self.assertFalse(self.app.batch_status("interrupted")["recorded"])
        result = self.app.import_batch(batch([story()], "interrupted"), partial=True)
        self.assertEqual(result["status"], "imported")


class PreviewRetryAssessmentTests(AppCase, unittest.TestCase):
    def test_preview_reports_digest_and_assessment_only_with_a_batch_id(self):
        result = self.app.import_preview(None, [story()])
        self.assertNotIn("source_digest", result)
        self.assertNotIn("retry_assessment", result)
        with_batch = self.app.import_preview("planned", [story()])
        self.assertEqual(with_batch["retry_assessment"]["kind"], "new_batch")
        self.assertFalse(with_batch["retry_assessment"]["idempotent"])

    def test_preview_detects_identical_and_changed_resubmissions(self):
        self.app.import_batch(batch([story()], "batch-x"), partial=True)
        same = self.app.import_preview("batch-x", [story()])
        self.assertEqual(same["retry_assessment"]["kind"], "identical_retry")
        self.assertTrue(same["retry_assessment"]["idempotent"])
        changed = story()
        changed["title"] = "Different title"
        different = self.app.import_preview("batch-x", [changed])
        self.assertEqual(different["retry_assessment"]["kind"], "different_content")
        self.assertFalse(different["retry_assessment"]["idempotent"])
        self.assertIn("recorded_source_digest", different["retry_assessment"])

    def test_preview_uses_database_state_to_classify_rows(self):
        self.import_story()
        preview = self.app.import_preview("next", [story()])
        self.assertEqual(preview["status_counts"]["already_present"], 1)
        self.assertTrue(preview["ready"])
        changed = story()
        changed["title"] = "Another"
        conflicting = self.app.import_preview("next", [changed])
        self.assertEqual(conflicting["status_counts"]["conflict"], 1)
        self.assertFalse(conflicting["ready"])

    def test_preview_does_not_write_anything(self):
        self.app.import_preview("planned", [story()])
        self.assertFalse(self.app.batch_status("planned")["recorded"])
        self.assertEqual(self.app.catalog.list_stories(), [])


class BatchLedgerAuditTests(AppCase, unittest.TestCase):
    def test_audit_accepts_a_fully_reviewed_partial_batch(self):
        malformed = story("tale-b")
        malformed["events"][0]["actor_id"] = "ghost"
        self.app.import_batch(batch([story("tale-a"), malformed], "mixed"), partial=True)
        result = self.app.audit()
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["batch_links_checked"], 1)

    def test_audit_flags_a_rejected_row_linked_into_the_catalog(self):
        malformed = story("tale-b")
        malformed["events"][0]["actor_id"] = "ghost"
        self.app.import_batch(batch([malformed], "bad"), partial=True)
        # Link the invalid payload by hand: the audit must notice.
        with self.app.store.transaction() as connection:
            connection.execute(
                "INSERT INTO stories(story_id, revision, title, tradition, source_kind, document) "
                "VALUES('tale-b', 1, 'x', 'coast', 'local_synthetic', ?)",
                (json.dumps(malformed),),
            )
            connection.execute("INSERT INTO batch_stories VALUES('bad','tale-b',1)")
        result = self.app.audit()
        self.assertFalse(result["ok"])
        self.assertTrue(any("was linked" in item["problem"] or "story count" in item["problem"]
                            for item in result["issues"]))

    def test_audit_flags_a_tampered_row_payload(self):
        self.app.import_batch(batch([story()], "one"), partial=True)
        with self.app.store.transaction() as connection:
            connection.execute("UPDATE import_batch_rows SET payload='{}' WHERE batch_id='one'")
        result = self.app.audit()
        self.assertFalse(result["ok"])
        self.assertTrue(any("payload digest" in item["problem"] for item in result["issues"]))

    def test_audit_flags_a_tampered_batch_state(self):
        self.app.import_batch(batch([story()], "one"), partial=True)
        with self.app.store.transaction() as connection:
            connection.execute("UPDATE import_batches SET state='partial' WHERE batch_id='one'")
        result = self.app.audit()
        self.assertFalse(result["ok"])
        self.assertTrue(any("state differs" in item["problem"] for item in result["issues"]))

    def test_audit_flags_retry_pointer_to_a_missing_batch(self):
        self.app.import_batch(batch([story()], "fix"), partial=True)
        with self.app.store.transaction() as connection:
            connection.execute("UPDATE import_batches SET retry_of='ghost' WHERE batch_id='fix'")
        result = self.app.audit()
        self.assertFalse(result["ok"])
        self.assertTrue(any("retry_of" in item["problem"] for item in result["issues"]))

    def test_manifest_surfaces_state_and_rejected_counts(self):
        malformed = story("tale-b")
        malformed["events"][0]["actor_id"] = "ghost"
        self.app.import_batch(batch([story("tale-a"), malformed], "mixed"), partial=True)
        row = self.app.catalog_manifest()["batches"][0]
        self.assertEqual(row["state"], "partial")
        self.assertEqual(row["source_row_count"], 2)
        self.assertEqual(row["rejected_row_count"], 1)
        self.assertIsNotNone(row["source_digest"])


class ClassificationUnitTests(unittest.TestCase):
    def test_digest_covers_rejected_rows_and_file_order(self):
        rows = [story("a"), {"story_id": "b"}, story("c")]
        entries = classify_rows(rows_from_objects(rows))
        first = source_digest("b1", entries)
        reordered = classify_rows(rows_from_objects([story("c"), {"story_id": "b"}, story("a")]))
        self.assertNotEqual(source_digest("b1", reordered), first)
        self.assertEqual(source_digest("b2", entries), source_digest("b2", entries))
        self.assertNotEqual(source_digest("b2", entries), first)

    def test_existing_document_comparison_uses_canonical_json(self):
        stored = story()
        lookup = lambda story_id, revision: canonical_json(stored)  # noqa: E731
        entries = classify_rows(rows_from_objects([story()]), lookup)
        self.assertEqual(entries[0]["status"], "already_present")
        stored["title"] = "changed"
        entries = classify_rows(rows_from_objects([story()]), lookup)
        self.assertEqual(entries[0]["status"], "conflict")


if __name__ == "__main__":
    unittest.main()
