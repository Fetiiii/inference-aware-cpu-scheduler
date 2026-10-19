"""Legacy author-side private packaging; Git execution uses git_delivery.

These exports retain checksum/recovery tests and optional author workflows.
They are not a collaborator runtime dependency or Git execution authorization.
No downloads, builds or execution occur here.
"""
from pathlib import Path
import hashlib
import json
import shutil
import zipfile

from .common import digest, sha256_file

CODE_ROOT = Path(__file__).resolve().parents[3]
PACKAGE = Path('conference/journal_extension/amd_oneshot')
SUPPORT = (
    'conference/experiments/c01_stock_scheduler.py',
    'conference/experiments/c02_external_oracle.py',
    'conference/tools/c03_amd_preflight.py',
    'conference/analysis/stats.py',
    'conference/diagnostic/llama_cpp_phase_mark.patch',
    'harness/bench_lib.py', 'harness/run_once.py',
    'harness/thread_residency.py', 'harness/phase_switch.py',
    'harness/prompt_512.txt', 'LICENSE',
)


def package_files(root=CODE_ROOT):
    root = Path(root)
    files = {name: root / name for name in SUPPORT}
    for path in sorted((root / PACKAGE).rglob('*')):
        if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts:
            if path.suffix in ('.py', '.md', '.json', '.sh'):
                files[str(path.relative_to(root))] = path
    launcher = root / PACKAGE / 'amd_oneshot.sh'
    files['amd_oneshot.sh'] = launcher
    for name, path in files.items():
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'Missing/nonregular portable dependency: {name}')
    return files


def package_manifest(root=CODE_ROOT):
    return {name: sha256_file(path) for name, path in sorted(package_files(root).items())}


def package_fingerprint(root=CODE_ROOT):
    return digest(package_manifest(root))


def snapshot_source(destination, root=CODE_ROOT):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    before = package_manifest(root)
    for name, source in package_files(root).items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if sha256_file(target) != before[name] or sha256_file(source) != before[name]:
            raise ValueError(f'Package changed during snapshot: {name}')
    return {'package_sha256': digest(before), 'files': before}


def validate_prep_zip(path):
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f'ZIP CRC mismatch: {bad}')
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate ZIP members')
        sums = json.loads(archive.read('SOURCE_MANIFEST.json'))
        if set(names) != set(sums) | {'SOURCE_MANIFEST.json', 'SHA256SUMS'}:
            raise ValueError('ZIP inventory mismatch')
        expected = ''.join(f'{value}  {name}\n' for name, value in sorted(sums.items()))
        if archive.read('SHA256SUMS').decode() != expected:
            raise ValueError('SHA256SUMS inventory mismatch')
        for name, sha in sums.items():
            if Path(name).is_absolute() or '..' in Path(name).parts:
                raise ValueError('Unsafe archive member')
            if hashlib.sha256(archive.read(name)).hexdigest() != sha:
                raise ValueError(f'ZIP SHA256 mismatch: {name}')
    return {'status': 'PASS', 'files': len(sums), 'sha256': sha256_file(path)}


def build_prep_zip(target, root=CODE_ROOT, historical_zip=None):
    target = Path(target)
    files, before = package_files(root), package_manifest(root)
    if historical_zip is not None:
        from .historical_zip import EXPECTED_SHA256
        historical_zip = Path(historical_zip)
        if not historical_zip.is_absolute() or historical_zip.is_symlink() or sha256_file(historical_zip) != EXPECTED_SHA256:
            raise ValueError('Explicit historical ZIP identity mismatch')
        # Private delivery payload is pinned by the plan and content manifest;
        # it is not confused with the source-only approval fingerprint.
        files['private_history/conference_c03_results.zip'] = historical_zip
        before['private_history/conference_c03_results.zip'] = EXPECTED_SHA256
    # Derived unsigned review bindings are outside the source-only fingerprint
    # to avoid a self-referential hash. They are covered by ZIP member hashes.
    from .cli import review_template
    from .protocol import load_plan
    template = (json.dumps(review_template(load_plan(Path(root) / PACKAGE / 'RUN_PLAN.json'),
                                         package_fingerprint(root)), indent=2, sort_keys=True) + '\n').encode()
    template_name = 'execution_approval.template.json'
    before[template_name] = hashlib.sha256(template).hexdigest()
    with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name, source in sorted(files.items()):
            archive.write(source, name, compress_type=zipfile.ZIP_STORED if name.startswith('private_history/') else zipfile.ZIP_DEFLATED)
        archive.writestr(template_name, template)
        archive.writestr('SOURCE_MANIFEST.json', json.dumps(before, indent=2, sort_keys=True) + '\n')
        archive.writestr('SHA256SUMS', ''.join(f'{value}  {name}\n' for name, value in sorted(before.items())))
    after = package_manifest(root)
    after[template_name] = hashlib.sha256(template).hexdigest()
    if historical_zip is not None:
        after['private_history/conference_c03_results.zip'] = sha256_file(historical_zip)
    if after != before:
        raise ValueError('Sources changed while packaging; preserve failed ZIP for review')
    report = validate_prep_zip(target)
    with target.with_suffix(target.suffix + '.sha256').open('x') as handle:
        handle.write(f'{report["sha256"]}  {target.name}\n')
    return report
