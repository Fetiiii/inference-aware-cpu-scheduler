"""Explicit preparation and execution gates; importing/--help never runs inference."""
import argparse
import fcntl
import signal
import shlex
from pathlib import Path
import sys
import time

from . import archive, distribution, git_delivery, historical_identity, historical_zip, preflight, protocol
from .common import digest, load_json, save_json, sha256_file


def review_template(plan=None, package_hash=None):
    """Legacy ZIP author-side packaging only, never Git execution approval."""
    plan = plan or protocol.load_plan()
    return {**protocol.approval_bindings(plan),
            'package_sha256': package_hash or distribution.package_fingerprint(),
            'authorized': False, 'author_review_id': '',
            'runtime_fallback_authorized': False,
            'review_notice': 'UNSIGNED / NOT AUTHORIZED. Author must review both schedules, the exact duration rule and optional fallback policy before deployment. No in-session methodological choices.'}


def final_archive(session, kind='preflight'):
    target = archive.private_archive_path(session, kind, str(time.time_ns()))
    report = archive.archive_session(session, target)
    if report['status'] != 'PASS':
        print(f'ARCHIVE VERIFICATION FAILED: {target}')
        raise RuntimeError('Private archive failed integrity verification; preserve diagnostics')
    label = {'preflight': 'PREFLIGHT-ONLY ARCHIVE', 'recovery': 'MANUAL RECOVERY ARCHIVE',
             'run_failed': 'FAILED RUN/PREPARATION ARCHIVE'}[kind]
    print(f'{label}: {target}\nSHA256: {report["sha256"]}')
    print_delivery_command(target, session)
    return report


def print_delivery_command(target, session):
    launcher = distribution.CODE_ROOT / distribution.PACKAGE / 'amd_oneshot.sh'
    print('VERIFY BEFORE DELIVERY: ' + shlex.join(['bash', str(launcher), 'verify', str(target), '--session', str(session)]))


def checked_session(path):
    """CLI may archive/resume only this checkout's dedicated JE session roots."""
    session = Path(path).absolute()
    if (session.resolve() != session or session.parent != distribution.CODE_ROOT.resolve() / 'results'
            or not session.name.startswith('journal_extension_amd_') or session.is_symlink()):
        raise ValueError('Session must be this checkout results/journal_extension_amd_*; no unrelated archival')
    return session


def parser():
    root = argparse.ArgumentParser(prog='amd_oneshot.sh', description='Git-native JE AMD; run requires exact-commit author approval AND --confirm.')
    sub = root.add_subparsers(dest='command', required=True)
    pf = sub.add_parser('preflight', help='Read-only Git/source, public identity, machine and runtime checks; NO inference/build.')
    pf.add_argument('--repo', type=Path, default=distribution.CODE_ROOT)
    pf.add_argument('--server-bin', type=Path, help='Default: llama.cpp/build-diag/bin/llama-server; identity always verified.')
    pf.add_argument('--model', type=Path, help='Default: models/Qwen3.5-9B-Q4_K_M.gguf; full frozen SHA required.')
    pf.add_argument('--prompt', type=Path)
    pf.add_argument('--session', type=Path)
    pf.add_argument('--build-manifest', type=Path)
    pf.add_argument('--build-manifest-sha256')
    run = sub.add_parser('run', help='Author-approved one-session run; collaborator consent required.')
    run.add_argument('--session', type=Path, default=distribution.CODE_ROOT / 'results/journal_extension_amd_01')
    run.add_argument('--approval', type=Path)
    run.add_argument('--confirm', action='store_true')
    pk = sub.add_parser('package', help='Archive existing session only; never reruns experiments.')
    pk.add_argument('--session', type=Path, default=distribution.CODE_ROOT / 'results/journal_extension_amd_01')
    validate = sub.add_parser('verify', help='Verify private result ZIP CRC and included SHA256.')
    validate.add_argument('archive', type=Path)
    validate.add_argument('--session', type=Path, help='Required before delivery: verify this exact ZIP against final local session state, not merely CRC/SHA.')
    prep = sub.add_parser('prepare-bundle', help='Create PRIVATE portable source + verified historical-evidence ZIP; no execution.')
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--historical-zip', type=Path, required=True)
    sub.add_parser('fingerprint', help='Verify tracked sources and print exact Git/plan/source identities.')
    template = sub.add_parser('approval-template', help='Write an unsigned hash-bound review template (authorized=false).')
    template.add_argument('--output', type=Path, required=True)
    locate = sub.add_parser('locate', help='Show canonical local model/runtime candidates; never downloads or runs them.')
    locate.add_argument('--model', type=Path)
    locate.add_argument('--server-bin', type=Path)
    history = sub.add_parser('verify-history', help='AUTHOR ONLY: validate private C03 ZIP offline; not required by new measurements.')
    history.add_argument('--historical-zip', type=Path, required=True)
    history.add_argument('--output', type=Path, required=True)
    return root


