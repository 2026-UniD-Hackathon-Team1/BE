"""수집 응답을 검증하고 트랜잭션 단위로 공통 카탈로그에 반영한다."""
import base64
import hashlib
import json
from urllib.parse import quote
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from backend.catalog.config import CollectionConfig, RepositoryConfig, is_fixture_folder
from backend.catalog.classification import CLASSIFIER_VERSION, classify_bundle
from backend.catalog.policy import bind_policy
from backend.catalog.github_client import CollectionError, GithubClient, RepositorySnapshot
from backend.models import Asset, AssetVersion, ChangeEvent, Source, utc_now

STOP_ERRORS = {"rate_limited", "authentication_failed", "forbidden",
               "request_budget_exceeded", "collection_deadline_exceeded", "collection_cooldown"}


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


def completed_indices(state):
    """기존 연속 커서를 읽고 새 완료 목록은 범위와 연속 접두 위치가 일치하는지 확인한다."""
    total = len(state["folders"]) + 1
    cursor = state.get("next")
    if not isinstance(cursor, int) or isinstance(cursor, bool) or not 0 <= cursor <= total:
        raise CollectionError("invalid_collection_state")
    values = state.get("completed_indices", list(range(cursor)))
    if (not isinstance(values, list) or len(values) > total or any(not isinstance(index, int)
        or isinstance(index, bool) or not 0 <= index < total for index in values)):
        raise CollectionError("invalid_collection_state")
    completed = set(values)
    prefix = next((index for index in range(total) if index not in completed), total)
    if len(completed) != len(values) or cursor != prefix or (total - 1 in completed and len(completed) != total):
        raise CollectionError("invalid_collection_state")
    return completed


