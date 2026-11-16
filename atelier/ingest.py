from __future__ import annotations

import json
from typing import Any

from .domain import CampaignSpec, DomainError, canonical, digest, parse_time, require_identifier, utc_now
from .store import Store


def normalize_sample(spec: CampaignSpec, item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise DomainError("invalid_sample", "sample must be an object")
    sample_id = require_identifier(item.get("sample_id"), "sample_id")
    group_id = require_identifier(item.get("group_id"), "group_id")
    if group_id not in spec.groups:
        raise DomainError("unknown_group", "sample group is not declared")
    shift_id = item.get("shift_id")
    if spec.shifts:
        if shift_id is None:
            raise DomainError("missing_shift", "sample needs a shift attribution")
        shift_id = require_identifier(shift_id, "shift_id")
        if shift_id not in spec.shifts:
            raise DomainError("unknown_shift", "sample shift is not declared")
    elif shift_id is not None:
        raise DomainError("unknown_shift", "campaign does not declare shifts")
    event_time = parse_time(item.get("event_time"))
    measurements = spec.validate_measurements(item.get("measurements"))
    return {"sample_id": sample_id, "group_id": group_id, "shift_id": shift_id,
            "event_time": event_time, "measurements": measurements, "spec_version": spec.version}


class Ingestor:
    def __init__(self, store: Store):
        self.store = store

    def ingest(self, campaign_id: str, batch_id: str, samples: Any, actor: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(batch_id, "batch_id")
        require_identifier(actor, "actor")
        if not isinstance(samples, list) or not 1 <= len(samples) <= 1000:
            raise DomainError("invalid_batch", "batch must contain 1-1000 samples")
        try:
            payload_sha = digest(samples)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DomainError("invalid_batch", "batch must contain JSON finite values") from exc
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM batches WHERE campaign_id=? AND batch_id=?",
                                  (campaign_id, batch_id)).fetchone()
            if existing:
                if existing["payload_sha"] != payload_sha:
                    raise DomainError("batch_conflict", "batch id already names different content")
                return {**json.loads(existing["body"]), "replayed": True}
            spec = self.store.get_spec(campaign_id, db=db)
            accepted = []
            rejected = []
            seen = set()
            now = utc_now()
            for ordinal, raw in enumerate(samples):
                try:
                    sample = normalize_sample(spec, raw)
                    sample_id = sample["sample_id"]
                    if sample_id in seen:
                        raise DomainError("duplicate_in_batch", "sample id repeated in this batch")
                    seen.add(sample_id)
                    prior = db.execute("SELECT content_sha FROM samples WHERE campaign_id=? AND sample_id=?",
                                       (campaign_id, sample_id)).fetchone()
                    if prior:
                        if prior["content_sha"] == digest(sample):
                            raise DomainError("already_ingested", "sample was already accepted")
                        raise DomainError("sample_conflict", "sample id has different accepted content")
                    db.execute("INSERT INTO samples(campaign_id,sample_id,batch_id,group_id,event_time,"
                               "spec_version,measurements,content_sha,status,created_at,updated_at,"
                               "shift_id,shift_source,shift_updated_at)"
                               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (campaign_id, sample_id, batch_id, sample["group_id"], sample["event_time"],
                                spec.version, canonical(sample["measurements"]), digest(sample),
                                "active", now, now, sample["shift_id"], "ingest", now))
                    accepted.append(sample_id)
                except DomainError as error:
                    sample_id = raw.get("sample_id") if isinstance(raw, dict) else None
                    if not isinstance(sample_id, str):
                        sample_id = None
                    try:
                        event_time = parse_time(raw.get("event_time")) if isinstance(raw, dict) else None
                    except DomainError:
                        event_time = None
                    db.execute("INSERT INTO rejections(campaign_id,batch_id,ordinal,sample_id,reason,detail,event_time)"
                               " VALUES(?,?,?,?,?,?,?)", (campaign_id, batch_id, ordinal, sample_id,
                                                           error.code, str(error), event_time))
                    rejected.append({"ordinal": ordinal, "sample_id": sample_id,
                                     "reason": error.code, "detail": str(error)})
            result = {"campaign_id": campaign_id, "batch_id": batch_id, "spec_version": spec.version,
                      "accepted": accepted, "rejected": rejected, "payload_sha": payload_sha,
                      "replayed": False}
            db.execute("INSERT INTO batches(campaign_id,batch_id,payload_sha,spec_version,accepted,rejected,"
                       "body,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (campaign_id, batch_id, payload_sha, spec.version, len(accepted), len(rejected),
                        canonical(result), now))
            if accepted:
                self.store.bump(db, campaign_id)
            self.store.audit(db, campaign_id, "batch_ingested", actor, batch_id, None, result)
            return result

    def correct(self, campaign_id: str, sample_id: str, replacement: Any,
                expected_sha: str, actor: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(sample_id, "sample_id")
        require_identifier(actor, "actor")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64 or any(ch not in "0123456789abcdef" for ch in expected_sha):
            raise DomainError("invalid_sha", "expected_sha must be a SHA-256 digest")
        with self.store.transaction() as db:
            row = self.store.one(db, "SELECT * FROM samples WHERE campaign_id=? AND sample_id=?",
                                 (campaign_id, sample_id))
            if row["content_sha"] != expected_sha:
                raise DomainError("version_conflict", "sample content changed since it was read")
            if row["status"] != "active":
                raise DomainError("inactive_sample", "only active samples may be corrected")
            spec = self.store.get_spec(campaign_id, db=db)
            if isinstance(replacement, dict) and replacement.get("shift_id") is None \
                    and row["shift_id"] is not None and spec.shifts:
                # Corrections that only touch measurements keep the shift already
                # attributed to the sample rather than forcing re-entry.
                replacement = {**replacement, "shift_id": row["shift_id"]}
            normalized = normalize_sample(spec, replacement)
            if normalized["sample_id"] != sample_id:
                raise DomainError("identity_change", "correction cannot change sample id")
            previous = {"sample_id": sample_id, "group_id": row["group_id"],
                        "shift_id": row["shift_id"],
                        "event_time": row["event_time"], "spec_version": row["spec_version"],
                        "measurements": json.loads(row["measurements"]), "content_sha": row["content_sha"]}
            next_sha = digest(normalized)
            if next_sha == row["content_sha"]:
                return {**previous, "changed": False}
            now = utc_now()
            db.execute("UPDATE samples SET group_id=?, event_time=?,spec_version=?,measurements=?,"
                       "content_sha=?,shift_id=?,shift_source=?,shift_updated_at=?,updated_at=?"
                       " WHERE campaign_id=? AND sample_id=?",
                       (normalized["group_id"], normalized["event_time"], spec.version,
                        canonical(normalized["measurements"]), next_sha, normalized["shift_id"],
                        "correction" if normalized["shift_id"] != row["shift_id"] else row["shift_source"],
                        now if normalized["shift_id"] != row["shift_id"] else row["shift_updated_at"],
                        now, campaign_id, sample_id))
            if normalized["shift_id"] != row["shift_id"]:
                self._record_shift_change(db, campaign_id, sample_id, row["shift_id"],
                                          normalized["shift_id"], actor,
                                          "shift changed by sample correction", now)
            self.store.bump(db, campaign_id)
            result = {**normalized, "content_sha": next_sha, "changed": True}
            self.store.audit(db, campaign_id, "sample_corrected", actor, sample_id, previous, result)
            return result

    def reassign_shift(self, campaign_id: str, sample_id: str, shift_id: Any,
                       expected_sha: str, reason: Any, actor: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(sample_id, "sample_id")
        require_identifier(actor, "actor")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64 or any(ch not in "0123456789abcdef" for ch in expected_sha):
            raise DomainError("invalid_sha", "expected_sha must be a SHA-256 digest")
        shift_id = require_identifier(shift_id, "shift_id")
        if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 500:
            raise DomainError("invalid_reason", "shift correction reason must have 8-500 characters")
        with self.store.transaction() as db:
            row = self.store.one(db, "SELECT * FROM samples WHERE campaign_id=? AND sample_id=?",
                                 (campaign_id, sample_id))
            if row["content_sha"] != expected_sha:
                raise DomainError("version_conflict", "sample content changed since it was read")
            if row["status"] != "active":
                raise DomainError("inactive_sample", "only active samples may change shift")
            spec = self.store.get_spec(campaign_id, db=db)
            if shift_id not in spec.shifts:
                raise DomainError("unknown_shift", "sample shift is not declared")
            before = row["shift_id"]
            if before == shift_id:
                return {"sample_id": sample_id, "shift_id": shift_id, "changed": False,
                        "content_sha": row["content_sha"],
                        "history": self._shift_history(db, campaign_id, sample_id)}
            normalized = normalize_sample(spec, {
                "sample_id": sample_id, "group_id": row["group_id"], "shift_id": shift_id,
                "event_time": row["event_time"], "measurements": json.loads(row["measurements"])})
            next_sha = digest(normalized)
            now = utc_now()
            db.execute("UPDATE samples SET shift_id=?,shift_source='correction',shift_updated_at=?,"
                       "content_sha=?,updated_at=? WHERE campaign_id=? AND sample_id=?",
                       (shift_id, now, next_sha, now, campaign_id, sample_id))
            history = self._record_shift_change(db, campaign_id, sample_id, before, shift_id,
                                                actor, reason.strip(), now)
            revision = self.store.bump(db, campaign_id)
            self.store.audit(db, campaign_id, "shift_reassigned", actor, sample_id,
                             {"shift_id": before, "content_sha": row["content_sha"]},
                             {"shift_id": shift_id, "content_sha": next_sha,
                              "revision": revision, "reason": reason.strip()})
            return {"sample_id": sample_id, "shift_id": shift_id, "before_shift": before,
                    "changed": True, "content_sha": next_sha, "revision": revision,
                    "history": history}

    @staticmethod
    def _record_shift_change(db, campaign_id: str, sample_id: str, before: str | None,
                             after: str | None, actor: str, reason: str, at: str | None = None) -> list[dict[str, Any]]:
        db.execute("INSERT INTO shift_corrections(campaign_id,sample_id,before_shift,after_shift,"
                   "actor,reason,at) VALUES(?,?,?,?,?,?,?)",
                   (campaign_id, sample_id, before, after, actor, reason, at or utc_now()))
        return Ingestor._shift_history(db, campaign_id, sample_id)

    @staticmethod
    def _shift_history(db, campaign_id: str, sample_id: str) -> list[dict[str, Any]]:
        rows = db.execute("SELECT before_shift,after_shift,actor,reason,at FROM shift_corrections"
                          " WHERE campaign_id=? AND sample_id=? ORDER BY id",
                          (campaign_id, sample_id)).fetchall()
        return [dict(row) for row in rows]

    def shift_history(self, campaign_id: str, sample_id: str) -> list[dict[str, Any]]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(sample_id, "sample_id")
        with self.store.connect() as db:
            self.store.one(db, "SELECT 1 FROM samples WHERE campaign_id=? AND sample_id=?",
                           (campaign_id, sample_id))
            return self._shift_history(db, campaign_id, sample_id)

    def void(self, campaign_id: str, sample_id: str, expected_sha: str,
             reason: str, actor: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(sample_id, "sample_id")
        require_identifier(actor, "actor")
        if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 500:
            raise DomainError("invalid_reason", "void reason must have 8-500 characters")
        with self.store.transaction() as db:
            row = self.store.one(db, "SELECT * FROM samples WHERE campaign_id=? AND sample_id=?",
                                 (campaign_id, sample_id))
            if row["content_sha"] != expected_sha:
                raise DomainError("version_conflict", "sample content changed since it was read")
            if row["status"] != "active":
                raise DomainError("inactive_sample", "sample is already void")
            db.execute("UPDATE samples SET status='void',updated_at=? WHERE campaign_id=? AND sample_id=?",
                       (utc_now(), campaign_id, sample_id))
            self.store.bump(db, campaign_id)
            result = {"sample_id": sample_id, "status": "void", "reason": reason.strip()}
            self.store.audit(db, campaign_id, "sample_voided", actor, sample_id,
                             {"status": row["status"], "content_sha": row["content_sha"]}, result)
            return result

    def list_rejections(self, campaign_id: str, batch_id: str) -> list[dict[str, Any]]:
        with self.store.connect() as db:
            self.store.one(db, "SELECT 1 FROM batches WHERE campaign_id=? AND batch_id=?",
                           (campaign_id, batch_id))
            rows = db.execute("SELECT ordinal,sample_id,reason,detail,event_time FROM rejections"
                              " WHERE campaign_id=? AND batch_id=? ORDER BY ordinal",
                              (campaign_id, batch_id)).fetchall()
            return [dict(row) for row in rows]