def do_preflight(args):
    preparation_started = time.time()
    repo = args.repo.resolve()
    if repo != distribution.CODE_ROOT.resolve():
        raise ValueError('Use the launcher in the target Git checkout; --repo must match that checkout')
    session = (args.session or repo / 'results/journal_extension_amd_01').resolve()
    if session.exists():
        raise ValueError('Session already exists; never overwrite. Inspect/archive it; resume uses run with existing session.')
    # Avoid overwriting any historical dataset, source tree or wider directory.
    approved_parent = repo / 'results'
    if session.parent != approved_parent or not session.name.startswith('journal_extension_amd_'):
        raise ValueError('New session must be results/journal_extension_amd_* under the explicit repository')
    session.mkdir(parents=True, exist_ok=False)
    try:
        plan = protocol.load_plan()
        save_json(session / 'plan.json', plan)
        save_json(session / 'schedule.json', protocol.schedule(plan))
        save_json(session / 'duration_planning.json', protocol.forecast(plan))
        git_report = git_delivery.verify_checkout(repo)
        save_json(session / 'git_provenance.json', git_report)
        source = git_delivery.snapshot_source(session / 'source', git_report, root=repo)
        save_json(session / 'source_manifest.json', source)
        # Author-side verified identities are not a claim that private raw
        # records exist here. Private archive validation is an offline task.
        anchor = historical_identity.stage_identity(session, plan)
        server = args.server_bin or repo / 'llama.cpp/build-diag/bin/llama-server'
        model = args.model or repo / 'models' / plan['model_name']
        config = dict(repo_root=str(repo), server_bin=str(server.resolve()),
                      plan=plan,
                      identity_kind='git', git_commit=git_report['commit_sha'],
                      model=str(model.resolve()),
                      prompt=str((args.prompt or repo / plan['prompt_relative_path']).resolve()),
                      pinned_revision=plan['source_revision'],
                      expected_model_sha256=plan['model_sha256'],
                      expected_prompt_sha256=plan['prompt_sha256'],
                      package_sha256=source['package_sha256'], disk_path=str(session),
                      session_started_wall_s=preparation_started,
                      historical_identity=anchor['diagnostic_build'],
                      **plan['runtime'], **plan['safety'])
        for name in ('build_manifest',):
            if getattr(args, name):
                config[name] = str(getattr(args, name).resolve())
        if args.build_manifest_sha256:
            config['expected_build_manifest_sha256'] = args.build_manifest_sha256
        config['interval_ms'] = plan['calibration']['interval_ms']
        save_json(session / 'config.json', config)
        report = preflight.perform_preflight(config, session / 'environment')
        # A deferred runtime gate is NOT a preflight PASS or build permission.
        # Only explicit compatibility failures qualify; all other gates remain
        # hard failures. Original diagnostics are retained without rewriting.
        allowed_runtime_errors = ('ORIGINAL_BINARY_MISSING:', 'ORIGINAL_BINARY_MISMATCH:',
                                  'ORIGINAL_BINARY_INCOMPATIBLE:', 'ORIGINAL_KNOWN_LIBRARY_MISMATCH:')
        if (report['errors']
                and all(any(error.startswith('build provenance: ' + prefix)
                            for prefix in allowed_runtime_errors) for error in report['errors'])):
            report['status'] = 'RUNTIME_FALLBACK_PENDING'
            report['runtime_fallback_notice'] = 'All non-runtime checks passed. No build/execution authorized; run requires separately approved fallback policy and a new full preflight.'
        save_json(session / 'preflight.json', report)
        print(f'PREFLIGHT: {report["status"]}\nSESSION: {session}')
        print('After NEW exact-commit author approval: ' + shlex.join([
            'bash', 'conference/journal_extension/amd_oneshot/amd_oneshot.sh',
            'run', '--session', str(session), '--confirm', '--approval',
            '.je_amd_authorization/hotfix_execution_approval.json']))
        for error in report['errors']:
            print(f'GATE: {error}')
        return 0 if report['status'] == 'PASS' else 2
    except BaseException as exc:
        save_json(session / f'preparation_failure_{time.time_ns()}.json',
                  {'error_type': type(exc).__name__, 'message': str(exc)})
        raise
    finally:
        # Even missing source/model/build evidence is returned in one archive.
        final_archive(session)


