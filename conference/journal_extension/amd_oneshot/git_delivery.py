"""Offline Git-native source identity and separately supplied execution approval."""
from pathlib import Path
import hashlib
import os
import re
import stat
import subprocess

from . import distribution
from .common import digest

PINNED_SUBMODULE_COMMIT = '571d0d540df04f25298d0e159e520d9fc62ed121'
SUFFIXES = {'.py', '.md', '.json', '.sh'}
# These dependencies cannot disappear merely by deleting them in a later commit.
CORE_FILES = ('__init__.py', 'analysis.py', 'archive.py', 'calibration.py', 'cli.py',
              'common.py', 'coordinator.py', 'distribution.py', 'entrypoint.py',
              'git_delivery.py', 'historical.py', 'historical_zip.py', 'historical_identity.py', 'preflight.py',
              'protocol.py', 'runtime.py', 'runtime_fallback.py', 'RUN_PLAN.json',
              'amd_oneshot.sh', 'HISTORICAL_IDENTITY.json', 'GIT_DELIVERY_TR.md',
              'COLLECTION_MANIFEST_SPEC.md', 'COLLABORATOR_README_TR.md',
              'EXECUTION_APPROVAL.example.json', 'EXPECTED_DURATION.md',
              'FAILURE_AND_RESUME_POLICY.md', 'HISTORICAL_RECOVERY_REPORT.json',
              'HISTORICAL_SIGNAL_REVIEW.json', 'LOCAL_EVIDENCE_GAPS.md',
              'PREFLIGHT_REQUIREMENTS.md', 'PREP_FINAL_CHANGE_REPORT.md',
              'PREP_R_CHANGE_REPORT.md', 'PREP_VALIDATION.md', 'PROVENANCE_POLICY.md',
              'READY_TO_SEND_MESSAGE_TR.md', 'RESEARCH_PROTOCOL.md', 'SOURCE_AUDIT.md',
              'tests/test_archive.py', 'tests/test_calibration.py',
              'tests/test_coordinator.py', 'tests/test_duration.py',
              'tests/test_final_cli.py', 'tests/test_git_delivery.py',
              'tests/test_historical_zip.py', 'tests/test_integration.py',
              'tests/test_preflight.py', 'tests/test_runtime.py',
              'tests/test_runtime_fallback.py')


def _git(root, *args):
    # Do not inherit alternate repositories, object stores, indexes, or replacements.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_NO_REPLACE_OBJECTS='1', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(root), *args],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise ValueError('Git source verification failed')
    return result.stdout


def _relevant(name):
    path = Path(name)
    return (path.is_relative_to(distribution.PACKAGE) and path.suffix in SUFFIXES
            and not set(path.parts) & {'__pycache__', 'build', 'models', 'results', '.pytest_cache'})


def _entries(raw, tree=False):
    result = {}
    for item in raw.split(b'\0'):
        if not item:
            continue
        metadata, name = item.split(b'\t', 1)
        fields = metadata.decode().split()
        result[name.decode('utf-8')] = (fields[0], fields[2] if tree else fields[1])
        if not tree and fields[2] != '0':
            raise ValueError('Unmerged source index')
    return result