class CatalogCollector:
    def __init__(self, sessions, client: GithubClient):
        """DB 세션과 수집 클라이언트를 주입받아 실행 환경 의존성을 분리한다."""
        self.sessions = sessions
        self.client = client
        if client.before_request is None:
            bind_policy(sessions, client)

    def collect(self, config: RepositoryConfig, *, batch_size=5):
        """한 회차의 고정 원본을 폴더 단위로 저장하고 다음 위치를 남긴다."""
        source_id = "github:" + config.repository
        with self.sessions.begin() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:source, 0))"), {"source": source_id})
            source = session.get(Source, source_id)
            if source is None:
                source = Source(id=source_id, kind="github_repo", url="https://github.com/" + config.repository)
                session.add(source)
                session.flush()
            elif not source.enabled:
                return {"repository": config.repository, "status": "disabled", "new_versions": 0}
            previous = source.sync_state.get("batch")
        count = 0
        advanced = 0
        state = None
        deferred = []
        try:
            pending = previous and previous["next"] < len(previous["folders"]) + 1
            plan = self.client.prepare(config, previous if pending else None)
            planned_state = {**plan.state, "generation": plan.state.get("generation", uuid4().hex)}
            completed = completed_indices(planned_state)
            state = {**planned_state, "completed_indices": sorted(completed)}
            with self.sessions.begin() as session:
                source = session.get(Source, source_id, with_for_update=True)
                if source.sync_state.get("batch") != previous:
                    raise CollectionError("collection_batch_conflict")
                if not source.enabled:
                    raise CollectionError("source_disabled")
                source.sync_state = {**source.sync_state, "batch": state}
            total = len(state["folders"]) + 1
            inspected = 0
            for index in range(total):
                if index in completed:
                    continue
                if advanced >= batch_size or inspected >= min(2000, max(20, batch_size * 4)):
                    break
                # 저장소 README는 모든 스킬이 저장된 뒤에만 회차 완료 이벤트로 기록한다.
                if index == total - 1 and len(completed) < total - 1:
                    break
                inspected += 1
                self.client.check_budget()
                required = len(self.client.unit_entries(plan, index))
                reason = ("bundle_request_budget_exceeded" if required > self.client.max_requests - 2
                    else "request_budget_insufficient" if required > self.client.max_requests - self.client.request_count else None)
                if reason:
                    if len(deferred) < 20:
                        deferred.append({"folder": state["folders"][index] if index < total - 1 else "README.md",
                            "required_requests": required, "reason": reason})
                    continue
                bundle = self.client.read_unit(plan, index)
                next_completed = completed | {index}
                cursor = next((item for item in range(total) if item not in next_completed), total)
                next_state = {**state, "next": cursor, "completed_indices": sorted(next_completed)}
                snapshot = RepositorySnapshot(config.repository, state["commit_sha"], state["metadata"], [bundle])
                count += self._save(source_id, snapshot, state, next_state)
                advanced += 1
                state = next_state
                completed = next_completed
            result = self._batch_result(config.repository, state, count)
            if deferred:
                result.update(pause_reason=deferred[0]["reason"], deferred_units=deferred)
            return result
        except (CollectionError, SQLAlchemyError) as error:
            code = error.code if isinstance(error, CollectionError) else "storage_error"
            with self.sessions.begin() as session:
                source = session.get(Source, source_id)
                source.last_error = code
            result = self._batch_result(config.repository, state, count) if state else {
                "repository": config.repository, "status": "failed", "new_versions": count}
            result.update(status="partial" if advanced else "failed", error=code)
            if deferred:
                result.update(pause_reason=deferred[0]["reason"], deferred_units=deferred)
            if isinstance(error, CollectionError):
                result.update(retry_after=error.retry_after, reset_at=error.reset_at)
            return result

    def _batch_result(self, repository, state, count):
        """저장한 원본 수와 전체 진행 및 재개 필요 여부를 명시한다."""
        total = len(state["folders"]) + 1
        complete = state["next"] == total
        return {"repository": repository, "status": "done" if complete else "partial", "new_versions": count,
            "commit_sha": state["commit_sha"], "completed_units": len(completed_indices(state)), "total_units": total,
            "resume_required": not complete}

    def _save(self, source_id, snapshot, expected_state, next_state):
        """한 번들의 원본 저장과 진행 커서 전진을 같은 트랜잭션으로 처리한다."""
        count = 0
        with self.sessions.begin() as session:
            source = session.get(Source, source_id, with_for_update=True)
            if source.sync_state.get("batch") != expected_state:
                raise CollectionError("collection_batch_conflict")
            if not source.enabled:
                raise CollectionError("source_disabled")
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
                        "metadata_observed_at": next_state.get("metadata_observed_at"),
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
                version.metadata_json = {**version.metadata_json, "source_folder": bundle.folder,
                    "collection_role": "repository_fixture" if is_fixture_folder(bundle.folder) else "candidate_asset"}
                if (version.metadata_json.get("classification_status") == "pending"
                    or version.metadata_json.get("classifier_version") != CLASSIFIER_VERSION):
                    version.tags = classify_bundle(bundle.files)
                    version.metadata_json = {**version.metadata_json,
                        "classification_status": "rule_classified" if version.tags["task_types"] else "insufficient_evidence",
                        "classifier_version": CLASSIFIER_VERSION}
                current[asset_id] = version_id
            source.sync_state = {**source.sync_state, "batch": next_state, "current_versions": current}
            if next_state["next"] == len(next_state["folders"]) + 1:
                source.sync_state = {**source.sync_state, "commit_sha": snapshot.commit_sha}
                source.sync_state.pop("queued_config", None)
                source.last_success_at = utc_now()
            source.last_error = None
        return count

    def run(self, config: CollectionConfig, *, progress=None):
        """검색으로 발견한 후보와 기존 추적 저장소를 중복 없이 순차 수집한다."""
        repos = {item.repository: item for item in config.repositories}
        if config.resume_pending:
            with self.sessions() as session:
                for source in session.scalars(select(Source).where(Source.enabled.is_(True),
                        Source.kind == "github_repo").order_by(Source.id)):
                    state = source.sync_state.get("batch")
                    queued = source.sync_state.get("queued_config")
                    if len(repos) >= 20:
                        break
                    if state and state["next"] < len(state["folders"]) + 1:
                        item = RepositoryConfig(repository=source.id.removeprefix("github:"),
                            skill_folders=state["scope"], folder_hints=state.get("folder_hints", []))
                        repos.setdefault(item.repository, item)
                    elif queued:
                        item = RepositoryConfig.model_validate(queued)
                        repos.setdefault(item.repository, item)
        discovery_errors = []
        discovery = [{"query": query, "status": "not_attempted", "repositories": [], "error": None}
                     for query in config.discovery_queries]
        discovery_stopped = False
        def report(items):
            """외부 요청을 추가하지 않고 현재 저장 결과를 진행 이력에 전달한다."""
            if progress:
                progress({"items": list(items), "discovery_errors": list(discovery_errors),
                    "discovery": [{**item, "repositories": list(item["repositories"])} for item in discovery]})
        report([])
        for index, query in enumerate(config.discovery_queries):
            try:
                discovered = self.client.discover(query, config.discovery_limit,
                    sort=config.discovery_sort.get(query, "updated"), hints=config.discovery_hints)
                discovery[index] = {"query": query, "status": "done",
                    "repositories": [item.repository for item in discovered], "error": None}
                for item in discovered:
                    item = item.model_copy(update={"folder_hints": config.discovery_hints})
                    if item.repository in repos or len(repos) < 20:
                        repos.setdefault(item.repository, item)
            except CollectionError as error:
                discovery[index] = {"query": query, "status": "failed", "repositories": [], "error": error.code}
                discovery_errors.append({"query": query, "error": error.code,
                                         "retry_after": error.retry_after, "reset_at": error.reset_at})
                if error.code in STOP_ERRORS:
                    discovery_stopped = True
            report([])
            if discovery_stopped:
                break
        # 검색 결과가 다음 검색에서 사라져도 아직 시작하지 않은 후보를 이어받는다.
        for item in repos.values():
            source_id = "github:" + item.repository
            with self.sessions.begin() as session:
                session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:source, 0))"), {"source": source_id})
                source = session.get(Source, source_id, with_for_update=True)
                if source is None:
                    source = Source(id=source_id, kind="github_repo", url="https://github.com/" + item.repository,
                        sync_state={"queued_config": item.model_dump()})
                    session.add(source)
                elif source.enabled and not source.sync_state.get("queued_config"):
                    source.sync_state = {**source.sync_state, "queued_config": item.model_dump()}
        if discovery_stopped:
            return {"items": [], "discovery_errors": discovery_errors, "discovery": discovery}
        results = []
        for item in repos.values():
            result = self.collect(item, batch_size=config.batch_size)
            results.append(result)
            report(results)
            if result.get("error") in STOP_ERRORS:
                break
            if (result.get("pause_reason") in {"request_budget_insufficient", "bundle_request_budget_exceeded"}
                    and self.client.max_requests - self.client.request_count < 2):
                break
        return {"items": results, "discovery_errors": discovery_errors, "discovery": discovery}
