"""Safety-only amendment and isolated-session delivery contract; no requests."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from conference.journal_extension.amd_oneshot import cli, protocol
from conference.journal_extension.amd_oneshot.common import digest


ROOT = Path(__file__).resolve().parents[4]
BASE = 'ab9d3bd3d8bc32c1a659da3b8eedb32f9338a045'


class HotfixContractTests(unittest.TestCase):
    def test_only_safety_plan_fields_change_and_both_schedules_are_identical(self):
        original = json.loads(subprocess.check_output([
            'git', '-C', str(ROOT), 'show',
            BASE + ':conference/journal_extension/amd_oneshot/RUN_PLAN.json']))
        current = protocol.load_plan()
        old_science, new_science = copy.deepcopy(original), copy.deepcopy(current)
        old_science.pop('safety'); new_science.pop('safety')
        self.assertEqual(old_science, new_science)
        expected = {**original['safety'], 'min_available_memory_gib': 8,
                    'min_build_available_memory_gib': 12,
                    'min_runtime_available_memory_gib': 1}
        self.assertEqual(current['safety'], expected)
        self.assertNotEqual(digest(current), digest(original))
        for tier, count in [('FULL', 72), ('STATIC_SIGNAL', 48)]:
            self.assertEqual(protocol.schedule(original, tier=tier),
                             protocol.schedule(current, tier=tier))
            self.assertEqual(len(protocol.schedule(current, tier=tier)), count)
        # Existing approvals carry the former plan digest, not the amended one.
        approval = {**protocol.approval_bindings(original), 'authorized': True,
                    'author_review_id': 'SYNTHETIC-OLD', 'package_sha256': 'fixture'}
        with self.assertRaises(ValueError):
            protocol.check_approval(current, approval, 'fixture')

    def test_retry_commands_explicitly_bind_all_operations_to_new_session(self):
        doc = (protocol.PLAN_PATH.parent / 'GIT_DELIVERY_TR.md').read_text()
        for operation in ('preflight', 'run', 'package'):
            self.assertIn(operation + ' --session results/journal_extension_amd_retry01', doc)
        self.assertIn('verify "TAM_ARSIV_YOLU.zip" --session results/journal_extension_amd_retry01', doc)
        self.assertIn('hotfix_execution_approval.json', doc)

    def test_existing_session_cannot_be_overwritten_or_start_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / 'results/journal_extension_amd_01'
            session.mkdir(parents=True)
            evidence = session / 'evidence.json'
            evidence.write_bytes(b'original private evidence')
            args = cli.parser().parse_args(['preflight', '--repo', str(root),
                                           '--session', str(session)])
            with patch.object(cli.distribution, 'CODE_ROOT', root), \
                    patch.object(cli.preflight, 'perform_preflight') as perform:
                with self.assertRaisesRegex(ValueError, 'Session already exists'):
                    cli.do_preflight(args)
                perform.assert_not_called()
            self.assertEqual(evidence.read_bytes(), b'original private evidence')


if __name__ == '__main__':
    unittest.main()
