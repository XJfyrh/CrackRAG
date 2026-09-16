"""Scan exactly the Git publication set; never print detected secret values."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
patterns = {
    'provider credential': re.compile(rb'\bsk-[A-Za-z0-9_-]{24,}\b'),
    'GitHub credential': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'host user path': re.compile(rb'[A-Za-z]:[\\/]Users[\\/](?!Public[\\/])[^\s"\r\n]+', re.I),
}
allowed_pdf = {'web/public/samples/financial.pdf', 'web/public/samples/ambiguous.pdf'}
blocked_dirs = {'.release', 'backups', 'evidence', 'archive', 'node_modules', '.venv', '__pycache__'}
blocked_suffixes = {'.zip', '.dump', '.sqlite', '.sqlite3', '.docx', '.bundle', '.exe', '.dll', '.tar', '.webm', '.mp4'}

def main():
    names = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT).decode().split('\0')
    failures = []
    count = 0
    for name in sorted(set(names) - {''}):
        p = ROOT / name
        if not p.exists():
            failures.append((name, 'tracked file missing')); continue
        count += 1
        if p.is_symlink() or not p.resolve().is_relative_to(ROOT):
            failures.append((name, 'symlink or outside source tree')); continue
        if blocked_dirs.intersection(p.relative_to(ROOT).parts) or p.suffix.lower() in blocked_suffixes or (p.suffix.lower() == '.pdf' and name not in allowed_pdf):
            failures.append((name, 'private/generated/third-party artifact')); continue
        if p.name.startswith('.env') and p.name != '.env.example':
            failures.append((name, 'environment file')); continue
        if p.stat().st_size > 5_000_000:
            failures.append((name, 'oversized source artifact')); continue
        data = p.read_bytes()
        for label, pattern in patterns.items():
            if pattern.search(data): failures.append((name, label))
    for name, reason in failures:
        print(f'FAIL {name}: {reason}')
    print(f'Public-source scan: {count} files; {len(failures)} findings. This is a heuristic check plus an explicit artifact allowlist, not proof of no secrets.')
    return bool(failures)

if __name__ == '__main__': sys.exit(main())
