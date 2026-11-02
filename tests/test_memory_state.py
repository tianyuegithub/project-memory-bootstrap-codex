from __future__ import annotations

import hashlib
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_memory_bootstrap_codex import memory_state as state
from project_memory_bootstrap_codex.bootstrap import doctor, init_project
from project_memory_bootstrap_codex.errors import MemoryProtocolError
from project_memory_bootstrap_codex.facts import parse_facts, reject_sensitive, native_credential_context
from project_memory_bootstrap_codex.memory_index import context_from_ids, get_memory_by_id, index_status, rebuild_index, search_memory
from project_memory_bootstrap_codex.project import register_project


def crash_publication(root: str, proposal: str, after_file: bool) -> None:
    def terminate(*args):
        os._exit(72 if after_file else 71)
    if after_file:
        state._finish = terminate
    else:
        state._atomic_text = terminate
    state.commit(Path(root), proposal)


def concurrent_publication(root: str, proposal: str, results) -> None:
    try:
        result = state.commit(Path(root), proposal)
        results.put(result["status"])
    except MemoryProtocolError as exc:
        results.put(exc.code)


class MemoryStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.registry = self.root / "local-registry.json"
        registry_environment = patch.dict(os.environ, {"PROJECT_MEMORY_REGISTRY": str(self.registry)})
        registry_environment.start()
        self.addCleanup(registry_environment.stop)
        init_project(self.root)
        self.context = register_project(self.root, "test-project", {"root": "."}, maintenance_enabled=True,
                                        registry_path=self.registry)
        state.initialize_state(self.context, new_registration=True)
        self.sequence = 0

    def capture(self, object_id: str | None = None, **extra):
        self.sequence += 1
        payload = {"source_instance": "test", "object_id": object_id or str(self.sequence),
                   "source_revision": "1", "locator": "test://controlled-observation", "kind": "observation",
                   "observed_at": f"2026-11-02T01:00:{self.sequence:02d}+00:00", "excerpt": "Verified fixture observation."}
        payload.update(extra)
        return state.capture(self.root, payload), payload

    def change(self, fact_id: str, source, **extra):
        result = {"fact_id": fact_id, "expected_version": None, "title": "Fixture fact", "body": "A verified fixture fact.",
                  "kind": "decision", "scope": ["repo:root"], "status": "active",
                  "sources": [{"source_id": source["source_id"], "version": source["source_revision"], "locator": source["locator"]}],
                  "verified_at": "2026-11-02", "verification": "Direct fixture observation, no runtime deployment claim.",
                  "expires_when": "The fixture decision is superseded."}
        result.update(extra)
        return result

    def proposal(self, source, changes):
        return state.propose(self.root, {"candidate_id": source["candidate_id"], "verified_by": "test-task",
                                        "verification_scope": "Only the deterministic fixture.", "changes": changes})["proposal_id"]

    def publish(self, fact_id: str, **extra):
        source, _ = self.capture()
        proposal = self.proposal(source, [self.change(fact_id, source, **extra)])
        return state.commit(self.root, proposal)

    def test_capture_commit_and_replay_are_idempotent(self):
        source, payload = self.capture()
        duplicate = state.capture(self.root, payload)
        self.assertEqual(duplicate["candidate_id"], source["candidate_id"])
        self.assertTrue(duplicate["duplicate"])
        payload["observed_at"] = "2026-11-02T02:00:00+00:00"
        self.assertEqual(state.capture(self.root, payload)["candidate_id"], source["candidate_id"])
        proposal = self.proposal(source, [self.change("F-one", source)])
        first = state.commit(self.root, proposal)
        self.assertEqual(first, state.commit(self.root, proposal))
        self.assertEqual(state.inspect(self.root, "F-one")["metadata"]["revision"], 1)
        self.assertEqual(state.pending(self.root), [])
        self.assertEqual(get_memory_by_id(self.root, "F-one").id, "F-one")

    def test_multiple_facts_from_one_source_are_published_together(self):
        source, _ = self.capture()
        proposal = self.proposal(source, [self.change("F-one", source), self.change("F-two", source)])
        receipt = state.commit(self.root, proposal)
        self.assertEqual({r["fact_id"] for r in receipt["facts"]}, {"F-one", "F-two"})
        self.assertEqual(len(parse_facts(self.context.memory_file.read_text())), 2)

    def test_supporting_evidence_does_not_create_duplicate_semantic_work(self):
        evidence, payload = self.capture(enqueue=False)
        self.assertEqual(evidence["status"], "evidence")
        self.assertEqual(state.pending(self.root), [])
        source, _ = self.capture()
        change = self.change("F-supported", evidence)
        state.commit(self.root, self.proposal(source, [change]))
        self.assertEqual(state.pending(self.root), [])
        payload["enqueue"] = True
        queued = state.capture(self.root, payload)
        self.assertEqual(queued["candidate_id"], evidence["candidate_id"])
        self.assertEqual(queued["status"], "pending")
        payload["enqueue"] = False
        self.assertEqual(state.capture(self.root, payload)["status"], "pending")

    def test_no_new_knowledge_records_a_decision_without_rewriting_memory(self):
        self.publish("F-one")
        original_inode = self.context.memory_file.stat().st_ino
        published_at = state.health(self.root)["last_publication"]
        source, _ = self.capture()
        proposal = state.propose(self.root, {"candidate_id": source["candidate_id"], "verified_by": "test-task",
            "verification_scope": "Reviewed this fixture event.", "changes": [], "skip_reason": "No new reusable knowledge."})
        self.assertEqual(state.commit(self.root, proposal["proposal_id"])["facts"], [])
        self.assertEqual(self.context.memory_file.stat().st_ino, original_inode)
        self.assertEqual(state.health(self.root)["last_publication"], published_at)
        self.assertEqual(state.pending(self.root), [])

    def test_conflicting_fact_version_preserves_pending_candidate(self):
        self.publish("F-one")
        source, _ = self.capture()
        proposal = self.proposal(source, [self.change("F-one", source, body="This stale writer must not replace it.")])
        with self.assertRaises(MemoryProtocolError) as raised:
            state.commit(self.root, proposal)
        self.assertEqual(raised.exception.code, "VERSION_CONFLICT")
        self.assertEqual(len(state.pending(self.root)), 1)
        self.assertNotIn("stale writer", state.inspect(self.root, "F-one")["body"])

    def test_real_process_death_recovers_before_or_after_file_replace(self):
        for after_file in (False, True):
            with self.subTest(after_file=after_file):
                fact_id = "F-after" if after_file else "F-before"
                source, _ = self.capture()
                proposal = self.proposal(source, [self.change(fact_id, source)])
                process = multiprocessing.get_context("spawn").Process(target=crash_publication,
                    args=(str(self.root), proposal, after_file))
                process.start()
                process.join(10)
                self.assertEqual(process.exitcode, 72 if after_file else 71)
                self.assertEqual(state.health(self.root)["status"], "recovery_required")
                state.recover(self.root)
                self.assertEqual(state.inspect(self.root, fact_id)["metadata"]["revision"], 1)
                self.assertEqual(state.commit(self.root, proposal)["facts"][0]["revision"], 1)
                self.assertEqual(state.health(self.root)["prepared_operations"], 0)

    def test_concurrent_processes_preserve_different_facts_in_one_file(self):
        proposals = []
        for fact_id in ("F-a", "F-b"):
            source, _ = self.capture()
            proposals.append(self.proposal(source, [self.change(fact_id, source)]))
        ctx = multiprocessing.get_context("spawn")
        results = ctx.Queue()
        processes = [ctx.Process(target=concurrent_publication, args=(str(self.root), p, results)) for p in proposals]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual([results.get(timeout=2) for _ in processes], ["committed", "committed"])
        self.assertEqual({f.fact_id for f in parse_facts(self.context.memory_file.read_text())}, {"F-a", "F-b"})

    def test_superseded_fact_resolves_to_current_successor(self):
        self.publish("F-old", body="Old qualified rule.")
        old = state.inspect(self.root, "F-old")
        source, _ = self.capture()
        changes = [self.change("F-old", source, expected_version=old["version"], status="superseded", body="Old qualified rule."),
                   self.change("F-new", source, body="New qualified rule.", supersedes=["F-old"])]
        state.commit(self.root, self.proposal(source, changes))
        self.assertEqual(get_memory_by_id(self.root, "F-old").id, "F-new")
        self.assertEqual(get_memory_by_id(self.root, "F-old", history=True).id, "F-old")
        self.assertNotIn("Old qualified rule", context_from_ids(self.root, ["F-old"]))

    def test_forgetting_survives_interruption_and_state_loss_is_not_bypassed(self):
        self.publish("F-private", body="Private fixture phrase that must stop being recalled.")
        old_document = self.context.memory_file.read_text()
        self.assertTrue(search_memory(self.root, "Private fixture"))
        old = state.inspect(self.root, "F-private")
        source, _ = self.capture()
        proposal = self.proposal(source, [self.change("F-private", source, expected_version=old["version"], status="forgotten", body="")])
        process = multiprocessing.get_context("spawn").Process(target=crash_publication, args=(str(self.root), proposal, False))
        process.start()
        process.join(10)
        self.assertEqual(process.exitcode, 71)
        self.assertEqual(search_memory(self.root, "Private fixture", history=True), [])
        self.assertIsNone(get_memory_by_id(self.root, "F-private", history=True))
        self.assertNotIn("Private fixture phrase", self.context.memory_file.read_text())
        source2, _ = self.capture()
        proposed = self.proposal(source2, [self.change("F-private", source2)])
        with self.assertRaises(MemoryProtocolError) as raised:
            state.commit(self.root, proposed)
        self.assertEqual(raised.exception.code, "FACT_SUPPRESSED")
        self.context.memory_file.write_text(old_document)
        (self.context.state_dir / "ledger.sqlite").unlink()
        with self.assertRaises(MemoryProtocolError) as raised:
            search_memory(self.root, "Private fixture", history=True)
        self.assertEqual(raised.exception.code, "STATE_MISSING")
        with self.assertRaises(MemoryProtocolError):
            state.initialize_state(self.context)

    def test_changed_file_evidence_cannot_be_published(self):
        evidence = self.root / "evidence.md"
        evidence.write_text("Original requirement.\n")
        version = hashlib.sha256(evidence.read_bytes()).hexdigest()
        source, _ = self.capture("evidence", kind="file", locator="evidence.md", source_revision=version)
        change = self.change("F-file", source)
        change["sources"][0]["version"] = version
        proposal = self.proposal(source, [change])
        evidence.write_text("Changed requirement.\n")
        with self.assertRaises(MemoryProtocolError) as raised:
            state.commit(self.root, proposal)
        self.assertEqual(raised.exception.code, "SOURCE_CHANGED")
        self.assertEqual(state.pending(self.root)[0]["candidate_id"], source["candidate_id"])

    def test_late_source_does_not_roll_back_observed_revision(self):
        source, _ = self.capture("message", source_revision="new", observed_at="2026-11-02T03:00:00+00:00")
        older, _ = self.capture("message", source_revision="old", observed_at="2026-11-02T02:00:00+00:00")
        change = self.change("F-late", older)
        change["sources"][0]["version"] = "old"
        proposal = self.proposal(older, [change])
        with self.assertRaises(MemoryProtocolError) as raised:
            state.commit(self.root, proposal)
        self.assertEqual(raised.exception.code, "SOURCE_CHANGED")

    def test_duplicate_current_observation_prevents_a_late_revision_from_winning(self):
        source, payload = self.capture("clocked", source_revision="A", observed_at="2026-11-02T01:00:00+00:00")
        payload["observed_at"] = "2026-11-02T03:00:00+00:00"
        duplicate = state.capture(self.root, payload)
        self.assertEqual(duplicate["candidate_id"], source["candidate_id"])
        late, _ = self.capture("clocked", source_revision="B", observed_at="2026-11-02T02:00:00+00:00")
        proposal = self.proposal(late, [self.change("F-late-clock", late)])
        with self.assertRaises(MemoryProtocolError) as raised:
            state.commit(self.root, proposal)
        self.assertEqual(raised.exception.code, "SOURCE_CHANGED")
        skipped = state.propose(self.root, {"candidate_id": late["candidate_id"], "verified_by": "test-task",
            "verification_scope": "Compared source observation times.", "changes": [], "skip_reason": "This version predates the current observation."})
        self.assertEqual(state.commit(self.root, skipped["proposal_id"])["facts"], [])

    def test_explicit_source_forgetting_removes_related_facts_and_proposals(self):
        source, payload = self.capture()
        shared = [self.change("F-source-one", source, body="Source-private-one."),
                  self.change("F-source-two", source, body="Source-private-two.")]
        state.commit(self.root, self.proposal(source, shared))
        second_version, _ = self.capture()
        old_two = state.inspect(self.root, "F-source-two")
        state.commit(self.root, self.proposal(second_version, [
            self.change("F-source-two", source, expected_version=old_two["version"], status="superseded", body="Source-private-two."),
            self.change("F-source-latest", source, supersedes=["F-source-two"], body="Source-private-latest."),
        ]))
        self.assertTrue(search_memory(self.root, "Source-private"))
        other, _ = self.capture()
        waiting = self.proposal(other, [self.change("F-source-future", source, body="Source-private-future.")])
        old = state.inspect(self.root, "F-source-one")
        forget, _ = self.capture()
        proposal = self.proposal(forget, [self.change("F-source-one", forget, expected_version=old["version"],
            status="forgotten", body="", forget_sources=True)])
        receipt = state.commit(self.root, proposal)
        self.assertEqual(set(receipt["forgotten_fact_ids"]), {"F-source-one", "F-source-two", "F-source-latest"})
        for fact_id in ("F-source-one", "F-source-two", "F-source-latest"):
            self.assertIsNone(get_memory_by_id(self.root, fact_id, history=True))
            with self.assertRaises(MemoryProtocolError):
                state.inspect(self.root, fact_id)
        self.assertEqual(search_memory(self.root, "Source-private", history=True), [])
        self.assertNotIn("Source-private", self.context.memory_file.read_text())
        with self.assertRaises(MemoryProtocolError):
            state.commit(self.root, waiting)
        with self.assertRaises(MemoryProtocolError):
            state.candidate(self.root, other["candidate_id"])
        with self.assertRaises(MemoryProtocolError) as raised:
            state.capture(self.root, payload)
        self.assertEqual(raised.exception.code, "SOURCE_SUPPRESSED")

    def test_fact_only_forgetting_does_not_forget_other_facts_from_the_source(self):
        source, _ = self.capture()
        state.commit(self.root, self.proposal(source, [self.change("F-one-scope", source), self.change("F-two-scope", source)]))
        first = state.inspect(self.root, "F-one-scope")
        forget, _ = self.capture()
        state.commit(self.root, self.proposal(forget, [self.change("F-one-scope", forget, expected_version=first["version"], status="forgotten", body="")]))
        self.assertIsNotNone(get_memory_by_id(self.root, "F-two-scope"))

    def test_candidate_cannot_publish_two_competing_decisions(self):
        source, _ = self.capture()
        first = self.proposal(source, [self.change("F-first", source)])
        second = self.proposal(source, [self.change("F-second", source)])
        state.commit(self.root, first)
        with self.assertRaises(MemoryProtocolError) as raised:
            state.commit(self.root, second)
        self.assertEqual(raised.exception.code, "CANDIDATE_PROCESSED")

    def test_source_reverting_to_earlier_content_is_a_new_observation(self):
        first, _ = self.capture("changing", source_revision="old")
        self.capture("changing", source_revision="new")
        restored, _ = self.capture("changing", source_revision="old")
        self.assertNotEqual(first["candidate_id"], restored["candidate_id"])
        self.assertGreater(restored["source_epoch"], first["source_epoch"])
        proposal = self.proposal(first, [self.change("F-obsolete", first)])
        with self.assertRaises(MemoryProtocolError) as raised:
            state.commit(self.root, proposal)
        self.assertEqual(raised.exception.code, "SOURCE_CHANGED")

    def test_source_identity_cannot_be_retyped_to_bypass_file_checks(self):
        with self.assertRaises(MemoryProtocolError) as raised:
            self.capture("pretend-file", source_instance="file", kind="observation")
        self.assertEqual(raised.exception.code, "CANDIDATE_INVALID")
        self.capture("stable-object")
        with self.assertRaises(MemoryProtocolError) as raised:
            self.capture("stable-object", source_revision="2", kind="different-source-type")
        self.assertEqual(raised.exception.code, "SOURCE_IDENTITY_CONFLICT")

    def test_external_edit_is_preserved_and_reported(self):
        self.publish("F-one")
        edited = self.context.memory_file.read_text() + "\nExternal human note.\n"
        self.context.memory_file.write_text(edited)
        with self.assertRaises(MemoryProtocolError) as raised:
            get_memory_by_id(self.root, "F-one")
        self.assertEqual(raised.exception.code, "EXTERNAL_EDIT_CONFLICT")
        self.assertEqual(self.context.memory_file.read_text(), edited)

    def test_empty_governed_index_is_current_and_legacy_is_not_active(self):
        rebuild_index(self.root)
        self.assertTrue(index_status(self.root).is_current)
        self.assertEqual(search_memory(self.root, "memory"), [])
        with self.assertRaises(MemoryProtocolError):
            search_memory(self.root, "anything", include_legacy=True)

    def test_budget_returns_a_locator_instead_of_cutting_conditions(self):
        body = "BODY_START " + "有证据的长说明。" * 250 + " END_CONDITION: only for the fixture."
        self.publish("F-long", body=body)
        small = context_from_ids(self.root, ["F-long"], max_tokens=120)
        self.assertIn("F-long", small)
        self.assertNotIn("BODY_START", small)
        full = get_memory_by_id(self.root, "F-long").text
        self.assertIn("BODY_START", full)
        self.assertIn("END_CONDITION", full)

    def test_candidate_credentials_are_rejected_before_storage(self):
        with self.assertRaises(MemoryProtocolError) as raised:
            self.capture(excerpt="api_key=abcdefgh1234567890")
        self.assertEqual(raised.exception.code, "SENSITIVE_CONTENT")
        self.assertEqual(state.pending(self.root), [])

    def test_capture_rejects_chinese_backtick_and_raw_quoted_credentials(self):
        examples = ['密码：`FICTIONAL_VALUE_A123`', '口令为 FICTIONAL_VALUE_B123',
                    '密钥是 `FICTIONAL_VALUE_C123`', '令牌: FICTIONAL_VALUE_D123',
                    'api_key：`FICTIONAL_VALUE_E123`', 'password="FICTIONAL_VALUE_F123"']
        for index, text in enumerate(examples):
            with self.subTest(example=index):
                with self.assertRaises(MemoryProtocolError) as raised:
                    self.capture(excerpt=text)
                self.assertEqual(raised.exception.code, 'SENSITIVE_CONTENT')
        self.assertEqual(state.pending(self.root), [])

    def test_propose_rejects_raw_credential_strings_nested_in_fact_bodies(self):
        source, _ = self.capture()
        examples = ['密码：`FICTIONAL_BODY_A123`', '口令为 FICTIONAL_BODY_B123',
                    '密钥是 `FICTIONAL_BODY_C123`', '令牌: FICTIONAL_BODY_D123',
                    'token=`FICTIONAL_BODY_E123`', 'password="FICTIONAL_BODY_F123"']
        for index, text in enumerate(examples):
            with self.subTest(example=index):
                with self.assertRaises(MemoryProtocolError) as raised:
                    self.proposal(source, [self.change('F-sensitive-body', source, body=text)])
                self.assertEqual(raised.exception.code, 'SENSITIVE_CONTENT')
        with state._locked(self.context) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM proposals').fetchone()[0], 0)
        # The whole JSON pass still catches credentials represented as key/value.
        with self.assertRaises(MemoryProtocolError) as raised:
            reject_sensitive({'nested': [{'password': 'FICTIONAL_STRUCTURED_123'}]})
        self.assertEqual(raised.exception.code, 'SENSITIVE_CONTENT')

    def test_secret_references_and_existing_placeholder_forms_remain_allowed(self):
        reject_sensitive({'secretRef': 'fixture-reference', 'password': '${FIXTURE_PASSWORD}'})
        reject_sensitive({'password': '<redacted>'})
        safe = ('密码通过 SecretRef 解析。 '
                '密码：`<REDACTED>`；口令为 `$ENV_VALUE`；密钥是 `********`；令牌: `...PLACEHOLDER`。 '
                'password="<REDACTED>" token=`$ENV_VALUE` api_key=******** secret=...PLACEHOLDER')
        source, _ = self.capture(excerpt=safe)
        proposal = self.proposal(source, [self.change('F-reference-only', source, body=safe)])
        self.assertEqual(state.commit(self.root, proposal)['status'], 'committed')

    def test_mechanical_curl_basic_auth_and_literal_credential_defaults_are_rejected(self):
        snippets = ['curl -u fixture-user:FICTIONAL_AUTH_VALUE https://fixture.invalid/',
                    'curl --user=fixture-user:FICTIONAL_AUTH_VALUE https://fixture.invalid/',
                    'curl -ufixture-user:FICTIONAL_AUTH_VALUE https://fixture.invalid/',
                    '${PG_PASSWORD:-FICTIONAL_DEFAULT_VALUE}', '${ACCESS_TOKEN-FICTIONAL_DEFAULT_VALUE}',
                    '${API_SECRET:-FICTIONAL_DEFAULT_VALUE}']
        source, _ = self.capture()
        for index, snippet in enumerate(snippets):
            with self.subTest(example=index):
                with self.assertRaises(MemoryProtocolError) as raised:
                    self.capture(excerpt=snippet)
                self.assertEqual(raised.exception.code, 'SENSITIVE_CONTENT')
                with self.assertRaises(MemoryProtocolError) as raised:
                    self.proposal(source, [self.change('F-mechanical-sensitive', source, body=snippet)])
                self.assertEqual(raised.exception.code, 'SENSITIVE_CONTENT')
        self.assertEqual(len(state.pending(self.root)), 1)

    def test_native_context_is_separate_from_curated_shared_fact_policy(self):
        contexts = ['密码口径见关联笔记', '口令由环境管理', '密钥采用引用', '令牌统一管理', '凭据不保存', '凭证清理',
                    'password policy', 'passwd policy', 'username field', 'credential reference', 'secret policy',
                    'API key rotation', 'access-key reference', 'private_key policy', 'service token policy',
                    'access_token policy', 'auth token policy', 'session-token policy', '服务token策略',
                    'Authorization header policy', 'Cookie policy', 'BasicAuth reference', 'curl -u "$USER:$PASS" URL']
        for index, context in enumerate(contexts):
            with self.subTest(context=index):
                self.assertTrue(native_credential_context(context))
        references = ['${PG_PASSWORD}', '$FIXTURE_PASSWORD', '<redacted>', '${PG_PASSWORD:-<redacted>}',
                      '${ACCESS_TOKEN:-$OTHER_TOKEN}', 'curl -u "$FIXTURE_USER:${PG_PASSWORD}" https://fixture.invalid/',
                      '预算 token 为 4096，output tokens 为 512。']
        for index, reference in enumerate(references):
            with self.subTest(reference=index):
                reject_sensitive(reference)
        self.assertFalse(native_credential_context('预算 token 为 4096，output tokens 为 512。'))
        self.assertFalse(native_credential_context('${PG_PASSWORD}'))
        policy = '密码与 access token 通过 SecretRef 解析，普通共享事实只保存抽象规范。'
        self.assertTrue(native_credential_context(policy))
        source, _ = self.capture(excerpt=policy)
        proposal = self.proposal(source, [self.change('F-curated-credential-policy', source, body=policy)])
        self.assertEqual(state.commit(self.root, proposal)['status'], 'committed')

    def test_registered_initialization_cannot_reset_published_memory(self):
        self.publish("F-persist")
        original = self.context.memory_file.read_text()
        with self.assertRaises(MemoryProtocolError) as raised:
            init_project(self.root, force=True)
        self.assertEqual(raised.exception.code, "GOVERNED_INIT_FORBIDDEN")
        self.assertEqual(self.context.memory_file.read_text(), original)
        self.context.memory_file.unlink()
        with self.assertRaises(MemoryProtocolError) as raised:
            init_project(self.root)
        self.assertEqual(raised.exception.code, "MEMORY_MISSING")
        self.assertFalse(self.context.memory_file.exists())

    def test_doctor_does_not_report_healthy_when_state_is_missing(self):
        self.assertTrue(doctor(self.root).ok)
        (self.context.state_dir / "ledger.sqlite").unlink()
        report = doctor(self.root)
        self.assertFalse(report.ok)
        self.assertTrue(any("STATE_MISSING" in item for item in report.messages))


if __name__ == "__main__":
    unittest.main()
