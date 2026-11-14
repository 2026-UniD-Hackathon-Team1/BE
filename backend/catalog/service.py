"""수집 응답을 검증하고 트랜잭션 단위로 공통 카탈로그에 반영한다."""
import base64
import hashlib
import json
from urllib.parse import quote
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.catalog.config import CollectionConfig, RepositoryConfig
from backend.catalog.classification import CLASSIFIER_VERSION, classify_bundle
from backend.catalog.github_client import CollectionError, GithubClient
from backend.models import Asset, AssetVersion, ChangeEvent, Source, utc_now


def stable_id(prefix: str, value: str) -> str:
    """동일 원본 키에 대해 재수집해도 변하지 않는 식별자를 만든다."""
    return prefix + "_" + hashlib.sha256(value.encode()).hexdigest()[:32]


def bundle_payload(bundle, metadata):
    """상대 경로와 파일 내용을 정렬해 재현 가능한 번들 JSON을 만든다."""
    payload = {"files": {path: base64.b64encode(content).decode("ascii")
                         for path, content in sorted(bundle.files.items())}}
    if bundle.kind == "repo":
        payload["repository"] = metadata
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    return payload, hashlib.sha256(encoded).hexdigest()


class CatalogCollector:
    def __init__(self, sessions, client: GithubClient):
        """DB 세션과 수집 클라이언트를 주입받아 실행 환경 의존성을 분리한다."""
        self.sessions = sessions
        self.client = client

    def collect(self, config: RepositoryConfig):
        """전체 응답 확인 후 한 저장소를 원자적으로 저장하고 실패 상태를 기록한다."""
        source_id = "github:" + config.repository
        with self.sessions.begin() as session:
            source = session.get(Source, source_id)
            if source is None:
                session.add(Source(id=source_id, kind="github_repo", url="https://github.com/" + config.repository))
            elif not source.enabled:
                return {"repository": config.repository, "status": "disabled", "new_versions": 0}
        try:
            snapshot = self.client.snapshot(config)
            return self._save(source_id, snapshot)
        except (CollectionError, SQLAlchemyError) as error:
            code = error.code if isinstance(error, CollectionError) else "storage_error"
            with self.sessions.begin() as session:
                source = session.get(Source, source_id)
                source.last_error = code
            result = {"repository": config.repository, "status": "failed", "error": code, "new_versions": 0}
            if isinstance(error, CollectionError):
                result.update(retry_after=error.retry_after, reset_at=error.reset_at)
            return result

    def _save(self, source_id, snapshot):
        """새 콘텐츠만 저장하고 전체 성공 시 현재 버전과 마지막 성공 시각을 갱신한다."""
        count = 0
        with self.sessions.begin() as session:
            source = session.get(Source, source_id)
            current = dict(source.sync_state.get("current_versions", {}))
            for bundle in snapshot.bundles:
                key = f"github:{snapshot.repository}"
                if bundle.kind == "skill":
                    key += ":" + bundle.folder
                asset_id = stable_id("asset", key)
                asset = session.get(Asset, asset_id)
                if asset is None:
                    asset = Asset(id=asset_id, kind=bundle.kind, canonical_key=key,
                                  display_name=snapshot.repository + (" / " + bundle.folder if bundle.kind == "skill" else ""),
                                  source_id=source_id)
                    session.add(asset)
                    session.flush()
                payload, digest = bundle_payload(bundle, snapshot.metadata)
                version_id = stable_id("ver", asset_id + ":" + digest)
                version = session.get(AssetVersion, version_id)
                if version is None:
                    version = AssetVersion(id=version_id, asset_id=asset_id, revision=snapshot.commit_sha,
                        bundle_sha256=digest, metadata_json={"bundle": payload, "repository": snapshot.metadata,
                        "source_verified": True, "classification_status": "pending", "support_level": "information_only",
                        "source_url": f"https://github.com/{snapshot.repository}/tree/{snapshot.commit_sha}/" + quote(bundle.folder, safe="/")},
                        tags={"task_types": [], "capabilities": [], "languages": ["unknown"], "supported_tools": [], "evidence": []})
                    session.add(version)
                    session.flush()
                    previous = current.get(asset_id)
                    session.add(ChangeEvent(id="change_" + uuid4().hex, asset_id=asset_id,
                        from_version_id=previous, to_version_id=version_id,
                        change_type="updated" if previous else "new",
                        source_url=version.metadata_json["source_url"]))
                    count += 1
                if version.metadata_json.get("classification_status") == "pending":
                    version.tags = classify_bundle(bundle.files)
                    version.metadata_json = {**version.metadata_json,
                        "classification_status": "rule_classified" if version.tags["task_types"] else "pending",
                        "classifier_version": CLASSIFIER_VERSION}
                current[asset_id] = version_id
            source.sync_state = {"commit_sha": snapshot.commit_sha, "current_versions": current}
            source.last_success_at = utc_now()
            source.last_error = None
        return {"repository": snapshot.repository, "status": "done", "new_versions": count}

    def run(self, config: CollectionConfig):
        """검색으로 발견한 후보와 기존 추적 저장소를 중복 없이 순차 수집한다."""
        repos = {item.repository: item for item in config.repositories}
        discovery_errors = []
        for query in config.discovery_queries:
            try:
                for item in self.client.discover(query, config.discovery_limit):
                    if item.repository in repos or len(repos) < 20:
                        repos.setdefault(item.repository, item)
            except CollectionError as error:
                discovery_errors.append({"query": query, "error": error.code,
                                         "retry_after": error.retry_after, "reset_at": error.reset_at})
                if error.code in {"rate_limited", "authentication_failed", "forbidden"}:
                    return {"items": [], "discovery_errors": discovery_errors}
        results = []
        for item in repos.values():
            result = self.collect(item)
            results.append(result)
            if result.get("error") in {"rate_limited", "authentication_failed", "forbidden"}:
                break
        return {"items": results, "discovery_errors": discovery_errors}
