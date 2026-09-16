"""Capture management commands without Docker, databases, or provider requests."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ReleaseEntrypointsTest(unittest.TestCase):
    def assert_project(self, calls, expected):
        self.assertTrue(calls)
        for arguments in calls:
            self.assertEqual(arguments[0], 'compose')
            self.assertEqual(arguments.count('--project-name'), 1)
            self.assertEqual(arguments[arguments.index('--project-name') + 1], expected)

    def exercise_entrypoint(self, shell, powershell=False):
        for project in (None, 'crackrag-release-entrypoint-test'):
            for ready in (False, True):
                with self.subTest(project=project, ready=ready), tempfile.TemporaryDirectory() as folder:
                    work = Path(folder)
                    state = work / 'release state'
                    state.mkdir()
                    if ready:
                        # No secrets are needed: Docker is replaced by a recorder.
                        (state / 'compose.env').write_text('CRACKRAG_PROVIDER=mock\n')
                    capture = work / 'capture'
                    env = dict(os.environ)
                    env.pop('CRACKRAG_PROJECT', None)
                    env.update(CRACKRAG_STATE=state.as_posix(),
                               COMPOSE_PROJECT_NAME='unrelated-host-project',
                               RELEASE_CAPTURE=capture.as_posix())
                    if project:
                        env['CRACKRAG_PROJECT'] = project
                    action = 'stop' if ready else 'build'
                    if powershell:
                        target = str(ROOT / 'scripts/release.ps1').replace("'", "''")
                        code = (
                            'function docker { '
                            '[IO.File]::AppendAllText($env:RELEASE_CAPTURE, '
                            '((ConvertTo-Json -Compress -InputObject @($args)) + "`n")); '
                            '$global:LASTEXITCODE=0 }; '
                            f"& '{target}' {action}"
                        )
                        command = [shell, '-NoProfile', '-NonInteractive', '-Command', code]
                    else:
                        fake = work / 'docker'
                        fake.write_text('#!/bin/sh\n'
                                        'printf "%s\\0" "$@" >> "$RELEASE_CAPTURE"\n'
                                        'printf "\\n" >> "$RELEASE_CAPTURE"\n', newline='\n')
                        fake.chmod(0o755)
                        env['PATH'] = str(work) + os.pathsep + env['PATH']
                        command = [shell, str(ROOT / 'scripts/release.sh'), action]
                    subprocess.run(command, env=env, check=True, capture_output=True, text=True)
                    if powershell:
                        calls = [json.loads(line) for line in capture.read_text(encoding='utf-8-sig').splitlines()]
                    else:
                        calls = [line.decode().rstrip('\0').split('\0')
                                 for line in capture.read_bytes().splitlines()]
                    self.assert_project(calls, project or 'crackrag-release')
                    self.assertEqual(len(calls), 2 if ready else 1)
                    self.assertEqual('--env-file' in calls[0], ready)
                    if ready:
                        self.assertEqual(calls[0][-1], 'pause')
                        self.assertEqual(calls[1][-7:], ['stop', 'api', 'runtime', 'api2', 'runtime2', 'redis', 'postgres'])
                    else:
                        self.assertEqual(calls[0][-2:], ['build', 'admin'])

    def test_shell_project_wins_over_inherited_compose_project(self):
        shell = shutil.which('sh') if os.name != 'nt' else 'C:/Program Files/Git/bin/bash.exe'
        if not shell or not Path(shell).exists():
            self.skipTest('POSIX shell unavailable')
        self.exercise_entrypoint(shell)

    def test_powershell_project_wins_over_inherited_compose_project(self):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if not shell:
            self.skipTest('PowerShell unavailable')
        self.exercise_entrypoint(shell, powershell=True)

    def test_go_database_reset_always_uses_dedicated_test_project(self):
        calls = []

        def capture_run(command, **_):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        class FinishedGo:
            stdout = []

            def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as folder:
            # Execute an isolated copy: even the test log goes to this temp root.
            target = Path(folder) / 'scripts/check_go.py'
            env = {'COMPOSE_PROJECT_NAME': 'unrelated-host-project',
                   'CRACKRAG_PROJECT': 'crackrag-release-not-tests'}
            with patch.dict(os.environ, env), patch('subprocess.run', capture_run), \
                    patch('subprocess.Popen', return_value=FinishedGo()) as go, \
                    redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit_result:
                exec(compile((ROOT / 'scripts/check_go.py').read_text(), str(target), 'exec'),
                     {'__file__': str(target), '__name__': '__main__'})
            self.assertEqual(exit_result.exception.code, 0)
            self.assertTrue(any('DROP DATABASE' in part for call in calls for part in call))
            self.assert_project([call[1:] for call in calls], 'crackrag-release-tests')
            go.assert_called_once()


if __name__ == '__main__':
    unittest.main()