def authorize_run(args, session):
    try:
        plan = load_json(session / 'plan.json')
        if plan != protocol.load_plan():
            raise ValueError('Frozen session plan differs from packaged protocol')
        report = load_json(session / 'preflight.json')
        if report['status'] not in ('PASS', 'RUNTIME_FALLBACK_PENDING'):
            raise ValueError('Successful non-runtime preflight is required')
        config = load_json(session / 'config.json')
        if config.get('identity_kind') != 'git':
            raise ValueError('Git-native run rejects ZIP-era sessions/approvals')
        if Path(config['repo_root']).resolve() != distribution.CODE_ROOT.resolve():
            raise ValueError('Session checkout differs from the executing Git source checkout')
        git_report = git_delivery.verify_checkout(config['repo_root'], expected_commit=config['git_commit'])
        if git_report != load_json(session / 'git_provenance.json'):
            raise ValueError('Git provenance changed after preflight')
        historical_identity.verify_session_identity(session, plan)
        current_hash = git_report['source_sha256']
        if config['package_sha256'] != current_hash:
            raise ValueError('Package changed after preflight; do not mix versions')
        auth_path = session / 'execution_authorized.json'
        if auth_path.exists():
            approval = load_json(auth_path)
            if args.approval and load_json(args.approval) != approval:
                raise ValueError('Cannot replace frozen execution approval')
        elif args.approval:
            approval = load_json(args.approval)
            protocol.check_approval(plan, approval, current_hash, git_report=git_report)
            save_json(auth_path, approval)
        else:
            raise ValueError('Separate author approval file required via --approval')
        protocol.check_approval(plan, approval, current_hash, git_report=git_report)
        if report['status'] == 'RUNTIME_FALLBACK_PENDING' and approval.get('runtime_fallback_authorized') is not True:
            raise ValueError('Historical runtime unavailable; fallback was not separately author-approved')
        return plan, config, approval, report
    except BaseException:
        final_archive(session, kind='run_failed')
        raise


def copy_runtime_diagnostics(session, work_root):
    """Snapshot textual build evidence only; reject replacement on a later call."""
    import shutil
    candidates = [work_root / name for name in ('runtime-fallback-manifest.json', 'runtime-fallback-failure.json')]
    candidates += sorted(work_root.glob('command-*.log'))
    candidates += sorted(work_root.glob('command-*.memory.json'))
    for source_file in candidates:
        if not source_file.is_file():
            continue
        target = session / 'runtime_fallback_diagnostics' / source_file.name
        if target.exists():
            if sha256_file(target) != sha256_file(source_file):
                raise ValueError('Runtime diagnostic copy drift')
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target)
    directory = session / 'runtime_fallback_diagnostics'
    return {str(path.relative_to(directory)): sha256_file(path)
            for path in sorted(directory.glob('*')) if path.is_file()}


