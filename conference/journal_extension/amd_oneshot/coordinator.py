"""Sequential, fail-closed session lifecycle. No inference simulation CLI exists.

``deps`` is a Python-only injection boundary for synthetic tests. Production
callers supply frozen session documents and a separately signed-off approval.
"""
from copy import deepcopy
import fcntl
import math
from pathlib import Path
import time
import uuid

from . import analysis, archive, calibration, preflight, protocol, runtime
from .common import append_event, digest, ledger, load_json, save_json, sha256_file, stat_identity


class SessionStop(Exception):
    pass


def package_hash(config):
    if config.get('identity_kind') == 'git':
        from .git_delivery import verify_checkout
        return verify_checkout(root=Path(config['repo_root']),
                               expected_commit=config['git_commit'])['source_sha256']
    from .distribution import package_fingerprint
    return package_fingerprint()


def _attempt_snapshot(path):
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_dir():
        raise SessionStop('invalid attempt directory')
    snapshot = {}
    for child in sorted(path.rglob('*')):
        if child.is_symlink() or not (child.is_file() or child.is_dir()):
            raise SessionStop('unsafe attempt artifact')
        if child.is_file():
            snapshot[str(child.relative_to(path))] = sha256_file(child)
        else:
            snapshot[str(child.relative_to(path)) + '/'] = None
    return snapshot


def _validate_config(config, plan):
    expected = {**plan['runtime'], **plan['safety'],
                'interval_ms': plan['calibration']['interval_ms'],
                'pinned_revision': plan['source_revision'],
                'expected_model_sha256': plan['model_sha256'],
                'expected_prompt_sha256': plan['prompt_sha256']}
    if any(config.get(k) != v for k, v in expected.items()):
        raise SessionStop('config runtime/safety differs from frozen plan')
    # These knobs would weaken machine gates or change the source identity.
    forbidden = {'live_safety_check', 'expected_workload_pids', 'max_load_average',
                 'heavy_process_cpu_percent', 'threads', 'threads_batch', 'port'}
    if forbidden.intersection(config):
        raise SessionStop('unapproved config override')


def _verify_manifest_files(root, files, exact=False):
    if not files or root.is_symlink() or not root.is_dir():
        raise SessionStop('missing or unsafe evidence manifest root')
    guards = {}
    if exact:
        observed = _attempt_snapshot(root)
        if {k: v for k, v in observed.items() if not k.endswith('/')} != files:
            raise SessionStop('source snapshot file census/hash mismatch')
    for name, expected in files.items():
        relative = Path(name)
        path = root / relative
        if relative.is_absolute() or '..' in relative.parts or path.is_symlink():
            raise SessionStop('unsafe evidence manifest path')
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise SessionStop('missing or escaped evidence manifest file')
        if sha256_file(path) != expected:
            raise SessionStop('evidence manifest hash mismatch')
        guards[str(path)] = stat_identity(path)
    return guards


def _verify_identities(config, report):
    """Re-hash the large model once per segment; retain cheap guards thereafter."""
    guards = {}
    for name in ('model', 'prompt'):
        path = Path(config[name])
        expected = report['identities'][name]
        if sha256_file(path) != expected['sha256']:
            raise SessionStop(f'{name} identity changed')
        guards[str(path)] = stat_identity(path)
    current = preflight.verify_build(config)
    if digest(current) != digest(report['source_fingerprint']):
        raise SessionStop('source/build identity changed')
    def collect(value):
        if isinstance(value, dict):
            path = value.get('resolved_path', value.get('path'))
            if path and value.get('sha256'):
                guards[str(path)] = stat_identity(path)
            for key, v in value.items():
                # Historical records may contain relocated/redacted paths. Their
                # copied evidence is hash-guarded separately; never stat them as
                # if they were the CURRENT resolved runtime closure.
                if key not in ('historical_known_identities', 'historical_compiler_identity'):
                    collect(v)
        elif isinstance(value, list):
            for v in value:
                collect(v)
    collect(current)
    return guards


