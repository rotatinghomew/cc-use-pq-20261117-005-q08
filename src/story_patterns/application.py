from story_patterns.analysis.report import ReportService
from story_patterns.catalog.service import CatalogService
from story_patterns.review.service import ReviewService
from story_patterns.storage import StoryStore


class StoryPatternApplication:
    """协调样本导入、证据审阅与可复现的本地汇总。"""

    def __init__(self, database: str):
        self.store = StoryStore(database)
        self.catalog = CatalogService(self.store)
        self.review = ReviewService(self.store)
        self.reports = ReportService(self.store)

    def import_batch(self, batch: dict, partial: bool = False) -> dict:
        return self.catalog.import_batch(batch, partial=partial)

    def import_preview(self, batch_id: str | None, rows: list, retry_of: str | None = None) -> dict:
        return self.catalog.preview(rows, batch_id=batch_id, retry_of=retry_of)

    def batch_status(self, batch_id: str) -> dict:
        return self.catalog.batch_status(batch_id)

    def recorded_batches(self) -> list[dict]:
        return self.catalog.batches()

    def record_decision(self, decision: dict) -> dict:
        return self.review.record_decision(decision)

    def create_report(self, tradition: str | None = None, minimum_reviewers: int = 2) -> dict:
        return self.reports.create(tradition, minimum_reviewers)

    def review_queue(self, minimum_reviewers: int = 2, tradition: str | None = None) -> dict:
        return self.review.queue(minimum_reviewers, tradition)

    def stored_report(self, report_id: str) -> dict | None:
        return self.reports.get(report_id)

    def preview_stories(self, rows: list[dict], batch_id: str | None = None,
                        retry_of: str | None = None) -> dict:
        return self.catalog.preview(rows, batch_id=batch_id, retry_of=retry_of)

    def revision_lineage(self) -> list[dict]:
        return self.catalog.lineage()

    def catalog_manifest(self) -> dict:
        return self.catalog.manifest()

    def record_adjudication(self, record: dict) -> dict:
        return self.review.record_adjudication(record)

    def compare_traditions(self, minimum_reviewers: int = 2) -> dict:
        return self.reports.compare(minimum_reviewers)

    def search_stories(self, **criteria) -> list[dict]:
        return self.catalog.search(**criteria)

    def review_history(self, story_id: str, revision: int) -> dict:
        return self.review.history(story_id, revision)

    def audit(self) -> dict:
        return self.catalog.audit()
