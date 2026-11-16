from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from atelier.analysis import Analyzer
from atelier.api import create_app
from atelier.composition import CompositionService
from atelier.domain import CampaignSpec, DomainError
from atelier.evidence import EvidenceService
from atelier.ingest import Ingestor
from atelier.inspection import Inspector
from atelier.reconcile import Reconciler
from atelier.release import Publisher
from atelier.store import Store


def campaign():
    return {
        "campaign_id": "shift_trial",
        "title": "Shift composition trial",
        "groups": ["control", "process_b"],
        "baseline": "control",
        "metrics": [{"name": "yield_pct", "unit": "percent", "minimum": "0",
                     "maximum": "100", "precision": 2}],
        "min_samples": 1,
        "max_bad_fraction": "0.2",
        "window_hours": 24,
        "shifts": ["morning", "night"],
    }


def samples():
    rows = []
    # Morning: both groups near 70.  Night: control is much weaker, so a pooled
    # comparison exaggerates the process effect while the layer effect is small.
    layout = [
        ("morning", "control", ["70.00", "71.00"]),
        ("morning", "process_b", ["72.00", "73.00"]),
        ("night", "control", ["60.00", "62.00"]),
        ("night", "process_b", ["64.00", "66.00"]),
    ]
    index = 0
    for shift, group, values in layout:
        for value in values:
            index += 1
            rows.append({"sample_id": f"sample_{index:02d}", "group_id": group,
                         "shift_id": shift, "event_time": f"2026-02-01T0{index}:00:00Z",
                         "measurements": {"yield_pct": value}})
    return rows


COMPOSITION = {
    "composition_id": "target_mix",
    "title": "Target production mix",
    "population": "night-heavy export orders",
    "strata": {"morning": "0.25", "night": "0.75"},
}


class StratifiedTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name) / "ledger.sqlite3")
        self.spec = CampaignSpec.from_dict(campaign())
        self.store.create_campaign(self.spec, "operator")
        self.ingest = Ingestor(self.store)
        self.analyzer = Analyzer(self.store)
        self.compositions = CompositionService(self.store)
        self.publisher = Publisher(self.store)
        self.campaign_id = self.spec.campaign_id
        self.window = {"start": "2026-02-01T00:00:00Z", "end": "2026-02-02T00:00:00Z"}

    def add(self, rows=None):
        return self.ingest.ingest(self.campaign_id, "batch_one", rows or samples(), "operator")

    def compose(self, body=None):
        return self.compositions.create(self.campaign_id, "target_mix",
                                        {**COMPOSITION, **(body or {})}, "operator")

    def analyze(self, analysis_id="analysis_one", composition_id="target_mix",
                version=None):
        return self.analyzer.build(self.campaign_id, analysis_id, self.window["start"],
                                   self.window["end"], "analyst", composition_id, version)

    def approve(self, analysis, composition_version=1, population=None):
        population = population or analysis["target_composition"]["population"]
        for reviewer in ("reviewer_a", "reviewer_b"):
            self.publisher.review(self.campaign_id, analysis["analysis_id"], reviewer, "approve",
                                  "Target mix and layers were checked", analysis["result_sha"],
                                  population, "target_mix", composition_version)

    # --- attribution -------------------------------------------------------
    def test_shift_is_required_when_declared(self):
        rows = samples()
        del rows[0]["shift_id"]
        result = self.add(rows)
        self.assertEqual(result["rejected"][0]["reason"], "missing_shift")
        self.assertEqual(len(result["accepted"]), 7)

    def test_undeclared_shift_is_rejected(self):
        rows = samples()
        rows[0]["shift_id"] = "weekend"
        result = self.add(rows)
        self.assertEqual(result["rejected"][0]["reason"], "unknown_shift")

    def test_shift_correction_revises_revision_and_history(self):
        self.add()
        sample = self.store.list_samples(self.campaign_id)[0]
        revision = self.store.get_campaign(self.campaign_id)["revision"]
        changed = self.ingest.reassign_shift(self.campaign_id, sample["sample_id"], "night",
                                             sample["content_sha"],
                                             "Shift roster shows night coverage", "operator")
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["before_shift"], "morning")
        self.assertGreater(changed["revision"], revision)
        history = self.ingest.shift_history(self.campaign_id, sample["sample_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["after_shift"], "night")
        self.assertEqual(self.store.list_samples(self.campaign_id)[0]["shift_id"], "night")

    def test_shift_correction_invalidates_analysis_and_release_eligibility(self):
        self.add()
        self.compose()
        analysis = self.analyze()
        self.approve(analysis)
        sample = self.store.list_samples(self.campaign_id)[0]
        self.ingest.reassign_shift(self.campaign_id, sample["sample_id"], "night",
                                   sample["content_sha"],
                                   "Shift roster shows night coverage", "operator")
        self.assertFalse(self.analyzer.status(self.campaign_id, "analysis_one")["current"])
        impact = self.analyzer.shift_impact(self.campaign_id, "analysis_one")
        self.assertTrue(impact["must_reanalyse"])
        self.assertEqual(impact["later_shift_corrections"][0]["sample_id"], sample["sample_id"])
        with self.assertRaises(DomainError) as caught:
            self.publisher.publish(self.campaign_id, "analysis_one", "release_one",
                                   "publisher", analysis["result_sha"])
        self.assertEqual(caught.exception.code, "stale_analysis")

    def test_correction_without_shift_keeps_existing_attribution(self):
        self.add()
        sample = self.store.list_samples(self.campaign_id)[0]
        replacement = {"sample_id": sample["sample_id"], "group_id": sample["group_id"],
                       "event_time": sample["event_time"],
                       "measurements": {"yield_pct": "70.50"}}
        corrected = self.ingest.correct(self.campaign_id, sample["sample_id"], replacement,
                                        sample["content_sha"], "operator")
        self.assertTrue(corrected["changed"])
        self.assertEqual(corrected["shift_id"], sample["shift_id"])

    def test_shift_history_preserves_original_attribution(self):
        self.add()
        sample = self.store.list_samples(self.campaign_id)[0]
        self.ingest.reassign_shift(self.campaign_id, sample["sample_id"], "night",
                                   sample["content_sha"], "Roster correction applies", "operator")
        corrected = self.store.list_samples(self.campaign_id)[0]
        self.assertEqual(corrected["shift_id"], "night")
        self.assertEqual(corrected["shift_source"], "correction")
        audits = [row for row in self.store.list_audits(self.campaign_id)
                  if row["kind"] == "shift_reassigned"]
        self.assertEqual(audits[0]["before"]["shift_id"], "morning")

    # --- composition definition -------------------------------------------
    def test_target_weights_must_sum_to_one(self):
        with self.assertRaises(DomainError) as caught:
            self.compositions.create(self.campaign_id, "target_mix", {
                **COMPOSITION, "strata": {"morning": "0.25", "night": "0.70"}}, "operator")
        self.assertEqual(caught.exception.code, "invalid_composition")

    def test_composition_versions_are_immutable_and_current_is_tracked(self):
        self.compose()
        update = {**COMPOSITION, "actor": "operator",
                  "strata": {"morning": "0.5", "night": "0.5"}}
        changed = self.compositions.update(self.campaign_id, "target_mix", update, 1, "operator")
        self.assertEqual(changed["version"], 2)
        with self.assertRaises(DomainError) as caught:
            self.compositions.update(self.campaign_id, "target_mix", update, 1, "operator")
        self.assertEqual(caught.exception.code, "version_conflict")
        history = self.compositions.versions(self.campaign_id, "target_mix")
        self.assertEqual([row["version"] for row in history], [1, 2])
        self.assertEqual(history[0]["strata"], {"morning": "0.25", "night": "0.75"})

    # --- stratified analysis ----------------------------------------------
    def test_stratified_analysis_reports_layers_and_standardized_contrast(self):
        self.add()
        self.compose()
        result = self.analyze()
        self.assertEqual(result["analysis_kind"], "stratified")
        self.assertEqual(result["target_composition"]["version"], 1)
        self.assertEqual(result["layers"]["morning"]["sample_ids_by_group"],
                         {"control": ["sample_01", "sample_02"],
                          "process_b": ["sample_03", "sample_04"]})
        self.assertTrue(result["stratified_eligible"], result["stratification_reasons"])
        # Layer means: morning +2, night +4.  Target weights .25/.75 -> +3.5,
        # while the pooled difference is (68.75 - 65.75) = 3.0 because the
        # observed mix is half/half rather than the target mix.
        standard = result["standardized"]["yield_pct"]
        self.assertEqual(Decimal(standard["contrasts"]["process_b"]["difference"]), Decimal("3.5"))
        effect = result["composition_effect"]["metrics"]["yield_pct"]["process_b"]
        self.assertEqual(Decimal(effect["within_layer_difference"]), Decimal("3.5"))
        self.assertEqual(Decimal(effect["pooled_difference"]), Decimal("3.0"))
        self.assertEqual(Decimal(effect["observed_mix_difference"]), Decimal("3.0"))
        self.assertEqual(Decimal(effect["composition_mix_effect"]), Decimal("-0.5"))
        self.assertEqual(result["observed_composition"]["weighted_samples"], 8)

    def test_analysis_lists_used_and_excluded_samples(self):
        self.add()
        self.compose()
        result = self.analyze()
        self.assertEqual(result["analyzed_sample_ids"]["control"],
                         ["sample_01", "sample_02", "sample_05", "sample_06"])
        self.assertEqual(result["excluded_samples"], [])

    def test_void_sample_is_listed_as_excluded_not_dropped_silently(self):
        self.add()
        self.compose()
        sample = self.store.list_samples(self.campaign_id)[0]
        self.ingest.void(self.campaign_id, sample["sample_id"], sample["content_sha"],
                         "Coupon was damaged in transit", "operator")
        result = self.analyze("analysis_two")
        excluded = {row["sample_id"]: row["reasons"] for row in result["excluded_samples"]}
        self.assertEqual(excluded[sample["sample_id"]], ["void"])
        self.assertNotIn(sample["sample_id"],
                         sum(result["analyzed_sample_ids"].values(), []))

    def test_unweighted_shift_is_reported_and_excluded(self):
        self.compose({"strata": {"morning": "1"}})
        self.add()
        result = self.analyze()
        self.assertEqual(result["unweighted_strata"][0]["stratum"], "night")
        self.assertEqual(result["unweighted_strata"][0]["reason"], "not_in_composition")
        excluded = {row["sample_id"]: row["reasons"] for row in result["excluded_samples"]}
        self.assertEqual(excluded["sample_05"], ["not_in_composition"])
        self.assertNotIn("night", result["layers"])
        self.assertTrue(result["stratified_eligible"])

    def test_missing_weighted_layer_blocks_stratified_result(self):
        rows = [row for row in samples() if not (row["shift_id"] == "night"
                                                  and row["group_id"] == "process_b")]
        self.add(rows)
        self.compose()
        result = self.analyze()
        self.assertFalse(result["stratified_eligible"])
        self.assertEqual(result["missing_weighted_strata"][0]["stratum"], "night")
        self.assertIn("missing_weighted_stratum", result["stratification_reasons"])
        self.assertFalse(result["quality"]["passed"])
        self.assertIsNone(result["standardized"]["yield_pct"]["group_means"]["process_b"])
        # The remaining layers are not renormalised to absorb the missing weight.
        self.assertEqual(result["target_composition"]["weights"],
                         {"morning": "0.25", "night": "0.75"})
        self.assertEqual(result["observed_composition"]["weighted_samples"], 6)

    def test_stratified_gate_blocks_review_even_with_enough_total_samples(self):
        rows = [row for row in samples() if not (row["shift_id"] == "night"
                                                  and row["group_id"] == "process_b")]
        self.add(rows)
        self.compose()
        result = self.analyze()
        self.assertEqual(result["quality"]["accepted_count"], 6)
        with self.assertRaises(DomainError) as caught:
            self.publisher.review(self.campaign_id, "analysis_one", "reviewer_a", "approve",
                                  "Target mix and layers were checked", result["result_sha"],
                                  "night-heavy export orders", "target_mix", 1)
        self.assertEqual(caught.exception.code, "quality_failed")

    def test_corrected_shift_requires_new_analysis(self):
        self.add()
        self.compose()
        first = self.analyze()
        sample = self.store.list_samples(self.campaign_id)[0]
        self.ingest.reassign_shift(self.campaign_id, sample["sample_id"], "night",
                                   sample["content_sha"], "Roster correction applies", "operator")
        second = self.analyze("analysis_two")
        self.assertNotEqual(first["result_sha"], second["result_sha"])
        self.assertEqual(second["layer_counts"]["morning"]["control"], 1)
        self.assertEqual(second["layer_counts"]["night"]["control"], 3)

    # --- review scope and weights reissue ---------------------------------
    def test_review_must_name_population_and_composition_version(self):
        self.add()
        self.compose()
        analysis = self.analyze()
        with self.assertRaises(DomainError) as caught:
            self.publisher.review(self.campaign_id, "analysis_one", "reviewer_a", "approve",
                                  "Target mix and layers were checked", analysis["result_sha"])
        self.assertEqual(caught.exception.code, "review_scope_required")
        with self.assertRaises(DomainError) as caught:
            self.publisher.review(self.campaign_id, "analysis_one", "reviewer_a", "approve",
                                  "Target mix and layers were checked", analysis["result_sha"],
                                  "some other population", "target_mix", 1)
        self.assertEqual(caught.exception.code, "review_scope_mismatch")

    def test_old_approval_cannot_authorise_reweighted_result(self):
        self.add()
        self.compose()
        analysis = self.analyze()
        self.approve(analysis, composition_version=1)
        update = {**COMPOSITION, "actor": "operator",
                  "strata": {"morning": "0.5", "night": "0.5"}}
        self.compositions.update(self.campaign_id, "target_mix", update, 1, "operator")
        # The old analysis now names a superseded weight version.
        self.assertFalse(self.analyzer.status(self.campaign_id, "analysis_one")["composition_current"])
        with self.assertRaises(DomainError) as caught:
            self.publisher.publish(self.campaign_id, "analysis_one", "release_one",
                                   "publisher", analysis["result_sha"])
        self.assertEqual(caught.exception.code, "composition_superseded")
        reweighted = self.analyze("analysis_two", version=2)
        self.assertEqual(reweighted["target_composition"]["version"], 2)
        self.assertEqual(Decimal(
            reweighted["standardized"]["yield_pct"]["contrasts"]["process_b"]["difference"]),
            Decimal("3.0"))
        with self.assertRaises(DomainError) as caught:
            self.publisher.publish(self.campaign_id, "analysis_two", "release_two",
                                   "publisher", reweighted["result_sha"])
        self.assertEqual(caught.exception.code, "reviews_missing")
        self.approve(reweighted, composition_version=2)
        release = self.publisher.publish(self.campaign_id, "analysis_two", "release_two",
                                         "publisher", reweighted["result_sha"])
        self.assertEqual(release["composition_version"], 2)
        self.assertEqual(release["population"], "night-heavy export orders")

    def test_review_must_name_current_composition_even_before_rebuild(self):
        self.add()
        self.compose()
        analysis = self.analyze()
        update = {**COMPOSITION, "actor": "operator",
                  "strata": {"morning": "0.5", "night": "0.5"}}
        self.compositions.update(self.campaign_id, "target_mix", update, 1, "operator")
        with self.assertRaises(DomainError) as caught:
            self.publisher.review(self.campaign_id, "analysis_one", "reviewer_a", "approve",
                                  "Target mix and layers were checked", analysis["result_sha"],
                                  "night-heavy export orders", "target_mix", 1)
        self.assertEqual(caught.exception.code, "composition_superseded")

    def test_pooled_analysis_remains_available_unchanged(self):
        self.add()
        pooled = self.analyzer.build(self.campaign_id, "pooled_one", self.window["start"],
                                     self.window["end"], "analyst")
        self.assertEqual(pooled["analysis_kind"], "pooled")
        self.assertNotIn("target_composition", pooled)
        self.assertEqual(pooled["contrasts"]["process_b"]["yield_pct"]["difference"], "3.00")
        for reviewer in ("reviewer_a", "reviewer_b"):
            self.publisher.review(self.campaign_id, "pooled_one", reviewer, "approve",
                                  "Historic pooled comparison checked", pooled["result_sha"])
        release = self.publisher.publish(self.campaign_id, "pooled_one", "release_pooled",
                                         "publisher", pooled["result_sha"])
        self.assertEqual(release["population"], "unstratified")
        listed = {row["analysis_id"]: row for row in
                  self.analyzer.list_analyses(self.campaign_id)}
        self.assertEqual(listed["pooled_one"]["analysis_kind"], "pooled")

    def test_declared_shifts_cannot_be_removed_from_spec(self):
        with self.assertRaises(DomainError) as caught:
            self.store.update_spec(self.campaign_id, {**campaign(), "shifts": ["morning"]}, 1, "operator")
        self.assertEqual(caught.exception.code, "incompatible_spec")

    def test_composition_coverage_shows_layers_without_borrowing(self):
        self.add()
        self.compose()
        coverage = Inspector(self.store).composition_coverage(self.campaign_id, "target_mix")
        self.assertEqual(coverage["counts"]["morning"]["control"], 2)
        self.assertEqual(coverage["counts"]["night"]["process_b"], 2)
        self.assertEqual(coverage["insufficient_layers"], [])

    def test_evidence_and_reconciliation_cover_compositions_and_reviews(self):
        self.add()
        self.compose()
        analysis = self.analyze()
        self.approve(analysis)
        self.publisher.publish(self.campaign_id, "analysis_one", "release_one",
                               "publisher", analysis["result_sha"])
        self.assertTrue(EvidenceService(self.store).verify_campaign(self.campaign_id)["ok"])
        self.assertTrue(Reconciler(self.store).full_report(self.campaign_id)["ok"])
        reviews = self.publisher.list_reviews(self.campaign_id, "analysis_one")
        self.assertEqual({row["population"] for row in reviews}, {"night-heavy export orders"})
        self.assertEqual({row["composition_version"] for row in reviews}, {1})


class StratifiedApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.client = TestClient(create_app(str(Path(self.folder.name) / "api.sqlite3")))
        self.addCleanup(self.client.close)
        response = self.client.post("/campaigns", json={**campaign(), "actor": "operator"})
        self.assertEqual(response.status_code, 201, response.text)
        self.base = "/campaigns/shift_trial"
        uploaded = self.client.post(self.base + "/batches/batch_one",
                                    json={"actor": "operator", "samples": samples()})
        self.assertEqual(uploaded.status_code, 200, uploaded.text)

    def test_http_stratified_flow(self):
        created = self.client.post(self.base + "/compositions/target_mix",
                                   json={"actor": "operator", **COMPOSITION})
        self.assertEqual(created.status_code, 201, created.text)
        analysis = self.client.post(self.base + "/analyses/analysis_one",
                                    json={"start": "2026-02-01T00:00:00Z",
                                          "end": "2026-02-02T00:00:00Z", "actor": "analyst",
                                          "composition_id": "target_mix"}).json()
        self.assertEqual(analysis["analysis_kind"], "stratified")
        for reviewer in ("reviewer_a", "reviewer_b"):
            response = self.client.post(self.base + "/analyses/analysis_one/reviews",
                                        json={"reviewer": reviewer, "decision": "approve",
                                              "note": "Target mix and layers were checked",
                                              "expected_sha": analysis["result_sha"],
                                              "population": "night-heavy export orders",
                                              "composition_id": "target_mix",
                                              "composition_version": 1})
            self.assertEqual(response.status_code, 200, response.text)
        released = self.client.post(self.base + "/releases/release_one",
                                    json={"analysis_id": "analysis_one", "actor": "publisher",
                                          "expected_sha": analysis["result_sha"]})
        self.assertEqual(released.status_code, 200, released.text)
        self.assertEqual(released.json()["composition_version"], 1)
        coverage = self.client.get(self.base + "/composition-coverage/target_mix").json()
        self.assertEqual(coverage["weights"]["night"], "0.75")

    def test_http_shift_correction_endpoint(self):
        sample = self.client.get(self.base + "/samples").json()[0]
        response = self.client.put(self.base + "/samples/" + sample["sample_id"] + "/shift",
                                   json={"shift_id": "night", "expected_sha": sample["content_sha"],
                                         "reason": "Roster correction applies", "actor": "operator"})
        self.assertEqual(response.status_code, 200, response.text)
        history = self.client.get(self.base + "/samples/" + sample["sample_id"]
                                  + "/shift-history").json()
        self.assertEqual(history[0]["after_shift"], "night")


class LegacyDatabaseTests(unittest.TestCase):
    def test_preexisting_database_gains_shift_and_review_columns(self):
        import sqlite3
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "legacy.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript("""
            CREATE TABLE campaigns (id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE specs (campaign_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
                content_sha TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(campaign_id,version));
            CREATE TABLE samples (campaign_id TEXT NOT NULL, sample_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                group_id TEXT NOT NULL, event_time TEXT NOT NULL, spec_version INTEGER NOT NULL,
                measurements TEXT NOT NULL, content_sha TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(campaign_id,sample_id));
            CREATE TABLE reviews (campaign_id TEXT NOT NULL, analysis_id TEXT NOT NULL,
                reviewer TEXT NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL,
                result_sha TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(campaign_id,analysis_id,reviewer));
        """)
        connection.commit()
        connection.close()
        store = Store(path)
        with store.connect() as db:
            review_columns = {row["name"] for row in db.execute("PRAGMA table_info(reviews)")}
            sample_columns = {row["name"] for row in db.execute("PRAGMA table_info(samples)")}
        self.assertTrue({"population", "composition_id", "composition_version"} <= review_columns)
        self.assertTrue({"shift_id", "shift_source", "shift_updated_at"} <= sample_columns)


if __name__ == "__main__":
    unittest.main()