def prepare_runtime(session, plan, config, approval, report):
    """Approved build-only fallback, before any request, archived on every stop."""
    from . import coordinator, runtime_fallback
    coordinator._validate_config(config, plan)
    # Revalidate source and historical copies BEFORE even an approved build.
    source = load_json(session / 'source_manifest.json')
    if source['package_sha256'] != config['package_sha256'] or digest(source['files']) != config['package_sha256']:
        raise ValueError('Source manifest approval mismatch')
    coordinator._verify_manifest_files(session / 'source', source['files'], exact=True)
    if config.get('identity_kind') == 'git':
        git_report = git_delivery.verify_checkout(config['repo_root'], expected_commit=config['git_commit'])
        protocol.check_approval(plan, approval, config['package_sha256'], git_report=git_report)
        if git_report != load_json(session / 'git_provenance.json'):
            raise ValueError('Git provenance changed before runtime preparation')
        historical_identity.verify_session_identity(session, plan)
    else:  # Python-only legacy fixtures/private author evidence, not CLI runs.
        history = load_json(session / 'historical_collection.json')
        if history != load_json(session / 'historical_c03/history_inventory.json'):
            raise ValueError('Historical collection changed')
        coordinator._verify_manifest_files(session / 'historical_c03',
                                          {row['path']: row['sha256'] for row in history['inventory']})
    clock_path = session / 'session_clock.json'
    clock_value = {'session_started_wall_s': config['session_started_wall_s'],
                   'approval_sha256': sha256_file(session / 'execution_authorized.json')}
    if clock_path.exists():
        if load_json(clock_path) != clock_value:
            raise ValueError('Session clock cannot be replaced')
    else:
        save_json(clock_path, clock_value)
    if report['status'] == 'PASS' or (session / 'runtime_selection.json').exists():
        return
    if (session / 'runtime_fallback_attempt.json').exists():
        raise ValueError('Previous runtime fallback attempt requires review; no automatic retry')
    if approval.get('runtime_fallback_authorized') is not True:
        raise ValueError('Runtime fallback is not approved')
    # Read-only machine/model/prompt checks must still pass immediately before
    # compilation. Compilation cannot be used to bypass an interference gate.
    build_config = {**config, 'memory_phase': 'pre_build',
                    'min_available_memory_gib': config.get('min_build_available_memory_gib', 12)}
    machine = preflight.check_machine_state(build_config, report['machine_state'])
    save_json(session / 'runtime_fallback_machine_check.json', machine)
    if machine['status'] != 'PASS':
        raise ValueError('Machine gate failed before fallback build')
    for name in ('model', 'prompt'):
        if (report['identities'][name]['sha256'] != plan[name + '_sha256']
                or sha256_file(config[name]) != report['identities'][name]['sha256']):
            raise ValueError(name + ' changed before runtime fallback')
    work_root = session.parent / ('.je_amd_runtime_' + session.name)
    save_json(session / 'runtime_fallback_attempt.json',
              {'work_root': str(work_root), 'approval_sha256': clock_value['approval_sha256'],
               'started_wall_s': time.time()})
    try:
        built = runtime_fallback.build_fallback(config, approval, work_root,
                                               clock_value['session_started_wall_s'])
        selected_config = {**config, **built['config_overrides']}
        save_json(session / 'runtime_fallback_build.json', built['manifest'])
        runtime_build_hash = sha256_file(session / 'runtime_fallback_build.json')
        if runtime_build_hash != selected_config.get('expected_runtime_fallback_manifest_sha256'):
            raise ValueError('Runtime build manifest copy hash mismatch')
        save_json(session / 'execution_config.json', selected_config)
        checked = preflight.perform_preflight(selected_config, session / 'execution_environment')
        save_json(session / 'execution_preflight.json', checked)
        if checked['status'] != 'PASS':
            raise ValueError('Fallback runtime full preflight failed')
        diagnostic_hashes = copy_runtime_diagnostics(session, work_root)
        if not diagnostic_hashes:
            raise ValueError('Missing runtime build diagnostics')
        save_json(session / 'runtime_selection.json', {
            'status': 'SELECTED',
            'original_config_sha256': sha256_file(session / 'config.json'),
            'original_preflight_sha256': sha256_file(session / 'preflight.json'),
            'execution_config_sha256': sha256_file(session / 'execution_config.json'),
            'execution_preflight_sha256': sha256_file(session / 'execution_preflight.json'),
            'approval_sha256': clock_value['approval_sha256'],
            'runtime_fallback_policy_sha256': digest(plan['runtime_fallback_policy']),
            'runtime_build_sha256': runtime_build_hash,
            'diagnostics_sha256': diagnostic_hashes})
    finally:
        # Preserve bounded textual diagnostics even if compilation is cancelled;
        # never put model weights, source checkout or compiled products in ZIP.
        copy_runtime_diagnostics(session, work_root)


