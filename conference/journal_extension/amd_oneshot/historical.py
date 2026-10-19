"""Validate the comparability anchor from copied ORIGINAL C03 records only."""
from pathlib import Path
import json
from .common import sha256_file


def check_anchor(collected, plan):
    """Current defaults, new manifests and retrospective summaries are not proof."""
    collected = Path(collected)
    checked, errors = [], []
    expected = {k: plan['runtime'][k] for k in ('ctx', 'batch', 'ubatch', 'n_predict', 'seed')}
    expected.update(cooldown_s=plan['cooldown_s'], initial_cooldown_s=plan['initial_cooldown_s'])
    for path in sorted((collected / 'history').glob('c03_*/raw/environment/*.json')):
        record = json.loads(path.read_text())
        if record.get('task') != 'TASK-C03' or record.get('selected_c03_path') != 'CROSS_VENDOR':
            continue
        reasons = []
        for label in ('model', 'prompt'):
            if record.get(label, {}).get('sha256') != plan[f'{label}_sha256']:
                reasons.append(f'{label} identity mismatch or missing')
        llama = record.get('llama_cpp', {})
        if llama.get('repository_commit') != plan['source_revision']:
            reasons.append('historical source revision mismatch or missing')
        if not llama.get('diagnostic_server_binary', {}).get('sha256'):
            reasons.append('historical runtime binary identity missing')
        for key, value in expected.items():
            if record.get('protocol', {}).get(key) != value:
                reasons.append(f'historical workload protocol mismatch or missing: {key}')
        checked.append({'path': str(path.relative_to(collected)), 'sha256': sha256_file(path),
                        'errors': reasons, 'model': record.get('model'), 'prompt': record.get('prompt'),
                        'llama_cpp': llama, 'protocol': record.get('protocol')})
        errors.extend(reasons)
    if not checked:
        errors.append('No original C03 CROSS_VENDOR raw/environment record establishes prompt/model/runtime/workload identity. Current source defaults are not historical evidence.')
    return {'status': 'PASS' if checked and not errors else 'NOT_READY',
            'records': checked, 'errors': errors,
            'verified_workload_fields': sorted(expected),
            'scope': 'Anchor identity from original environment records, NOT a claim that all twelve historical raw runs have been recovered. Missing per-run roles remain in history_inventory.json.',
            'source_defined_options': 'One-token warmup, settle/preroll, temperature=0, cache_prompt=false and ignore_eos=true are frozen from the inspected historical C03 runner, not fields recorded in its original environment schema.',
            'semantic_marker': 'first internally marked unbatched decode computation',
            'missing_raw_archives_are_not_reconstructed': True}
