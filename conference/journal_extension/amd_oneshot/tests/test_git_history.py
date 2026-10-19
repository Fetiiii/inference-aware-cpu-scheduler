"""Synthetic Git-delivery identity tests; no private ZIP or runtime required."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from conference.journal_extension.amd_oneshot import coordinator, historical_identity as history, protocol
from conference.journal_extension.amd_oneshot.common import load_json, sha256_file
from conference.journal_extension.amd_oneshot.tests import test_coordinator as lifecycle


class HistoricalIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-public-history-')
        self.addCleanup(self.temp.cleanup)
        self.session = Path(self.temp.name)
        self.plan = protocol.load_plan()

    def test_public_identity_has_no_private_raw_or_paths(self):
        loaded = history.load_identity(self.plan)
        text = json.dumps(loaded)
        for private in ('/home/', '/tmp/', 'raw/environment/', 'server_log'):
            self.assertNotIn(private, text)
        self.assertFalse(loaded['manifest']['private_raw_bundled'])
        self.assertEqual(loaded['manifest']['collaborator_raw_verification'], 'NOT_CHECKED')
        self.assertEqual(loaded['diagnostic_build']['server_binary']['sha256'], history.SERVER_SHA256)
        self.assertEqual(loaded['diagnostic_build']['phase_mark_capability']['matched_file_identities'],
                         [{'sha256': history.MARKER_SHA256}])

    def test_stage_and_verify_without_private_archive(self):
        result = history.stage_identity(self.session, self.plan)
        self.assertFalse(result['session_identity']['private_raw_present'])
        verified = history.verify_session_identity(self.session, self.plan)
        self.assertEqual(verified['diagnostic_build'], result['diagnostic_build'])
        self.assertEqual(sha256_file(self.session / 'historical_identity_source.json'),
                         result['session_identity']['source_sha256'])
        self.assertEqual(len(verified['guards']), 3)
        self.assertFalse((self.session / 'historical_c03').exists())

    def test_stage_exclusively_refuses_existing_record(self):
        history.stage_identity(self.session, self.plan)
        with self.assertRaises(ValueError):
            history.stage_identity(self.session, self.plan)

    def test_every_frozen_anchor_and_workload_is_checked(self):
        for key in ('historical_archive_sha256', 'historical_binary_sha256',
                    'source_revision', 'model_sha256', 'prompt_sha256', 'cooldown_s'):
            plan = deepcopy(self.plan)
            plan[key] = 'changed'
            with self.subTest(key=key), self.assertRaises(ValueError):
                history.load_identity(plan)
        plan = deepcopy(self.plan)
        plan['runtime']['ctx'] += 1
        with self.assertRaises(ValueError):
            history.load_identity(plan)

    def test_tampered_public_schema_and_known_marker_refused(self):
        for mutate in (lambda m: m.update(extra_private_path='/synthetic/private'),
                       lambda m: m['diagnostic_build']['phase_mark_capability'].update(matched_file_identities=[]),
                       lambda m: m.update(collaborator_raw_verification='PASS')):
            manifest = history.load_identity(self.plan)['manifest']
            mutate(manifest)
            source = self.session / 'synthetic.json'
            source.write_text(json.dumps(manifest))
            with patch.object(history, 'IDENTITY_PATH', source), self.assertRaises(ValueError):
                history.load_identity(self.plan)

    def test_modified_session_copies_refused(self):
        history.stage_identity(self.session, self.plan)
        source = self.session / 'historical_identity_source.json'
        original = source.read_bytes()
        source.write_bytes(original + b' ')
        with self.assertRaises(ValueError):
            history.verify_session_identity(self.session, self.plan)
        source.write_bytes(original)
        record = self.session / 'historical_identity.json'
        value = load_json(record)
        value['private_raw_present'] = True
        record.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            history.verify_session_identity(self.session, self.plan)

    def test_symlink_session_source_refused(self):
        history.stage_identity(self.session, self.plan)
        source = self.session / 'historical_identity_source.json'
        source.unlink()
        source.symlink_to(history.IDENTITY_PATH)
        with self.assertRaises(ValueError):
            history.verify_session_identity(self.session, self.plan)


class GitCoordinatorHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = lifecycle.CoordinatorTests('test_full_72_and_completed_never_rerun')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.session = self.fixture.session
        self.repo = Path(self.fixture.temp.name) / 'synthetic-checkout'
        self.repo.mkdir()
        (self.repo / 'synthetic.json').write_bytes((self.session / 'source/synthetic.json').read_bytes())
        files = {'synthetic.json': sha256_file(self.repo / 'synthetic.json')}
        self.report = {'commit_sha': 'a' * 40, 'source_sha256': self.fixture.package,
                       'required_files': files}
        (self.session / 'historical_identity.json').unlink()
        history.stage_identity(self.session, self.fixture.plan)
        # No historical collection/inventory prerequisite in Git delivery.
        (self.session / 'historical_collection.json').unlink()
        (self.session / 'historical_c03/history_inventory.json').unlink()
        self.rewrite('source_manifest.json', {'package_sha256': self.fixture.package,
                     'files': files, 'git_source': self.report})
        self.rewrite('git_provenance.json', self.report)
        config = load_json(self.session / 'config.json')
        config.update(identity_kind='git', git_commit=self.report['commit_sha'], repo_root=str(self.repo),
                      historical_identity=history.load_identity(self.fixture.plan)['diagnostic_build'])
        self.assertNotIn('repo', config)
        self.rewrite('config.json', config)
        from conference.journal_extension.amd_oneshot import git_delivery
        approval = git_delivery.approval_template(self.fixture.plan, self.report)
        approval.update(authorized=True, author_review_id='SYNTHETIC GIT LIFECYCLE ONLY')
        self.rewrite('execution_authorized.json', approval)

    def rewrite(self, name, value):
        (self.session / name).write_text(json.dumps(value))

    def run_session(self):
        with patch('conference.journal_extension.amd_oneshot.git_delivery.verify_checkout', return_value=self.report), \
                patch.object(protocol, 'check_approval', wraps=protocol.check_approval) as approval:
            result = coordinator.execute_session(self.session, self.fixture.deps)
            self.assertEqual(approval.call_args.kwargs['git_report'], self.report)
            return result

    def test_git_session_completes_without_historical_collection(self):
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        self.assertEqual(len(self.fixture.calls), 72)

    def test_git_provenance_mismatch_stops_before_request(self):
        self.rewrite('git_provenance.json', {**self.report, 'commit_sha': 'b' * 40})
        self.assertEqual(self.run_session()['status'], 'STOPPED')
        self.assertFalse(self.fixture.calls)

    def test_checkout_source_guard_detects_edit_between_requests(self):
        backend = self.fixture.deps['backend']
        def mutate(*args):
            record = backend(*args)
            (self.repo / 'synthetic.json').write_text('changed checkout')
            return record
        self.fixture.deps['backend'] = mutate
        result = self.run_session()
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual(len(self.fixture.calls), 1)

    def test_public_session_identity_tampering_stops_before_request(self):
        record = load_json(self.session / 'historical_identity.json')
        record['status'] = 'PASS'
        self.rewrite('historical_identity.json', record)
        self.assertEqual(self.run_session()['status'], 'STOPPED')
        self.assertFalse(self.fixture.calls)

    def test_git_package_hash_uses_verified_checkout(self):
        with patch('conference.journal_extension.amd_oneshot.git_delivery.verify_checkout', return_value=self.report) as verify:
            config = load_json(self.session / 'config.json')
            self.assertNotIn('repo', config)
            self.assertEqual(config['repo_root'], str(self.repo))
            self.assertEqual(coordinator.package_hash(config), self.report['source_sha256'])
            verify.assert_called_once_with(root=self.repo, expected_commit='a' * 40)
