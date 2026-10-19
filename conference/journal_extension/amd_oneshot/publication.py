"""Read-only, bounded privacy checks for explicitly selected public Git blobs.

Reports contain paths, categories and line numbers only. They never reproduce
matched content. This is a publication gate, not an automatic staging tool.
"""
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

MAX_BYTES = 256 * 1024
MAX_FILES = 4096
MAX_COMMITS = 1024
_TEXT_SUFFIXES = {'.py', '.md', '.json', '.sh'}
_RULES = (
    ('personal_home', re.compile(r'(?:/home/[A-Za-z0-9_.-]+|/Users/[A-Za-z0-9_.-]+|[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9_.-]+)')),
    ('private_key', re.compile(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----')),
    ('github_token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b')),
    ('huggingface_token', re.compile(r'\bhf_[A-Za-z0-9]{20,}\b')),
    ('aws_access_key', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')),
    ('credential_assignment', re.compile(r'(?i)\b(?:password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[=:]\s*[\x22\x27]?[A-Za-z0-9+/_.-]{16,}')),
    ('private_host_target', re.compile(r'(?i)\b(?:ssh|scp|sftp)\s+(?:[^\s]+\s+)*?[^\s@]+@[^\s/\x22\x27]+')),
    ('private_host_address', re.compile(r'(?i)\b(?:hostname|host_target|ssh_host|remote_host)\s*[=:]\s*[\x22\x27]?(?!(?:example|localhost|synthetic|placeholder|host\b|<))[A-Za-z0-9][A-Za-z0-9_.-]+')),
)


def _git(repo, *args):
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(repo), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=30, check=False)
    if result.returncode:
        raise ValueError('git_read_failed')
    return result.stdout


def _finding(path, category, line=0):
    return {'path': path, 'category': category, 'line': line}


def _safe_name(name):
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and all(p not in ('', '.', '..') for p in name.split('/')) and '\\' not in name


def _path_findings(name):
    parts = PurePosixPath(name).parts
    lower = [p.lower() for p in parts]
    if not _safe_name(name):
        return [_finding(name, 'unsafe_path')]
    if any(p in {'private_history', 'raw', 'results', 'environment', '.je_amd_authorization',
                 'models', 'build', 'build-diag'} or p.startswith('build-') for p in lower):
        return [_finding(name, 'private_or_generated_path')]
    base = lower[-1]
    if base in {'execution_approval.json', 'execution_authorized.json',
                'execution_approval.template.json'} or base.endswith(('.zip', '.gguf', '.so', '.bin', '.safetensors')):
        return [_finding(name, 'private_artifact')]
    if PurePosixPath(name).suffix.lower() not in _TEXT_SUFFIXES and base != '.gitignore':
        return [_finding(name, 'nonpublic_file_type')]
    return []


def _authorized_true(value):
    if isinstance(value, dict):
        return any((key == 'authorized' and item is True) or _authorized_true(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_authorized_true(item) for item in value)
    return False


def _content_findings(name, content):
    if b'\x00' in content:
        return [_finding(name, 'binary_content')]
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        return [_finding(name, 'non_utf8_content')]
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if any(ord(c) < 32 and c not in '\t\r' for c in line):
            findings.append(_finding(name, 'binary_content', number))
        for category, pattern in _RULES:
            if pattern.search(line):
                findings.append(_finding(name, category, number))
    if PurePosixPath(name).suffix.lower() == '.json':
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            findings.append(_finding(name, 'invalid_json'))
        else:
            if _authorized_true(value):
                findings.append(_finding(name, 'real_execution_approval'))
            if PurePosixPath(name).name == 'EXECUTION_APPROVAL.example.json' and (
                    not isinstance(value, dict) or value.get('authorized') is not False):
                findings.append(_finding(name, 'approval_example_must_be_false'))
    return findings


def _blob_findings(repo, name, mode, oid):
    findings = _path_findings(name)
    if mode not in ('100644', '100755'):
        return findings + [_finding(name, 'nonregular_blob')]
    size = int(_git(repo, 'cat-file', '-s', oid))
    if size > MAX_BYTES:
        return findings + [_finding(name, 'oversized_blob')]
    if findings:
        return findings
    return _content_findings(name, _git(repo, 'cat-file', 'blob', oid))


def _report(findings, checked, **fields):
    unique = sorted({(f['path'], f['category'], f['line']) for f in findings})
    return {'status': 'FAIL' if unique else 'PASS', 'files_checked': checked,
            'findings': [_finding(*row) for row in unique], **fields}


def audit_staged(repo, allowlist):
    """Check staged blobs against an explicit reviewed list; never stage files."""
    findings, checked = [], 0
    try:
        if not isinstance(allowlist, list) or not all(isinstance(p, str) and _safe_name(p) for p in allowlist):
            raise ValueError('invalid_allowlist')
        names = [p.decode('utf-8') for p in _git(repo, 'diff', '--cached', '--name-only', '-z', '--no-renames').split(b'\0') if p]
        if len(names) > MAX_FILES:
            raise ValueError('file_limit')
        entries = {}
        for row in _git(repo, 'ls-files', '--stage', '-z').split(b'\0'):
            if not row:
                continue
            meta, path = row.split(b'\t', 1)
            mode, oid, stage = meta.decode('ascii').split()
            name = path.decode('utf-8')
            if stage != '0' and name in names:
                findings.append(_finding(name, 'unmerged_index'))
            entries[name] = (mode, oid)
        for name in names:
            if name not in allowlist:
                findings.append(_finding(name, 'outside_allowlist'))
                continue
            if name not in entries:  # A staged deletion contains no new blob.
                continue
            mode, oid = entries[name]
            findings.extend(_blob_findings(repo, name, mode, oid))
            checked += 1
    except (ValueError, UnicodeError, subprocess.SubprocessError, OSError):
        findings.append(_finding('', 'audit_input_or_git_failure'))
    return _report(findings, checked)


def audit_history(repo, base, head):
    """Check every added/modified blob in base..head, including deleted secrets.

    At most 1024 commits and 4096 distinct path/blob pairs are inspected. Merge
    parent differences are checked as well. No checkout or code execution occurs.
    """
    findings, checked, seen = [], 0, set()
    try:
        base_oid = _git(repo, 'rev-parse', '--verify', '--end-of-options', str(base) + '^{commit}').decode().strip()
        head_oid = _git(repo, 'rev-parse', '--verify', '--end-of-options', str(head) + '^{commit}').decode().strip()
        _git(repo, 'merge-base', '--is-ancestor', base_oid, head_oid)
        commits = _git(repo, 'rev-list', '--max-count=' + str(MAX_COMMITS + 1), base_oid + '..' + head_oid).decode().splitlines()
        if len(commits) > MAX_COMMITS:
            raise ValueError('commit_limit')
        for commit in commits:
            rows = _git(repo, 'diff-tree', '--root', '-m', '--no-commit-id', '--raw', '-z', '-r', '--no-renames',
                        '--diff-filter=AMT', commit).split(b'\0')
            for index in range(0, len(rows) - 1, 2):
                meta, name = rows[index], rows[index + 1].decode('utf-8')
                oldmode, mode, oldoid, oid, status = meta.decode('ascii').split()
                key = (name, mode, oid)
                if key in seen:
                    continue
                seen.add(key)
                if len(seen) > MAX_FILES:
                    raise ValueError('file_limit')
                findings.extend(_blob_findings(repo, name, mode, oid))
                checked += 1
    except (ValueError, UnicodeError, subprocess.SubprocessError, OSError):
        findings.append(_finding('', 'audit_input_or_git_failure'))
    return _report(findings, checked)