def do_run(args):
    if not args.confirm:
        raise ValueError('No execution: deliberate collaborator --confirm is required')
    session = checked_session(args.session)
    if not session.is_dir():
        raise ValueError('No successful preflight/session; run is forbidden')
    previous = signal.getsignal(signal.SIGTERM)
    def cancel(signum, frame):
        raise KeyboardInterrupt('supervised cancellation (SIGTERM)')
    signal.signal(signal.SIGTERM, cancel)
    try:
        # Even build/error archives must not race another session writer.
        with (session / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            plan, config, approval, report = authorize_run(args, session)
            try:
                prepare_runtime(session, plan, config, approval, report)
            except BaseException as exc:
                save_json(session / f'runtime_preparation_failure_{time.time_ns()}.json',
                          {'error_type': type(exc).__name__, 'message': str(exc)})
                final_archive(session, kind='run_failed')
                raise
        # Coordinator independently reacquires and revalidates frozen inputs.
        from .coordinator import execute_session
        result = execute_session(session)
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(f'SESSION: {result}')
    final = result.get('final_archive') or {}
    if final.get('status') == 'PASS':
        with (session / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            delivery = archive.verify_delivery(Path(final['archive']), session)
        print(f'FINAL RUN ARCHIVE: {final["archive"]}\nFINAL RUN STATUS: {result["status"]}')
        print(f'FINAL SNAPSHOT VERIFICATION: {delivery["status"]}; completed requests={delivery.get("completed_requests", "UNKNOWN")}; measured requests={delivery.get("measured_requests", "UNKNOWN")}')
        print_delivery_command(Path(final['archive']), session)
        if delivery['status'] != 'PASS':
            print('DO NOT SEND: archive does not match final session state: ' + delivery.get('error', 'verification failed'))
            return 2
    return 0 if result.get('status') == 'COMPLETE' and final.get('status') == 'PASS' else 2


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'preflight':
            previous = signal.getsignal(signal.SIGTERM)
            def cancel_preparation(signum, frame):
                raise KeyboardInterrupt('supervised preflight cancellation (SIGTERM)')
            signal.signal(signal.SIGTERM, cancel_preparation)
            try:
                return do_preflight(args)
            finally:
                signal.signal(signal.SIGTERM, previous)
        if args.command == 'run':
            return do_run(args)
        if args.command == 'package':
            session = checked_session(args.session)
            if not session.is_dir():
                raise ValueError('Session directory missing')
            # Share the same session lock as execution; do not zip changing files.
            with (session / '.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                final_archive(session, kind='recovery')
            return 0
        if args.command == 'verify':
            if args.session is not None:
                session = checked_session(args.session)
                if not session.is_dir():
                    raise ValueError('Final local session directory missing; no delivery-state verification')
                with (session / '.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    report = archive.verify_delivery(args.archive, session)
            else:
                report = {**archive.validate_archive(args.archive), 'verification_scope': 'integrity_only',
                          'delivery_ready': False}
            print(report)
            return 0 if report['status'] == 'PASS' else 2
        if args.command == 'prepare-bundle':
            print(distribution.build_prep_zip(args.output, historical_zip=args.historical_zip.resolve()))
            return 0
        if args.command == 'fingerprint':
            report = git_delivery.verify_checkout()
            print('GIT COMMIT:', report['commit_sha'])
            print('PLAN SHA256:', digest(protocol.load_plan()))
            print('SOURCE SHA256:', report['source_sha256'])
            return 0
        if args.command == 'approval-template':
            save_json(args.output, git_delivery.approval_template(protocol.load_plan(), git_delivery.verify_checkout()))
            print(f'UNSIGNED / NOT AUTHORIZED: {args.output}')
            return 0
        if args.command == 'locate':
            plan = protocol.load_plan()
            for name, path, expected in [('MODEL', args.model or distribution.CODE_ROOT / 'models' / plan['model_name'], plan['model_sha256']),
                                          ('RUNTIME', args.server_bin or distribution.CODE_ROOT / 'llama.cpp/build-diag/bin/llama-server', plan['historical_binary_sha256'])]:
                path = path.resolve()
                print(f'{name}: {path}\nEXISTS: {path.is_file()}\nEXPECTED SHA256: {expected}')
            print('Candidates only; preflight performs full identity checks. No automatic model download or runtime build.')
            return 0
        if args.command == 'verify-history':
            plan = protocol.load_plan()
            ingested = historical_zip.ingest_archive(args.historical_zip.resolve(), args.output.resolve(), plan['historical_archive_sha256'])
            checked = historical_zip.validate_recovered(ingested, plan)
            print(f'AUTHOR OFFLINE HISTORICAL VALIDATION: {checked["status"]}')
            return 0 if checked['status'] == 'PASS' else 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'NOT READY: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
