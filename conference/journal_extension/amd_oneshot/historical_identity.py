"""Public identity attestation, not collaborator-side verification of private raw.

The author verified the original archive offline. Git delivery carries only this
small attestation; it neither recovers nor claims to inspect that archive on the
collaborator machine. The private ZIP ingester remains a separate author tool.
"""
from copy import deepcopy
from pathlib import Path

from .common import load_json, save_json, sha256_file, stat_identity

IDENTITY_PATH = Path(__file__).with_name('HISTORICAL_IDENTITY.json')
ARCHIVE_SHA256 = '02bbc0f878667fb71f3cbf07b3367c648e152889ff27c3047050fd831744ab07'
SOURCE_REVISION = '571d0d540df04f25298d0e159e520d9fc62ed121'
MODEL_SHA256 = '03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8'
PROMPT_SHA256 = '27f3f8ba7b73d3df1337549237a8e9f4ccf57e0551908cc93ce8a183480751b8'
SERVER_SHA256 = 'a3d96e1df84b2c56a2c04f8253040fe4c88241c8193640128ff3b539d3cc11e1'
MARKER_SHA256 = '8fdf5c06a445b11dc2a2d9e4dafb94668198cd15a8e31eeeeed9be545719b97e'


def _expected():
    return {
        'schema': 'je-amd-public-historical-identity-v1',
        'status': 'AUTHOR_SIDE_VERIFIED_IDENTITY',
        'verification_scope': 'Author offline verified original private C03 archive; private raw NOT BUNDLED and NOT CHECKED on collaborator.',
        'private_raw_bundled': False,
        'collaborator_raw_verification': 'NOT_CHECKED',
        'author_offline_verification': {
            'archive_sha256': ARCHIVE_SHA256, 'crc_verified': True,
            'archive_members': 84, 'valid_runs': 12, 'unique_environment_records': 2,
            'run_roles_verified': ['run', 'detector', 'phase', 'server', 'environment'],
            'rounds': [1, 2, 3, 4, 5, 6], 'arms': ['BIG_ONLY', 'ALL_CORES']},
        'source_revision': SOURCE_REVISION,
        'model_sha256': MODEL_SHA256, 'prompt_sha256': PROMPT_SHA256,
        'workload': {'ctx': 2048, 'batch': 2048, 'ubatch': 512,
                     'n_predict': 256, 'seed': 42, 'cooldown_s': 30,
                     'initial_cooldown_s': 30},
        'diagnostic_build': {
            'llama_cpp_commit': SOURCE_REVISION,
            'patch_sha256': '722adc5c8d8805d442753971d2eabd488ab91906aa8fa93961d6a615194833ce',
            'server_binary': {'sha256': SERVER_SHA256},
            'compiler_identity': {'returncode': 0, 'stdout': 'c++ (Ubuntu 15.2.0-16ubuntu1) 15.2.0',
                                  'capture': 'Normalized historical first version line; not current compiler evidence.'},
            'phase_mark_capability': {
                'supported': True, 'format': 'PHASE_MARK batched=%d t_mono_ns=%lld',
                'matched_file_identities': [{'sha256': MARKER_SHA256}]}}}


def load_identity(plan):
    """Validate the tracked public schema and all frozen known identity anchors."""
    if IDENTITY_PATH.is_symlink() or not IDENTITY_PATH.is_file():
        raise ValueError('public historical identity missing or unsafe')
    manifest = load_json(IDENTITY_PATH)
    # Exact schema/content also prevents accidental additions of private paths,
    # raw logs, or an assertion of collaborator-side archive verification.
    if manifest != _expected():
        raise ValueError('public historical identity schema or frozen anchors mismatch')
    for key, value in (('historical_archive_sha256', ARCHIVE_SHA256),
                       ('historical_binary_sha256', SERVER_SHA256),
                       ('source_revision', SOURCE_REVISION),
                       ('model_sha256', MODEL_SHA256), ('prompt_sha256', PROMPT_SHA256)):
        if plan.get(key) != value:
            raise ValueError(f'public historical identity plan mismatch: {key}')
    workload = {key: plan['runtime'].get(key) for key in
                ('ctx', 'batch', 'ubatch', 'n_predict', 'seed')}
    workload.update({key: plan.get(key) for key in ('cooldown_s', 'initial_cooldown_s')})
    if workload != manifest['workload']:
        raise ValueError('public historical identity workload mismatch')
    if plan['runtime_fallback_policy'].get('phase_patch_sha256') != manifest['diagnostic_build']['patch_sha256']:
        raise ValueError('public historical identity phase patch mismatch')
    return {'manifest': manifest, 'diagnostic_build': deepcopy(manifest['diagnostic_build'])}


def _session_record(loaded):
    return {'schema': 'je-amd-session-historical-identity-v1',
            'status': 'AUTHOR_SIDE_VERIFIED_IDENTITY', 'private_raw_present': False,
            'collaborator_raw_verification': 'NOT_CHECKED',
            'source_sha256': sha256_file(IDENTITY_PATH),
            'diagnostic_build': deepcopy(loaded['diagnostic_build'])}


def stage_identity(session, plan):
    """Exclusively stage the public attestation and its honest session record."""
    loaded = load_identity(plan)
    session = Path(session)
    record = _session_record(loaded)
    for name in ('historical_identity_source.json', 'historical_identity.json'):
        if (session / name).exists() or (session / name).is_symlink():
            raise ValueError('historical identity destination already exists')
    # Copy exact bytes so source_sha256 identifies the tracked file itself.
    with (session / 'historical_identity_source.json').open('xb') as stream:
        stream.write(IDENTITY_PATH.read_bytes())
    save_json(session / 'historical_identity.json', record)
    return {**loaded, 'session_identity': record}


def verify_session_identity(session, plan):
    """Recheck staged identities against tracked source, without private raw."""
    loaded = load_identity(plan)
    session = Path(session)
    source = session / 'historical_identity_source.json'
    record_path = session / 'historical_identity.json'
    for path in (source, record_path):
        if path.is_symlink() or not path.is_file():
            raise ValueError('session historical identity missing or unsafe')
    if sha256_file(source) != sha256_file(IDENTITY_PATH):
        raise ValueError('session public historical identity source changed')
    record = _session_record(loaded)
    if load_json(record_path) != record:
        raise ValueError('session historical identity changed')
    return {**loaded, 'session_identity': record,
            'guards': {str(path): stat_identity(path) for path in (source, record_path, IDENTITY_PATH)}}
