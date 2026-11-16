from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
import json
from typing import Any

from .composition import CompositionService
from .domain import DomainError, parse_time, require_identifier
from .quality import metric_health, validate_window
from .store import Store


class Inspector:
    def __init__(self, store: Store):
        self.store = store

    def batch_report(self, campaign_id: str, batch_id: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(batch_id, "batch_id")
        with self.store.connect() as db:
            batch = self.store.one(db, "SELECT * FROM batches WHERE campaign_id=? AND batch_id=?",
                                   (campaign_id, batch_id))
            accepted = db.execute("SELECT sample_id,group_id,shift_id,shift_source,event_time,"
                                  "status,content_sha FROM samples WHERE campaign_id=? AND batch_id=?"
                                  " ORDER BY sample_id",
                                  (campaign_id, batch_id)).fetchall()
            rejected = db.execute("SELECT ordinal,sample_id,reason,detail,event_time"
                                  " FROM rejections WHERE campaign_id=? AND batch_id=? ORDER BY ordinal",
                                  (campaign_id, batch_id)).fetchall()
            summary = json.loads(batch["body"])
            return {"campaign_id": campaign_id, "batch_id": batch_id,
                    "spec_version": batch["spec_version"], "payload_sha": batch["payload_sha"],
                    "recorded_accepted": batch["accepted"], "recorded_rejected": batch["rejected"],
                    "accepted": [dict(row) for row in accepted],
                    "rejected": [dict(row) for row in rejected],
                    "summary": summary, "created_at": batch["created_at"]}

    def campaign_overview(self, campaign_id: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        with self.store.connect() as db:
            campaign = self.store.get_campaign(campaign_id, db)
            batches = db.execute("SELECT count(*) AS count,coalesce(sum(accepted),0) AS accepted,"
                                 "coalesce(sum(rejected),0) AS rejected FROM batches WHERE campaign_id=?",
                                 (campaign_id,)).fetchone()
            sample_rows = db.execute("SELECT status,group_id,count(*) AS count FROM samples"
                                     " WHERE campaign_id=? GROUP BY status,group_id", (campaign_id,)).fetchall()
            statuses: dict[str, dict[str, int]] = defaultdict(dict)
            for row in sample_rows:
                statuses[row["status"]][row["group_id"]] = row["count"]
            analyses = db.execute("SELECT count(*) AS count,coalesce(max(source_revision),0) AS newest"
                                  " FROM analyses WHERE campaign_id=?", (campaign_id,)).fetchone()
            releases = db.execute("SELECT count(*) FROM releases WHERE campaign_id=?", (campaign_id,)).fetchone()[0]
            last_audit = db.execute("SELECT max(id) FROM audits WHERE campaign_id=?", (campaign_id,)).fetchone()[0]
            return {"campaign_id": campaign_id, "spec_version": campaign["spec"]["version"],
                    "revision": campaign["revision"], "batches": dict(batches),
                    "samples": dict(statuses), "analyses": dict(analyses),
                    "releases": releases, "last_audit_id": last_audit}

    def composition_coverage(self, campaign_id: str, composition_id: str,
                             version: int | None = None) -> dict[str, Any]:
        """Sample counts per group and shift against the target population."""
        require_identifier(campaign_id, "campaign_id")
        require_identifier(composition_id, "composition_id")
        with self.store.connect() as db:
            spec = self.store.get_spec(campaign_id, db=db)
            composition, content_sha = CompositionService(self.store).load(
                campaign_id, composition_id, version, db)
            rows = db.execute("SELECT group_id,shift_id,count(*) AS count FROM samples"
                              " WHERE campaign_id=? AND status='active' GROUP BY group_id,shift_id",
                              (campaign_id,)).fetchall()
            counts: dict[str, dict[str, int]] = {}
            for row in rows:
                shift = "unassigned" if row["shift_id"] is None else row["shift_id"]
                counts.setdefault(shift, {})[row["group_id"]] = row["count"]
            weighted = {shift for shift, _ in composition.strata}
            minimum = composition.min_samples if composition.min_samples is not None else spec.min_samples
            gaps = [{"stratum": shift, "group": group,
                     "samples": counts.get(shift, {}).get(group, 0), "required": minimum}
                    for shift, _ in composition.strata for group in spec.groups
                    if counts.get(shift, {}).get(group, 0) < minimum]
            unweighted = {shift: counts[shift] for shift in counts if shift not in weighted}
            return {"campaign_id": campaign_id, "composition_id": composition_id,
                    "version": composition.version, "content_sha": content_sha,
                    "population": composition.population,
                    "weights": {shift: str(weight) for shift, weight in composition.strata},
                    "min_samples_per_layer": minimum,
                    "counts": counts, "insufficient_layers": gaps,
                    "unweighted_strata": unweighted}

    def coverage(self, campaign_id: str, start: str, end: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        with self.store.connect() as db:
            spec = self.store.get_spec(campaign_id, db=db)
            left, right = validate_window(spec, start, end)
            samples = self.store.list_samples(campaign_id, status="active", start=left, end=right, db=db)
            by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for sample in samples:
                by_group[sample["group_id"]].append(sample)
            metrics = {group: metric_health(spec, by_group[group]) for group in spec.groups}
            shifts: dict[str, dict[str, int]] = defaultdict(dict)
            for sample in samples:
                shift = "unassigned" if sample.get("shift_id") is None else sample["shift_id"]
                shifts[shift][sample["group_id"]] = shifts[shift].get(sample["group_id"], 0) + 1
            older_versions = Counter(sample["spec_version"] for sample in samples)
            return {"campaign_id": campaign_id, "window": {"start": left, "end": right},
                    "groups": {group: {"samples": len(by_group[group]), "metrics": metrics[group]}
                               for group in spec.groups},
                    "shifts": dict(sorted(shifts.items())),
                    "spec_versions": dict(sorted(older_versions.items())),
                    "active_total": len(samples)}

    def compare_analyses(self, campaign_id: str, first_id: str, second_id: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(first_id, "first_id")
        require_identifier(second_id, "second_id")
        with self.store.connect() as db:
            first = self.store.get_analysis(campaign_id, first_id, db)
            second = self.store.get_analysis(campaign_id, second_id, db)
            a, b = first["body"], second["body"]
            if a["request"]["start"] != b["request"]["start"] or a["request"]["end"] != b["request"]["end"]:
                raise DomainError("incomparable_analyses", "analyses must share the same window")
            if a["request"]["spec_version"] != b["request"]["spec_version"]:
                raise DomainError("incomparable_analyses", "analyses must share the same specification version")
            if (a["request"].get("composition_id"), a["request"].get("composition_version")) != (
                    b["request"].get("composition_id"), b["request"].get("composition_version")):
                raise DomainError("incomparable_analyses",
                                  "analyses must share the same target composition version")
            changes = {}
            for group in a["groups"]:
                metrics = {}
                for name, before in a["groups"][group].items():
                    after = b["groups"].get(group, {}).get(name)
                    if before == after:
                        continue
                    if before is None or after is None:
                        delta = None
                    else:
                        delta = str(Decimal(after["mean"]) - Decimal(before["mean"]))
                    metrics[name] = {"before": before, "after": after, "mean_delta": delta}
                if metrics:
                    changes[group] = metrics
            return {"campaign_id": campaign_id, "first_id": first_id, "second_id": second_id,
                    "revision_before": a["source_revision"], "revision_after": b["source_revision"],
                    "quality_before": a["quality"], "quality_after": b["quality"],
                    "metrics_changed": changes}

    def rejection_trends(self, campaign_id: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        with self.store.connect() as db:
            self.store.get_campaign(campaign_id, db)
            rows = db.execute("SELECT b.batch_id,b.created_at,b.accepted,b.rejected,r.reason,"
                              "count(r.ordinal) AS reason_count FROM batches b LEFT JOIN rejections r"
                              " ON b.campaign_id=r.campaign_id AND b.batch_id=r.batch_id"
                              " WHERE b.campaign_id=? GROUP BY b.batch_id,r.reason ORDER BY b.created_at,b.batch_id",
                              (campaign_id,)).fetchall()
            batches: dict[str, dict[str, Any]] = {}
            for row in rows:
                entry = batches.setdefault(row["batch_id"], {"batch_id": row["batch_id"],
                    "created_at": row["created_at"], "accepted": row["accepted"],
                    "rejected": row["rejected"], "reasons": {}})
                if row["reason"] is not None:
                    entry["reasons"][row["reason"]] = row["reason_count"]
            return {"campaign_id": campaign_id, "batches": list(batches.values())}

    def release_context(self, campaign_id: str, release_id: str) -> dict[str, Any]:
        release = self.store.get_release(campaign_id, release_id)
        with self.store.connect() as db:
            reviews = db.execute("SELECT reviewer,decision,note,created_at FROM reviews"
                                 " WHERE campaign_id=? AND analysis_id=? ORDER BY reviewer",
                                 (campaign_id, release["analysis_id"])).fetchall()
            audit = db.execute("SELECT id,actor,at FROM audits WHERE campaign_id=?"
                               " AND kind='analysis_published' AND subject=? ORDER BY id",
                               (campaign_id, release_id)).fetchone()
            return {"release_id": release_id, "release_sha": release["body"]["release_sha"],
                    "source_revision": release["source_revision"],
                    "analysis_result_sha": release["result_sha"],
                    "reviews": [dict(row) for row in reviews],
                    "audit": dict(audit) if audit else None}
