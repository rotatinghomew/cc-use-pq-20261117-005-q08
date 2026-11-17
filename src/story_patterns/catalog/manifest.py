from __future__ import annotations


def build_manifest(batches: list[dict], stories: list[dict]) -> dict:
    by_tradition = {}
    for story in stories:
        by_tradition[story["tradition"]] = by_tradition.get(story["tradition"], 0) + 1
    normalized_batches = []
    for batch in batches:
        item = dict(batch)
        item.setdefault("state", "legacy")
        item.setdefault("source_digest", None)
        item.setdefault("source_row_count", None)
        item.setdefault("retry_of", None)
        item.setdefault("recorded_at", None)
        item["rejected_row_count"] = (
            None if item["source_row_count"] is None else item["source_row_count"] - item["story_count"]
        )
        normalized_batches.append(item)
    return {
        "batch_count": len(batches),
        "story_revision_count": len(stories),
        "tradition_counts": dict(sorted(by_tradition.items())),
        "batches": sorted(normalized_batches, key=lambda item: item["batch_id"]),
        "story_revisions": [
            {"story_id": item["story_id"], "revision": item["revision"],
             "source_reference": item["source"]["reference"], "source_kind": item["source"]["kind"]}
            for item in sorted(stories, key=lambda value: (value["story_id"], value["revision"]))
        ],
    }
