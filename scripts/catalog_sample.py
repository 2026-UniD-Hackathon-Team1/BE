"""공개 원본 표본을 로컬에서 보존해 추가 수집 없이 추천을 반복 평가한다."""
import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url

from backend.catalog.classification import CLASSIFIER_VERSION
from backend.catalog.inspection import inspect_bundle
from backend.catalog.reclassify import reclassify_versions
from backend.models import Asset, AssetVersion, ChangeEvent, Source, utc_now


def _local_simulation(session):
    """운영 DB와 일반 로컬 DB에 표본을 써 넣지 못하게 시연 스키마만 허용한다."""
    url = make_url(session.get_bind().url)
    if (url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}
            or url.host not in {"localhost", "127.0.0.1", "::1"} or url.database != "be_simulation"
            or not re.fullmatch(r"-csearch_path=simulation_[0-9a-f]{10,32}", url.query.get("options", ""))):
        raise ValueError("isolated local simulation schema required")


def export_sample(sessions, path):
    """계정과 토큰 및 개인 환경 없이 실제 수집한 공개 번들만 파일로 보존한다."""
    with sessions() as session:
        _local_simulation(session)
        rows = session.execute(select(Asset, AssetVersion, ChangeEvent, Source)
            .join(AssetVersion, AssetVersion.asset_id == Asset.id)
            .join(ChangeEvent, ChangeEvent.to_version_id == AssetVersion.id)
            .join(Source, Source.id == Asset.source_id).where(Source.enabled.is_(True))
            .order_by(Asset.id, AssetVersion.id)).all()
        sources, assets, versions, changes = {}, {}, {}, {}
        for asset, version, change, source in rows:
            if version.metadata_json.get("simulation_fixture") or version.metadata_json.get("demo"):
                continue
            if inspect_bundle(version.metadata_json, version.bundle_sha256)["integrity_status"] != "verified":
                raise ValueError("unverified sample bundle")
            sources[source.id] = {"id": source.id, "kind": source.kind, "url": source.url,
                "enabled": True, "last_success_at": source.last_success_at, "last_error": source.last_error,
                "sync_state": {"current_versions": source.sync_state.get("current_versions", {})}}
            assets[asset.id] = {key: getattr(asset, key) for key in ("id", "kind", "canonical_key", "display_name", "source_id")}
            versions[version.id] = {key: getattr(version, key) for key in (
                "id", "asset_id", "revision", "bundle_sha256", "metadata_json", "tags", "verified_source_at")}
            changes[change.id] = {key: getattr(change, key) for key in (
                "id", "asset_id", "from_version_id", "to_version_id", "change_type", "published_at", "first_seen_at", "source_url")}
    result = {"format_version": 1, "exported_at": utc_now(), "scope": "previously_collected_public_bundles",
        "sources": list(sources.values()), "assets": list(assets.values()), "versions": list(versions.values()),
        "changes": list(changes.values())}
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(output), "sources": len(sources), "versions": len(versions), "network_executed": False}


def load_sample(sessions, path):
    """해시를 확인한 공개 표본을 격리 DB에 재현하고 최신 규칙으로 오프라인 재분류한다."""
    source = Path(path)
    if source.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("sample file too large")
    data = json.loads(source.read_text(encoding="utf-8"))
    if (data.get("format_version") != 1 or any(not isinstance(data.get(key), list) or len(data[key]) > 500
            for key in ("sources", "assets", "versions", "changes"))):
        raise ValueError("invalid sample")
    for version in data["versions"]:
        if inspect_bundle(version["metadata_json"], version["bundle_sha256"])["integrity_status"] != "verified":
            raise ValueError("sample bundle integrity mismatch")
    with sessions.begin() as session:
        _local_simulation(session)
        for model, key in ((Source, "sources"), (Asset, "assets"), (AssetVersion, "versions"), (ChangeEvent, "changes")):
            for row in data[key]:
                session.merge(model(**row))
            session.flush()
    # 페이지를 모두 처리해 표본 크기와 상관없이 같은 규칙을 적용한다.
    after = None
    while True:
        result = reclassify_versions(sessions, limit=100, after=after, execute=True)
        after = result["next_after"]
        if after is None:
            break
    return {"sources": len(data["sources"]), "versions": len(data["versions"]),
        "exported_at": data.get("exported_at"), "classifier_version": CLASSIFIER_VERSION,
        "scope": "cached_public_sample", "network_executed": False}
