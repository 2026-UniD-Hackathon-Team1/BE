"""저장된 원본만 사용해 분류 규칙을 갱신하며 네트워크를 호출하지 않는다."""
import argparse
import base64
import json

from sqlalchemy import select

from backend.catalog.classification import CLASSIFIER_VERSION, classify_bundle
from backend.catalog.inspection import inspect_bundle
from backend.db import configured_engine, session_factory
from backend.models import AssetVersion


def reclassify_versions(sessions, *, limit=100, after=None, execute=False):
    """고정 원본과 해시는 유지하고 검증된 번들의 태그만 페이지 단위로 갱신한다."""
    if not 1 <= limit <= 500:
        raise ValueError("limit must be 1..500")
    with sessions.begin() as session:
        query = select(AssetVersion).order_by(AssetVersion.id).limit(limit + 1)
        if after:
            query = query.where(AssetVersion.id > after)
        rows = list(session.scalars(query))
        more = len(rows) > limit
        rows = rows[:limit]
        changed, skipped = 0, 0
        for version in rows:
            metadata = version.metadata_json
            if (metadata.get("demo") or metadata.get("classifier_version") == CLASSIFIER_VERSION or
                inspect_bundle(metadata, version.bundle_sha256)["integrity_status"] != "verified"):
                skipped += 1
                continue
            try:
                files = {path: base64.b64decode(content, validate=True)
                    for path, content in metadata["bundle"]["files"].items()
                    if path.lower() in {"skill.md", "readme.md", "readme"} and len(content) <= 1400000}
            except (ValueError, TypeError):
                skipped += 1
                continue
            tags = classify_bundle(files)
            changed += 1
            if execute:
                version.tags = tags
                version.metadata_json = {**metadata, "classifier_version": CLASSIFIER_VERSION,
                    "classification_status": "rule_classified" if tags["task_types"] else "insufficient_evidence"}
        return {"checked": len(rows), "updated": changed if execute else 0, "would_update": changed,
            "skipped": skipped, "next_after": rows[-1].id if more else None, "network_executed": False}


def main():
    """기본은 변경 예정 건수만 표시하고 --execute를 지정해야 DB에 반영한다."""
    parser = argparse.ArgumentParser(description="고정 원본의 오프라인 재분류")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--after")
    args = parser.parse_args()
    engine = configured_engine()
    try:
        print(json.dumps(reclassify_versions(session_factory(engine), limit=args.limit,
            after=args.after, execute=args.execute)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
