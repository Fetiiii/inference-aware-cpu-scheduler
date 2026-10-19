"""Synthetic repositories only; these approvals never authorize real execution."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from conference.journal_extension.amd_oneshot import distribution, git_delivery as gd, protocol


class GitDeliveryTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()

    def init(self, root):
        root.mkdir(exist_ok=True)
        self.git(root, 'init', '-q')
        self.git(root, 'config', 'user.name', 'Synthetic Test')
        self.git(root, 'config', 'user.email', 'synthetic@example.invalid')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'checkout'
        self.init(self.root)
        sub = self.root / 'llama.cpp'
        self.init(sub)
        (sub / 'synthetic').write_text('synthetic runtime only\n')
        self.git(sub, 'add', 'synthetic')
        self.git(sub, 'commit', '-qm', 'synthetic runtime')
        pinned = self.git(sub, 'rev-parse', 'HEAD')
        patcher = patch.object(gd, 'PINNED_SUBMODULE_COMMIT', pinned)
        patcher.start()
        self.addCleanup(patcher.stop)
        names = set(distribution.SUPPORT) | {str(distribution.PACKAGE / p) for p in gd.CORE_FILES}
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic source\n')
        (self.root / '.gitmodules').write_text('[submodule "llama.cpp"]\n path = llama.cpp\n url = https://example.invalid/synthetic\n')
        (self.root / '.gitignore').write_text('results/\nmodels/\nbuild/\n')
        self.git(self.root, 'add', '--', *sorted(names), '.gitmodules', '.gitignore')
        self.git(self.root, 'update-index', '--add', '--cacheinfo', f'160000,{pinned},llama.cpp')
        self.git(self.root, 'commit', '-qm', 'synthetic delivery')
        self.source = self.root / distribution.PACKAGE / 'runtime.py'

    def test_identity_snapshot_existing_and_fresh_checkout(self):
        report = gd.verify_checkout(self.root)
        self.assertIn('.gitignore', report['required_files'])
        self.assertEqual(self.git(self.root, 'ls-files', '--stage', '.gitignore').split()[0], '100644')
        self.assertFalse((self.root / 'conference_c03_results.zip').exists())
        self.assertFalse(any(name.endswith('.zip') for name in report['required_files']))
        self.assertEqual(report, gd.verify_checkout(self.root, report['commit_sha']))
        self.assertNotIn(str(self.root), json.dumps(report))
        destination = Path(self.tmp.name) / 'snapshot'
        manifest = gd.snapshot_source(destination, report, self.root)
        self.assertEqual(manifest['package_sha256'], report['source_sha256'])
        fresh = Path(self.tmp.name) / 'fresh'
        subprocess.check_call(['git', 'clone', '-q', '--no-local', str(self.root), str(fresh)])
        subprocess.check_call(['git', 'clone', '-q', '--no-local', str(self.root / 'llama.cpp'), str(fresh / 'llama.cpp')])
        self.assertEqual(report, gd.verify_checkout(fresh))
        with self.assertRaises(FileExistsError):
            gd.snapshot_source(destination, report, self.root)
        # Runtime marker modifications are checked later by runtime preflight.
        (self.root / 'llama.cpp' / 'synthetic').write_text('intentional marker patch\n')
        self.assertEqual(report, gd.verify_checkout(self.root))
        with self.assertRaises(ValueError):
            gd.verify_checkout(self.root, '0' * 40)

    def test_worktree_and_index_modifications_rejected(self):
        self.source.write_text('changed\n')
        with self.assertRaises(ValueError):
            gd.verify_checkout(self.root)
        self.git(self.root, 'add', str(self.source.relative_to(self.root)))
        with self.assertRaises(ValueError):
            gd.verify_checkout(self.root)
        self.source.write_text('synthetic source\n')
        with self.assertRaises(ValueError):
            gd.verify_checkout(self.root)

    def test_relevant_untracked_rejected_ignored_output_allowed(self):
        report = gd.verify_checkout(self.root)
        output = self.root / distribution.PACKAGE / 'results' / 'metrics.json'
        output.parent.mkdir()
        output.write_text('{}')
        self.assertEqual(report, gd.verify_checkout(self.root))
        extra = self.root / distribution.PACKAGE / 'new_science.py'
        extra.write_text('changed science\n')
        with self.assertRaises(ValueError):
            gd.verify_checkout(self.root)

    def test_missing_required_even_if_committed_rejected(self):
        self.git(self.root, 'rm', str(self.source.relative_to(self.root)))
        self.git(self.root, 'commit', '-qm', 'synthetic missing source')
        with self.assertRaises(ValueError):
            gd.verify_checkout(self.root)

    def test_index_removal_with_unchanged_worktree_rejected(self):
        self.git(self.root, 'rm', '--cached', str(self.source.relative_to(self.root)))
        self.assertTrue(self.source.is_file())
        with self.assertRaises(ValueError):
            gd.verify_checkout(self.root)

    def test_staged_mode_with_unchanged_worktree_rejected(self):
        self.git(self.root, 'update-index', '--chmod=+x', str(self.source.relative_to(self.root)))
        self.assertFalse(self.source.stat().st_mode & 0o100)
        with self.assertRaises(ValueError):
            gd.verify_checkout(self.root)

    def test_symlink_rejected(self):
        self.source.unlink()
        self.source.symlink_to(self.root / 'LICENSE')
        with self.assertRaises(ValueError):
            gd.verify_checkout(self.root)

    def test_mode_and_submodule_head_rejected(self):
        self.source.chmod(0o755)
        with self.assertRaises(ValueError):
            gd.verify_checkout(self.root)
        self.source.chmod(0o644)
        sub = self.root / 'llama.cpp'
        (sub / 'synthetic').write_text('different runtime\n')
        self.git(sub, 'add', 'synthetic')
        self.git(sub, 'commit', '-qm', 'synthetic wrong runtime')
        with self.assertRaises(ValueError):
            gd.verify_checkout(self.root)

    def test_synthetic_approval_bindings_and_legacy_rejection(self):
        report = gd.verify_checkout(self.root)
        plan = protocol.load_plan()
        approval = gd.approval_template(plan, report)
        self.assertFalse(approval['authorized'])
        with self.assertRaises(ValueError):
            gd.check_approval(plan, approval, report)
        approval.update(authorized=True, author_review_id='SYNTHETIC TEST ONLY')
        self.assertTrue(gd.check_approval(plan, approval, report))
        for key in ('git_commit', 'source_sha256', 'required_files_sha256', 'plan_sha256',
                    'maximum_session_s', 'schedule_sha256', 'duration_rule_sha256',
                    'runtime_fallback_policy_sha256'):
            changed = copy.deepcopy(approval)
            changed[key] = None
            with self.subTest(key=key), self.assertRaises(ValueError):
                gd.check_approval(plan, changed, report)
        approval['package_sha256'] = report['source_sha256']
        with self.assertRaises(ValueError):
            gd.check_approval(plan, approval, report)


if __name__ == '__main__':
    unittest.main()
