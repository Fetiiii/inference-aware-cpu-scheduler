import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from conference.journal_extension.amd_oneshot import preflight
from conference.journal_extension.amd_oneshot.preflight import _apply_frozen_patch


ROOT = Path(__file__).resolve().parents[4]
PINNED = '571d0d540df04f25298d0e159e520d9fc62ed121'


class PatchReconstructionTests(unittest.TestCase):
    def test_verify_build_real_pinned_tree_clean_patched_and_corruption(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            source = root / 'llama.cpp'
            subprocess.run(['git', 'clone', '--shared', '--no-checkout', '-q',
                            str(ROOT / 'llama.cpp'), str(source)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(source), 'checkout', '-q', '--detach', PINNED],
                           check=True, capture_output=True)
            context = source / 'src/llama-context.cpp'
            original = context.read_bytes()
            phase = root / 'conference/diagnostic/llama_cpp_phase_mark.patch'
            phase.parent.mkdir(parents=True)
            frozen = (ROOT / 'conference/diagnostic/llama_cpp_phase_mark.patch').read_bytes()
            phase.write_bytes(frozen)
            reconstructed = _apply_frozen_patch(original.decode(), frozen.decode()).encode()
            build = root / 'existing-build'
            binary = build / 'bin/llama-server'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'existing original binary fixture')
            binary.chmod(0o755)
            library = root / 'libllama.so'
            library.write_bytes(preflight.c03.MARKER)
            compiler = root / 'fixture-c++'
            compiler.write_bytes(b'compiler identity fixture')
            cache = dict(CMAKE_BUILD_TYPE='Release', GGML_NATIVE='OFF', GGML_AVX='ON',
                         GGML_BACKEND_DL='OFF', GGML_CPU_ALL_VARIANTS='OFF', GGML_BACKEND_DIR='',
                         GGML_AVX2='ON', GGML_FMA='ON', GGML_F16C='ON', GGML_AVX512='OFF',
                         GGML_OPENMP='ON', GGML_OPENMP_ENABLED='ON', BUILD_SHARED_LIBS='ON',
                         CMAKE_HOME_DIRECTORY=str(source), CMAKE_CXX_COMPILER=str(compiler))
            (build / 'CMakeCache.txt').write_text(''.join(f'{k}:STRING={v}\n' for k, v in cache.items()))
            (build / 'compile_commands.json').write_text(json.dumps([{'command': 'c++ -mavx2 -c fixture.cpp'}]))
            binary_hash = preflight.c03.sha256_file(binary)
            historical = {'llama_cpp_commit': PINNED, 'patch_sha256': preflight.PHASE_PATCH_SHA256,
                          'compiler_identity': 'historical compiler fixture',
                          'server_binary': {'sha256': binary_hash},
                          'phase_mark_capability': {'matched_file_identities': [
                              {'sha256': preflight.c03.sha256_file(library)}]}}
            config = {'repo_root': directory, 'server_bin': str(binary),
                      'historical_identity': {'diagnostic_build': historical}}
            stack.enter_context(patch.object(preflight, 'ORIGINAL_SERVER_SHA256', binary_hash))
            stack.enter_context(patch.object(preflight, '_runtime_closure', return_value=(
                [preflight.identity(library)], {'method': 'fixture static loader metadata'})))
            clean = preflight.verify_build(config)
            self.assertEqual(clean['source_state'], 'clean_pinned_base_binary_separately_anchored')
            self.assertEqual(clean['reconstructed_source_sha256'], hashlib.sha256(reconstructed).hexdigest())
            context.write_bytes(reconstructed)
            patched = preflight.verify_build(config)
            self.assertEqual(patched['source_state'], 'exact_frozen_phase_patch')
            self.assertEqual(clean['binary'], patched['binary'])
            phase.write_bytes(frozen + b'corrupt\n')
            with self.assertRaisesRegex(ValueError, 'patch SHA256 mismatch'):
                preflight.verify_build(config)
            phase.write_bytes(frozen)
            context.write_bytes(reconstructed + b'// unauthorized\n')
            with self.assertRaisesRegex(ValueError, 'source differs'):
                preflight.verify_build(config)
            context.write_bytes(original)
            other = source / 'CMakeLists.txt'
            other.write_bytes(other.read_bytes() + b'\n# unauthorized\n')
            with self.assertRaisesRegex(ValueError, 'source changes exceed'):
                preflight.verify_build(config)
            subprocess.run(['git', '-C', str(source), 'checkout', '--', 'CMakeLists.txt'],
                           check=True, capture_output=True)
            untracked = source / 'src/unauthorized.cpp'
            untracked.write_text('// untracked input\n')
            with self.assertRaisesRegex(ValueError, 'untracked source inputs'):
                preflight.verify_build(config)
            untracked.unlink()
            for field in ('llama_cpp_commit', 'patch_sha256'):
                saved = historical[field]
                historical[field] = 'contradictory historical identity'
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'historical .* mismatch'):
                    preflight.verify_build(config)
                historical[field] = saved
            historical_path = root / 'historical-preflight.json'
            historical_path.write_text(json.dumps({'diagnostic_build': {
                **historical, 'llama_cpp_commit': 'contradictory historical file'}}))
            file_config = {key: value for key, value in config.items() if key != 'historical_identity'}
            file_config['historical_preflight'] = str(historical_path)
            with self.assertRaisesRegex(ValueError, 'historical source commit mismatch'):
                preflight.verify_build(file_config)
            binary.write_bytes(b'substituted executable')
            with self.assertRaisesRegex(ValueError, 'ORIGINAL_BINARY_MISMATCH'):
                preflight.verify_build(config)

    def test_wrong_revision_rejected_before_reconstruction(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'server'
            binary.write_bytes(b'fixture')
            config = {'repo_root': directory, 'server_bin': str(binary)}
            with patch.object(preflight.c03, 'git_output', return_value='wrong-revision'), \
                    patch.object(preflight, '_apply_frozen_patch',
                                 side_effect=AssertionError('must not reconstruct')):
                with self.assertRaisesRegex(ValueError, 'source revision mismatch'):
                    preflight.verify_build(config)
                config['pinned_revision'] = 'substituted-pin'
                with self.assertRaisesRegex(ValueError, 'pin substitution forbidden'):
                    preflight.verify_build(config)

    def test_real_pinned_source_matches_isolated_git_apply(self):
        patch = (ROOT / 'conference/diagnostic/llama_cpp_phase_mark.patch').read_bytes()
        self.assertEqual(hashlib.sha256(patch).hexdigest(),
                         '722adc5c8d8805d442753971d2eabd488ab91906aa8fa93961d6a615194833ce')
        original = subprocess.run(
            ['git', '-C', str(ROOT / 'llama.cpp'), 'show', f'{PINNED}:src/llama-context.cpp'],
            check=True, capture_output=True).stdout
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', directory], check=True, capture_output=True)
            source = root / 'src/llama-context.cpp'
            source.parent.mkdir()
            source.write_bytes(original)
            subprocess.run(['git', '-C', directory, 'apply', '-'], input=patch,
                           check=True, capture_output=True)
            self.assertEqual(_apply_frozen_patch(original.decode(), patch.decode()).encode(),
                             source.read_bytes())

    def test_exact_and_bounded_offsets(self):
        original = 'zero\none\ntwo\nthree\nfour\nfive\nsix\nseven\neight\n'
        for declared in range(1, 8):
            with self.subTest(declared=declared):
                patch = f'@@ -{declared},2 +{declared},2 @@\n three\n-four\n+FOUR\n'
                self.assertEqual(_apply_frozen_patch(original, patch), original.replace('four', 'FOUR'))
        with self.assertRaises(ValueError):
            _apply_frozen_patch(original, '@@ -8,2 +8,2 @@\n three\n-four\n+FOUR\n')

    def test_corrupt_partial_ambiguous_and_empty_rejected(self):
        cases = [
            ('a\nb\n', ''),
            ('a\nb\n', '--- a/file\n+++ b/file\n'),
            ('a\nb\n', '@@ -1,2 +1,2 @@\n a\n-x\n+y\n'),
            ('a\nb\n', '@@ -1,3 +1,2 @@\n a\n-b\n+c\n'),
            ('a\nb\n', '@@ -1,2 +1,3 @@\n a\n-b\n+c\n'),
            ('a\na\n', '@@ -1 +1 @@\n-a\n+b\n'),
            ('a\nb\n', '@@ -1 +1 @@\n-a\n+A\n@@ broken\n'),
            ('a\nb\n', '@@ -1 +1 @@\n-a\n+A\ntrailing corruption\n'),
            ('a\nb\n', '@@ -1 +1 @@\n-a\n+A\ndiff --git a/other b/other\n'),
            ('a\nb\n', '@@ -1 +1 @@\n-a\n+A\n@@ -1 +1 @@\n-a\n+B\n'),
            ('a\nb\n', '@@ -1,0 +1 @@\n+x\n'),
        ]
        for original, patch in cases:
            with self.subTest(patch=patch):
                with self.assertRaises(ValueError):
                    _apply_frozen_patch(original, patch)


if __name__ == '__main__':
    unittest.main()