def verify_checkout(root=distribution.CODE_ROOT, expected_commit=None):
    root = Path(root)
    if _git(root, 'rev-parse', '--show-prefix').strip():
        raise ValueError('Source root must be the Git checkout root')
    commit = _git(root, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
    if not re.fullmatch('[0-9a-f]{40}', commit) or (expected_commit is not None and commit != expected_commit):
        raise ValueError('Git commit identity mismatch')
    tree = _entries(_git(root, 'ls-tree', '-rz', '--full-tree', 'HEAD'), tree=True)
    index = _entries(_git(root, 'ls-files', '--stage', '-z'))
    required = set(distribution.SUPPORT) | {'.gitmodules', '.gitignore'}
    required.update(str(distribution.PACKAGE / name) for name in CORE_FILES)
    required.update(name for name in tree if _relevant(name))
    required.update(name for name in index if _relevant(name))
    package = root / distribution.PACKAGE
    if package.is_symlink():
        raise ValueError('Symlink source namespace')
    required.update(str(p.relative_to(root)) for p in package.rglob('*') if _relevant(str(p.relative_to(root))) and not p.is_dir())
    files = {}
    for name in sorted(required):
        entry = tree.get(name)
        if entry is None or entry[0] not in ('100644', '100755') or index.get(name) != entry:
            raise ValueError(f'Source is missing, untracked, staged, or nonregular: {name}')
        path = root / name
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root.parent):
            raise ValueError(f'Symlink source: {name}')
        if not path.is_file():
            raise ValueError(f'Missing source: {name}')
        mode = '100755' if path.stat().st_mode & stat.S_IXUSR else '100644'
        content = path.read_bytes()
        if mode != entry[0] or content != _git(root, 'cat-file', 'blob', entry[1]):
            raise ValueError(f'Modified source: {name}')
        files[name] = hashlib.sha256(content).hexdigest()
    if tree.get('llama.cpp') != ('160000', PINNED_SUBMODULE_COMMIT) or index.get('llama.cpp') != tree['llama.cpp']:
        raise ValueError('Pinned runtime gitlink mismatch')
    modules = _git(root, 'config', '--blob', 'HEAD:.gitmodules', '--get', 'submodule.llama.cpp.path').decode().strip()
    if modules != 'llama.cpp' or (root / 'llama.cpp').is_symlink():
        raise ValueError('Pinned runtime submodule path mismatch')
    if _git(root / 'llama.cpp', 'rev-parse', 'HEAD').decode().strip() != PINNED_SUBMODULE_COMMIT:
        raise ValueError('Runtime submodule HEAD mismatch')
    return {'schema': 'git-native-source-v1', 'commit_sha': commit,
            'git_tree': _git(root, 'rev-parse', 'HEAD^{tree}').decode().strip(),
            'pinned_submodule_commit': PINNED_SUBMODULE_COMMIT,
            'required_files': files, 'source_sha256': digest(files)}


def snapshot_source(destination, report, root=distribution.CODE_ROOT):
    if verify_checkout(root, report['commit_sha']) != report:
        raise ValueError('Source report changed before snapshot')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for name, checksum in report['required_files'].items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        content = (Path(root) / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != checksum:
            raise ValueError('Source changed during snapshot')
        with target.open('xb') as handle:
            handle.write(content)
    if verify_checkout(root, report['commit_sha']) != report:
        raise ValueError('Source changed during snapshot')
    return {'identity_kind': 'git', 'git_commit': report['commit_sha'],
            'package_sha256': report['source_sha256'], 'files': report['required_files'],
            'git_source': report}


def approval_template(plan, report):
    from .protocol import approval_bindings
    return {'schema': 'je-amd-git-approval-v1', 'authorized': False,
            'author_review_id': '', 'git_commit': report['commit_sha'],
            'source_sha256': report['source_sha256'],
            'required_files_sha256': digest(report['required_files']),
            **approval_bindings(plan), 'runtime_fallback_authorized': False}


def check_approval(plan, approval, report):
    expected = approval_template(plan, report)
    if (approval.get('schema') != expected['schema'] or approval.get('authorized') is not True
            or not isinstance(approval.get('author_review_id'), str)
            or not approval['author_review_id'].strip() or 'package_sha256' in approval):
        raise ValueError('Separate Git-bound author execution approval is required')
    for key, value in expected.items():
        if key not in ('authorized', 'author_review_id', 'runtime_fallback_authorized') and approval.get(key) != value:
            raise ValueError(f'Git execution approval binding mismatch: {key}')
    fallback = approval.get('runtime_fallback_authorized', False)
    if (type(fallback) is not bool or (fallback and plan['runtime_fallback_policy'].get('schema') != 'je-amd-offline-runtime-fallback-v1')
            or approval.get('selected_tier', 'AUTO') != 'AUTO'):
        raise ValueError('Invalid fallback or tier authorization')
    return True
