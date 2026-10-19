"""Only temporary Git fixtures are staged/committed by these tests."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from conference.journal_extension.amd_oneshot import publication


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-publication-')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic Test')
        self.git('config', 'user.email', 'synthetic@example.invalid')
        self.write('README.md', 'synthetic baseline\n')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'synthetic baseline')
        self.base = self.git('rev-parse', 'HEAD').decode().strip()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def write(self, name, data):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())

    def stage(self, name, text):
        self.write(name, text)
        self.git('add', '--', name)

    def categories(self, report):
        return {item['category'] for item in report['findings']}

    def test_safe_explicit_public_files_and_sha_are_allowed(self):
        self.stage('public/module.py', "path='/path/to/runtime'\nroot='/synthetic'\nsha='" + 'a' * 64 + "'\nfixture={'authorized': True}\n")
        report = publication.audit_staged(self.repo, ['public/module.py'])
        self.assertEqual(report['status'], 'PASS', report)
        self.assertEqual(report['files_checked'], 1)

    def test_allowlist_is_required_and_does_not_stage_anything(self):
        self.stage('safe.md', 'public\n')
        before = self.git('diff', '--cached', '--raw')
        report = publication.audit_staged(self.repo, [])
        self.assertIn('outside_allowlist', self.categories(report))
        self.assertEqual(before, self.git('diff', '--cached', '--raw'))

    def test_private_paths_and_artifacts_are_rejected(self):
        names = ['private_history/archive.md', 'results/run.json', 'raw/run.json',
                 'environment/capture.json', 'models/model.gguf', 'build-dir/helper.py',
                 'execution_approval.json', 'data.zip']
        for name in names:
            self.stage(name, '{}')
        report = publication.audit_staged(self.repo, names)
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual({f['path'] for f in report['findings']}, set(names))

    def test_authorization_json_true_rejected_but_false_example_allowed(self):
        name = 'public/EXECUTION_APPROVAL.example.json'
        self.stage(name, json.dumps({'authorized': False}))
        self.assertEqual(publication.audit_staged(self.repo, [name])['status'], 'PASS')
        self.stage(name, json.dumps({'authorized': True}))
        self.assertIn('real_execution_approval', self.categories(publication.audit_staged(self.repo, [name])))
        self.stage(name, '{}')
        self.assertIn('approval_example_must_be_false', self.categories(publication.audit_staged(self.repo, [name])))

    def test_personal_home_and_credentials_report_no_values(self):
        token = 'gh' + 'p_' + 'X' * 36
        home = '/' + 'home/' + 'synthetic_person/runtime'
        self.stage('unsafe.md', 'first line\n' + home + '\n' + token + '\n')
        report = publication.audit_staged(self.repo, ['unsafe.md'])
        self.assertIn('personal_home', self.categories(report))
        self.assertIn('github_token', self.categories(report))
        encoded = json.dumps(report)
        self.assertNotIn(token, encoded)
        self.assertNotIn(home, encoded)
        self.assertTrue(all(set(f) == {'path', 'category', 'line'} for f in report['findings']))

    def test_token_and_key_families(self):
        payloads = [('huggingface_token', 'h' + 'f_' + 'a' * 30),
                    ('aws_access_key', 'AK' + 'IA' + 'A' * 16),
                    ('private_key', '-----BEGIN ' + 'RSA PRIVATE KEY-----'),
                    ('credential_assignment', 'api_key=' + 'x' * 24),
                    ('private_host_target', 's' + 'sh ' + 'person' + '@' + 'machine.invalid')]
        for category, text in payloads:
            with self.subTest(category=category):
                self.stage('unsafe.md', text)
                self.assertIn(category, self.categories(publication.audit_staged(self.repo, ['unsafe.md'])))

    def test_binary_nonutf8_oversize_and_symlink_rejected(self):
        self.stage('binary.py', b'hello\x00world')
        self.stage('invalid.md', b'\xff')
        self.stage('large.md', b'x' * (publication.MAX_BYTES + 1))
        (self.repo / 'link.py').symlink_to('README.md')
        self.git('add', 'link.py')
        report = publication.audit_staged(self.repo, ['binary.py', 'invalid.md', 'large.md', 'link.py'])
        self.assertTrue({'binary_content', 'non_utf8_content', 'oversized_blob', 'nonregular_blob'} <= self.categories(report))

    def test_staged_deletion_is_allowed_only_when_listed(self):
        self.git('rm', '-q', 'README.md')
        self.assertEqual(publication.audit_staged(self.repo, ['README.md'])['status'], 'PASS')
        self.assertIn('outside_allowlist', self.categories(publication.audit_staged(self.repo, [])))

    def test_audit_reads_index_blob_instead_of_unstaged_worktree(self):
        self.stage('public.md', 'h' + 'f_' + 'a' * 30)
        self.write('public.md', 'safe unstaged replacement')
        self.assertIn('huggingface_token', self.categories(publication.audit_staged(self.repo, ['public.md'])))

    def test_history_checks_transient_secret_deleted_before_head(self):
        token = 'h' + 'f_' + 'a' * 30
        self.stage('temporary.md', token)
        self.git('commit', '-qm', 'synthetic added')
        self.git('rm', '-q', 'temporary.md')
        self.git('commit', '-qm', 'synthetic removed')
        report = publication.audit_history(self.repo, self.base, 'HEAD')
        self.assertIn('huggingface_token', self.categories(report))
        self.assertNotIn(token, json.dumps(report))

    def test_history_safe_change_passes_and_unknown_ref_fails(self):
        self.stage('public.md', 'public source')
        self.git('commit', '-qm', 'synthetic public')
        self.assertEqual(publication.audit_history(self.repo, self.base, 'HEAD')['status'], 'PASS')
        self.assertEqual(publication.audit_history(self.repo, 'missing-ref', 'HEAD')['status'], 'FAIL')

    def test_policy_source_does_not_flag_its_own_regex_or_tests(self):
        paths = [Path(publication.__file__), Path(__file__)]
        for source in paths:
            name = 'public/' + source.name
            self.stage(name, source.read_bytes())
        report = publication.audit_staged(self.repo, ['public/' + p.name for p in paths])
        self.assertEqual(report['status'], 'PASS', report)

    def test_project_gitignore_covers_private_delivery_paths(self):
        root = Path(publication.__file__).resolve().parents[3]
        self.write('.gitignore', (root / '.gitignore').read_bytes())
        private = ['models/model.gguf', 'llama.cpp/build/output.o', 'llama.cpp/build-diag/output.o',
                   'private_history/original.zip', 'execution_approval.json',
                   '.je_amd_authorization/execution_approval.json',
                   'results/journal_extension_amd_synthetic/run.json',
                   'results/.je_amd_runtime_synthetic/manifest.json']
        result = subprocess.run(['git', '-C', str(self.repo), 'check-ignore', '--no-index', '--stdin'],
                                input=('\n'.join(private) + '\n').encode(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=True)
        self.assertEqual(set(result.stdout.decode().splitlines()), set(private))