def execute_session(session_dir, deps=None):
    """Run/resume approved whole cohorts, returning status and verified archive.

    Interrupts between requests are resumable. An unfinished attempt, terminal
    failure, or interruption inside a backend call requires author review.
    Every invocation produces its own analysis and external archive, even stops.
    """
    deps = deps or {}
    session = Path(session_dir).resolve()
    clock = deps.get('clock', time.monotonic)
    wall_clock = deps.get('wall_clock', time.time_ns)
    sleep = deps.get('sleep', time.sleep)
    backend = deps.get('backend', runtime.run_request)
    machine = deps.get('machine_check', preflight.check_machine_state)
    archive_builder = deps.get('archive_builder', archive.archive_session)
    analyzer = deps.get('analyzer', analysis.analyze)
    segment = uuid.uuid4().hex
    result = {'status': 'STOPPED', 'final_archive': None}
    records, gate = [], {'status': 'NOT_EVALUATED'}
    active, began, lock_acquired = False, False, False
    start = clock()
    lock = (session / '.lock').open('a+')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_acquired = True
        except BlockingIOError:
            return {**result, 'reason': 'session is locked'}
        plan = protocol.load_plan(session / 'plan.json')
        tier = plan.get('selected_tier')
        if tier != 'AUTO':
            raise SessionStop('unknown pre-execution tier')
        if plan != protocol.load_plan():
            raise SessionStop('session plan differs from packaged frozen plan')
        config = load_json(session / 'config.json')
        report = load_json(session / 'preflight.json')
        approval = load_json(session / 'execution_authorized.json')
        current_hash = deps.get('package_hash', package_hash)(config)
        git_report = None
        if config.get('identity_kind') == 'git':
            from .git_delivery import verify_checkout
            git_report = verify_checkout(root=Path(config['repo_root']), expected_commit=config['git_commit'])
            protocol.check_approval(plan, approval, current_hash, git_report=git_report)
        else:
            protocol.check_approval(plan, approval, current_hash)
        if config.get('package_sha256') != current_hash:
            raise SessionStop('config package identity changed')
        _validate_config(config, plan)
        runtime_evidence_guards = {}
        if (session / 'runtime_selection.json').exists():
            runtime_selection = load_json(session / 'runtime_selection.json')
            expected_selection = {
                'original_config_sha256': sha256_file(session / 'config.json'),
                'original_preflight_sha256': sha256_file(session / 'preflight.json'),
                'execution_config_sha256': sha256_file(session / 'execution_config.json'),
                'execution_preflight_sha256': sha256_file(session / 'execution_preflight.json'),
                'approval_sha256': sha256_file(session / 'execution_authorized.json'),
                'runtime_fallback_policy_sha256': digest(plan['runtime_fallback_policy'])}
            if (approval.get('runtime_fallback_authorized') is not True
                    or report.get('status') != 'RUNTIME_FALLBACK_PENDING'
                    or runtime_selection.get('status') != 'SELECTED'
                    or any(runtime_selection.get(k) != v for k, v in expected_selection.items())):
                raise SessionStop('invalid or unauthorized runtime execution overlay')
            config = load_json(session / 'execution_config.json')
            report = load_json(session / 'execution_preflight.json')
            build_copy = session / 'runtime_fallback_build.json'
            expected_build_hash = config.get('expected_runtime_fallback_manifest_sha256')
            if (not expected_build_hash or build_copy.is_symlink() or not build_copy.is_file()
                    or sha256_file(build_copy) != expected_build_hash
                    or runtime_selection.get('runtime_build_sha256') != expected_build_hash):
                raise SessionStop('runtime fallback archived build manifest identity mismatch')
            source_manifest_path = Path(config.get('runtime_fallback_manifest', ''))
            if not source_manifest_path.is_absolute():
                raise SessionStop('runtime fallback authoritative manifest requires absolute path')
            runtime_evidence_guards.update(_verify_manifest_files(source_manifest_path.parent,
                {source_manifest_path.name: expected_build_hash}))
            runtime_evidence_guards.update(_verify_manifest_files(session,
                {'runtime_fallback_build.json': expected_build_hash}))
            diagnostic_hashes = runtime_selection.get('diagnostics_sha256')
            if (not isinstance(diagnostic_hashes, dict) or not diagnostic_hashes
                    or any(Path(name).name != name or name in ('.', '..') for name in diagnostic_hashes)):
                raise SessionStop('runtime fallback diagnostics manifest missing or unsafe')
            runtime_evidence_guards.update(_verify_manifest_files(
                session / 'runtime_fallback_diagnostics', diagnostic_hashes, exact=True))
        elif any((session / name).exists() for name in ('execution_config.json', 'execution_preflight.json')):
            raise SessionStop('unbound runtime execution overlay')
        if config.get('package_sha256') != current_hash:
            raise SessionStop('config package identity changed')
        _validate_config(config, plan)
        source_manifest = load_json(session / 'source_manifest.json')
        if git_report is not None:
            from .historical_identity import verify_session_identity
            identity = verify_session_identity(session, plan)
            if (source_manifest.get('git_source') != git_report
                    or source_manifest.get('package_sha256') != current_hash
                    or source_manifest.get('files') != git_report['required_files']
                    or load_json(session / 'git_provenance.json') != git_report):
                raise SessionStop('Git source snapshot identity mismatch')
            if config.get('historical_identity') != identity['diagnostic_build']:
                raise SessionStop('config public historical build anchor changed')
            evidence_guards = _verify_manifest_files(session / 'source', source_manifest['files'], exact=True)
            # Guard both the actual checkout imported by this process and its
            # archived copy. A copied snapshot alone cannot detect live edits.
            evidence_guards.update(_verify_manifest_files(Path(config['repo_root']), git_report['required_files']))
            evidence_guards.update(identity['guards'])
        else:
            historical = load_json(session / 'historical_identity.json')
            collection = load_json(session / 'historical_collection.json')
            inventory = load_json(session / 'historical_c03' / 'history_inventory.json')
            if historical.get('status') != 'PASS':
                raise SessionStop('historical identity is not PASS')
            if source_manifest['package_sha256'] != current_hash or digest(source_manifest['files']) != current_hash:
                raise SessionStop('source snapshot package mismatch')
            evidence_guards = _verify_manifest_files(session / 'source', source_manifest['files'], exact=True)
            if collection != inventory:
                raise SessionStop('historical inventory differs from collection')
            evidence_guards.update(_verify_manifest_files(session / 'historical_c03',
                {row['path']: row['sha256'] for row in inventory['inventory']}))
            evidence_guards.update(_verify_manifest_files(session / 'historical_c03',
                {row['path']: row['sha256'] for row in historical['records']}))
        evidence_guards.update(runtime_evidence_guards)
        if report['status'] != 'PASS':
            raise SessionStop('preflight is not PASS')
        if config.get('plan') != plan:
            raise SessionStop('config plan differs from frozen plan')
        events = ledger(session)
        completed = [e['run_id'] for e in events if e['kind'] == 'request_completed']
        if len(completed) != len(set(completed)):
            raise SessionStop('duplicate completed run IDs')
        attempts = {e['attempt_id'] for e in events if e['kind'] == 'attempt_begin'}
        if any(Path(a).name != a or a in ('.', '..') for a in attempts):
            raise SessionStop('unsafe ledger attempt ID')
        closed = {e['attempt_id'] for e in events if e['kind'] in ('attempt_failed', 'request_completed')}
        if attempts != closed or any(e['kind'] == 'terminal_stop' for e in events):
            raise SessionStop('unfinished or terminal attempt requires author review')
        attempt_root = session / 'attempts'
        if attempt_root.exists():
            if attempt_root.is_symlink() or not attempt_root.is_dir():
                raise SessionStop('invalid attempts root')
            if any(p.name not in attempts or p.is_symlink() or not p.is_dir()
                   for p in attempt_root.iterdir()):
                raise SessionStop('unledgered attempt artifacts')
        for event in events:
            if event['kind'] in ('attempt_failed', 'request_completed'):
                if _attempt_snapshot(attempt_root / event['attempt_id']) != event['raw_sha256']:
                    raise SessionStop('raw attempt artifact digest changed')
        begins = {e['segment'] for e in events if e['kind'] == 'session_begin'}
        ends = {e['segment'] for e in events if e['kind'] == 'session_end'}
        if begins != ends:
            raise SessionStop('unclosed session segment requires author review')
        past = sum(e['elapsed_s'] for e in events if e['kind'] in ('session_end', 'archive_elapsed'))
        records = [load_json(session / e['record']) for e in events if e['kind'] == 'request_completed']
        if any(sha256_file(session / e['record']) != e['record_sha256']
               for e in events if e['kind'] == 'request_completed'):
            raise SessionStop('completed record digest changed')
        if [r['run_id'] for r in records] != completed or any(r.get('status') != 'ok' for r in records):
            raise SessionStop('completed record/ledger mismatch')
        slots = protocol.schedule(plan)
        if not set(completed) <= {s['run_id'] for s in slots}:
            raise SessionStop('unknown completed slot')
        if completed != [s['run_id'] for s in slots[:len(completed)]]:
            raise SessionStop('completed slots are not a sequential prefix')
        identity_check = deps.get('identity_check', _verify_identities)
        guards = identity_check(config, report)
        guards.update(evidence_guards)
        input_names = ('plan.json', 'config.json', 'preflight.json',
                       'execution_authorized.json', 'historical_identity.json',
                       'source_manifest.json')
        input_names += (('historical_identity_source.json', 'git_provenance.json') if git_report is not None
                        else ('historical_collection.json', 'historical_c03/history_inventory.json'))
        optional_inputs = ('zip_ingestion_report.json', 'historical_zip_ingestion.json',
                           'historical_recovery.json', 'original_zip_manifest.json',
                           'historical_zip/ZIP_INGESTION_REPORT.json',
                           'historical_zip/RECOVERED_VERIFICATION_REPORT.json',
                           'runtime_selection.json', 'execution_config.json', 'execution_preflight.json',
                           'session_clock.json', 'runtime_fallback_build.json')
        input_names += tuple(name for name in optional_inputs if (session / name).exists())
        for name in input_names:
            guards[str(session / name)] = stat_identity(session / name)
        frozen = digest({name: load_json(session / name) for name in input_names})
        prior = [e for e in events if e['kind'] == 'session_begin']
        if any(e['frozen_digest'] != frozen for e in prior):
            raise SessionStop('session inputs changed on resume')
        begin_event = append_event(session, 'session_begin', segment=segment, frozen_digest=frozen)
        first_wall_ns = min([e['timestamp_ns'] for e in prior] + [begin_event['timestamp_ns']])
        session_started_wall_s = config.get('session_started_wall_s')
        if (session / 'session_clock.json').exists():
            clock_record = load_json(session / 'session_clock.json')
            if clock_record.get('approval_sha256') != sha256_file(session / 'execution_authorized.json'):
                raise SessionStop('session clock approval mismatch')
            session_started_wall_s = clock_record.get('session_started_wall_s')
        if session_started_wall_s is not None:
            if (isinstance(session_started_wall_s, bool) or not isinstance(session_started_wall_s, (int, float))
                    or not math.isfinite(session_started_wall_s)
                    or session_started_wall_s * 1e9 > wall_clock()):
                raise SessionStop('invalid session start clock')
            first_wall_ns = min(first_wall_ns, session_started_wall_s * 1e9)
        began = True
        def elapsed():
            wall_elapsed = (wall_clock() - first_wall_ns) / 1e9
            if wall_elapsed < 0:
                raise SessionStop('wall clock moved backwards; session budget requires review')
            # Resume pauses must not reset the one-session deadline. Active
            # accounting remains a conservative lower bound under clock skew.
            return max(past + clock() - start, wall_elapsed)
        def guard():
            # Preserve the archival reserve even if the next bounded request
            # consumes both warmup and measured HTTP deadlines plus startup.
            next_attempt_bound = (plan['runtime']['startup_timeout_s']
                                  + 2 * plan['runtime']['request_timeout_s']
                                  + plan['runtime']['warmup_settle_s']
                                  + plan['runtime']['monitor_preroll_s']
                                  + plan['cooldown_s'] + 10)
            if elapsed() + next_attempt_bound + plan['archive_reserve_s'] + plan['duration_rule']['safety_reserve_s'] >= plan['maximum_session_s']:
                raise SessionStop('budget requires author review')
            if digest({name: load_json(session / name) for name in input_names}) != frozen:
                raise SessionStop('frozen input drift')
            if any(stat_identity(p) != v for p, v in guards.items()):
                raise SessionStop('file identity drift')
        sleep(plan['initial_cooldown_s'])
        # Thresholds remain the frozen Intel constants. The protocol hash must
        # bind the SAME held-out evaluation definition used for recalibration.
        zero = calibration.zero_shot_model(plan['calibration'])
        model = None
        selection = None
        duration = None
        retries = sum(e['kind'] == 'retry_authorized' for e in events)
        for stage in plan['cohorts']:
            cohort = stage['name']
            if cohort != 'technical_smoke' and duration is None:
                raise SessionStop('duration tier must be frozen before exploration')
            if cohort == 'policy_confirmatory' and duration['selected_tier'] == 'STATIC_SIGNAL':
                gate = {**gate, 'scientific_gate_status': gate.get('status'), 'status': 'NOT_PLANNED',
                        'execution_tier': 'STATIC_SIGNAL'}
                append_event(session, 'cohort_skipped', cohort=cohort,
                             run_ids=[s['run_id'] for s in protocol.schedule(plan, cohort, tier='FULL')],
                             planned_requests=0, omitted_policy_slots=24,
                             reason='policy NOT_PLANNED: frozen timing-only STATIC_SIGNAL tier')
                continue
            if cohort == 'calibration':
                path = session / 'selection.json'
                exploration = [r for r in records if r['cohort'] == 'exploration']
                inputs_digest = digest([exploration, plan['calibration']])
                if path.exists():
                    hashes = [e['selection_sha256'] for e in events if e['kind'] == 'selection_frozen']
                    if not hashes or any(h != sha256_file(path) for h in hashes):
                        raise SessionStop('frozen selection digest changed or freeze incomplete')
                    saved = load_json(path)
                    if saved['inputs_digest'] != inputs_digest:
                        raise SessionStop('selection exploration input drift')
                    selection = saved['selection']
                else:
                    selection = deps.get('select_opportunity', calibration.select_opportunity)(
                        deepcopy(exploration), plan['calibration'])
                    save_json(path, {'selection': selection, 'inputs_digest': inputs_digest})
                    append_event(session, 'selection_frozen', selection_sha256=sha256_file(path))
                if selection['initial_mask'] not in protocol.static_specs(report['topology']):
                    raise SessionStop('invalid frozen initial configuration')
                guards[str(path)] = stat_identity(path)
            if cohort == 'validation':
                path = session / 'frozen_detector.json'
                training = [r for r in records if r['cohort'] == 'calibration']
                if path.exists():
                    hashes = [e['model_sha256'] for e in events if e['kind'] == 'model_frozen']
                    if not hashes or any(h != sha256_file(path) for h in hashes):
                        raise SessionStop('frozen detector digest changed or freeze incomplete')
                    frozen_model = load_json(path)
                    if frozen_model['training_digest'] != digest(training):
                        raise SessionStop('frozen training changed')
                    model = frozen_model['model']
                else:
                    try:
                        model = deps.get('fit', calibration.fit)(deepcopy(training), plan['calibration'])
                        reason = None
                    except ValueError as exc:
                        if str(exc) not in ('no nondegenerate train-only threshold candidates',
                                            'no marker-eligible training requests'):
                            raise
                        reason = str(exc)
                    save_json(path, {'model': model, 'reason': reason,
                                     'training_digest': digest(training)})
                append_event(session, 'model_frozen', model_sha256=sha256_file(path))
                guards[str(path)] = stat_identity(path)
            if cohort == 'static_confirmatory':
                path = session / 'validation_gate.json'
                exploration = [r for r in records if r['cohort'] == 'exploration']
                validation = [r for r in records if r['cohort'] == 'validation']
                if path.exists():
                    hashes = [e['gate_sha256'] for e in events if e['kind'] == 'gate_frozen']
                    if not hashes or any(h != sha256_file(path) for h in hashes):
                        raise SessionStop('frozen gate digest changed or freeze incomplete')
                    saved = load_json(path)
                    if saved['inputs_digest'] != digest([exploration, validation, model, selection]):
                        raise SessionStop('validation gate input drift')
                    gate = saved['gate']
                else:
                    evaluate = deps.get('evaluate', calibration.evaluate)
                    zs = evaluate(deepcopy(validation), zero, plan['calibration'])
                    calibrated = evaluate(deepcopy(validation), model, plan['calibration']) if model else None
                    gate = (deps.get('gate_policy', calibration.gate_policy)(
                        deepcopy(exploration), calibrated, model, plan['calibration']) if model else
                        {**selection, 'status': 'NOT_ELIGIBLE', 'reasons': ['no suitable calibration model']})
                    if gate.get('selection_hash') != selection['selection_hash']:
                        raise SessionStop('validation gate selection differs from frozen selection')
                    save_json(path, {'gate': gate, 'zero_shot': zs, 'calibrated': calibrated,
                                     'inputs_digest': digest([exploration, validation, model, selection])})
                    append_event(session, 'gate_frozen', gate_sha256=sha256_file(path))
                guards[str(path)] = stat_identity(path)
                if gate.get('selection_hash') != selection['selection_hash']:
                    raise SessionStop('validation gate selection differs from frozen selection')
            if cohort == 'policy_confirmatory' and gate['status'] != 'ELIGIBLE':
                skipped = [s['run_id'] for s in protocol.schedule(plan, cohort)]
                append_event(session, 'cohort_skipped', cohort=cohort, run_ids=skipped,
                             reason='policy NOT_ELIGIBLE')
                continue
            specs = (protocol.policy_specs(report['topology'], gate) if stage['arms'] == 'policy'
                     else protocol.static_specs(report['topology']))
            for item in protocol.schedule(plan, cohort):
                if item['arm'] == 'INITIAL_SELECTED':
                    item = {**item, 'operating_configuration': selection['initial_mask']}
                actual_arm = item.get('operating_configuration', item['arm'])
                if item['run_id'] in completed:
                    previous = next(r for r in records if r['run_id'] == item['run_id'])
                    if any(previous.get(k) != v for k, v in item.items()):
                        raise SessionStop('completed slot metadata differs from frozen schedule')
                    continue
                slot_retries = sum(e['kind'] == 'retry_authorized' and e['run_id'] == item['run_id'] for e in events)
                while True:
                    guard()
                    check = machine(config, expected=report['machine_state'])
                    append_event(session, 'machine_gate', run_id=item['run_id'], report=check)
                    if check['status'] != 'PASS':
                        raise SessionStop('per-request machine gate failed')
                    attempt = item['run_id'] + '_' + uuid.uuid4().hex
                    attempt_dir = session / 'attempts' / attempt
                    attempt_dir.parent.mkdir(exist_ok=True)
                    append_event(session, 'attempt_begin', run_id=item['run_id'], attempt_id=attempt)
                    active = True
                    try:
                        record = backend(deepcopy(config), deepcopy(item), deepcopy(specs[actual_arm]),
                                         attempt_dir, deepcopy(model or zero))
                        if record.get('status') != 'ok' or any(record.get(k) != v for k, v in item.items()):
                            raise SessionStop('backend returned invalid slot record')
                    except runtime.RuntimeFailure as exc:
                        can_retry = (exc.retryable and exc.tokens_seen == 0 and
                                     exc.reason in plan['retry_codes'] and
                                     slot_retries < plan['maximum_technical_retries_per_slot'] and
                                     retries < plan['maximum_technical_retries_session'])
                        append_event(session, 'attempt_failed', run_id=item['run_id'], attempt_id=attempt,
                                     reason=exc.reason, tokens_seen=exc.tokens_seen, retryable=can_retry,
                                     raw_sha256=_attempt_snapshot(attempt_dir))
                        active = False
                        if not can_retry:
                            append_event(session, 'terminal_stop', reason=exc.reason)
                            raise SessionStop(exc.reason)
                        slot_retries += 1
                        retries += 1
                        append_event(session, 'retry_authorized', run_id=item['run_id'], attempt_id=attempt)
                        sleep(plan['cooldown_s'])
                        continue
                    record_path = Path('records') / (attempt + '.json')
                    save_json(session / record_path, record)
                    append_event(session, 'request_completed', run_id=item['run_id'],
                                 attempt_id=attempt, record=str(record_path),
                                 record_sha256=sha256_file(session / record_path),
                                 raw_sha256=_attempt_snapshot(attempt_dir))
                    active = False
                    records.append(record)
                    completed.append(item['run_id'])
                    sleep(plan['cooldown_s'])
                    break
            if cohort == 'static_confirmatory':
                path = session / 'frozen_transfer_report.json'
                static = [r for r in records if r['cohort'] == cohort]
                inputs_digest = digest([static, model, plan['calibration']])
                if path.exists():
                    hashes = [e['transfer_sha256'] for e in events if e['kind'] == 'transfer_frozen']
                    if not hashes or any(h != sha256_file(path) for h in hashes):
                        raise SessionStop('frozen transfer digest changed or freeze incomplete')
                    if load_json(path)['inputs_digest'] != inputs_digest:
                        raise SessionStop('transfer input drift')
                else:
                    transfer = (deps.get('transfer_report', calibration.transfer_report)(
                        deepcopy(static), deepcopy(model), plan['calibration']) if model else
                        {'status': 'NOT_EVALUATED', 'reason': 'no suitable calibration model', 'retuning_allowed': False})
                    save_json(path, {'report': transfer, 'inputs_digest': inputs_digest})
                    append_event(session, 'transfer_frozen', transfer_sha256=sha256_file(path))
                guards[str(path)] = stat_identity(path)
            if cohort == 'technical_smoke':
                path = session / 'tier_decision.json'
                smoke = [r for r in records if r['cohort'] == cohort]
                if not path.exists() and any(e['kind'] == 'tier_decision_frozen' for e in events):
                    raise SessionStop('frozen duration decision file is missing; recomputation forbidden')
                if path.exists():
                    hashes = [e['decision_sha256'] for e in events if e['kind'] == 'tier_decision_frozen']
                    if not hashes or any(h != sha256_file(path) for h in hashes):
                        raise SessionStop('frozen duration decision digest changed or freeze incomplete')
                    duration = load_json(path)
                    expected_duration = protocol.duration_decision(plan, smoke, duration['inputs']['elapsed_s'])
                    if duration != expected_duration:
                        raise SessionStop('frozen duration decision inputs/rule/schedules changed')
                else:
                    duration = protocol.duration_decision(plan, smoke, elapsed())
                    save_json(path, duration)
                    append_event(session, 'tier_decision_frozen', decision_sha256=sha256_file(path),
                                 selected_tier=duration['selected_tier'])
                guards[str(path)] = stat_identity(path)
                if duration['selected_tier'] == 'STOP':
                    raise SessionStop('observed smoke forecast exceeds budget for both approved tiers')
                result['selected_tier'] = duration['selected_tier']
                slots = protocol.schedule(plan, tier=duration['selected_tier'])
        result['status'] = 'COMPLETE'
    except (Exception, KeyboardInterrupt) as exc:
        result['reason'] = str(exc) or type(exc).__name__
        if lock_acquired:
            append_event(session, 'session_stopped' if isinstance(exc, KeyboardInterrupt) and not active else 'terminal_stop',
                         reason=result['reason'], resumable=isinstance(exc, KeyboardInterrupt) and not active)
    finally:
        if lock_acquired:
            if began:
                append_event(session, 'session_end', segment=segment,
                             elapsed_s=max(0, clock() - start), status=result['status'])
            try:
                output = analyzer(deepcopy(records), load_json(session / 'plan.json'), deepcopy(gate))
                save_json(session / 'analysis' / segment / 'analysis.json', output)
                deps.get('output_writer', analysis.write_outputs)(deepcopy(records), output,
                                                                 session / 'analysis' / segment)
            except Exception as exc:
                append_event(session, 'analysis_failed', error=str(exc))
                result['status'] = 'STOPPED'
            target = session.parent / (session.name + '_' + segment + '.zip')
            archive_start = clock()
            try:
                verified = archive_builder(session, target)
                result['final_archive'] = verified
                if verified.get('status') != 'PASS':
                    result['status'] = 'ARCHIVE_FAILED'
            except Exception as exc:
                result.update(status='ARCHIVE_FAILED', final_archive={'status': 'FAIL',
                              'archive': str(target), 'error': str(exc)})
            append_event(session, 'archive_elapsed', elapsed_s=max(0, clock() - archive_start),
                         archive=str(target), status=result['final_archive']['status'])
            fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return result
