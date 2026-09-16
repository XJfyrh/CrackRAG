"""Capture management commands without Docker, databases, or provider requests."""
from contextlib import redirect_stdout
import io
import importlib.util
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
            def __init__(self, command):
                self.stdout = []
                if '-run' in command:
                    pattern = command[command.index('-run') + 1]
                    name = pattern.replace('^', '').replace('$', '')
                    self.stdout = [json.dumps({'Action': 'pass', 'Package': 'crackrag/api/internal/app', 'Test': name}) + '\n']

            def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as folder:
            # Execute an isolated copy: even the test log goes to this temp root.
            target = Path(folder) / 'scripts/check_go.py'
            fixture = Path(folder) / 'api/internal/app/lifecycle_test.go'
            fixture.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / 'api/internal/app/lifecycle_test.go', fixture)
            env = {'COMPOSE_PROJECT_NAME': 'unrelated-host-project',
                   'CRACKRAG_PROJECT': 'crackrag-release-not-tests'}
            with patch.dict(os.environ, env), patch('subprocess.run', capture_run), \
                    patch('subprocess.Popen', side_effect=lambda command, **kwargs: FinishedGo(command)) as go, \
                    redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit_result:
                exec(compile((ROOT / 'scripts/check_go.py').read_text(), str(target), 'exec'),
                     {'__file__': str(target), '__name__': '__main__'})
            self.assertEqual(exit_result.exception.code, 0)
            self.assertTrue(any('DROP DATABASE' in part for call in calls for part in call))
            self.assert_project([call[1:] for call in calls], 'crackrag-release-tests')
            self.assertEqual(go.call_count, 8)
            self.assertIn('-skip', go.call_args_list[0].args[0])
            self.assertTrue(all('-run' in call.args[0] for call in go.call_args_list[1:]))
            self.assertEqual(sum('DROP DATABASE IF EXISTS crackrag_m1_test WITH (FORCE)' in call for call in calls), 8)

    def test_go_isolation_coverage_fails_closed(self):
        spec = importlib.util.spec_from_file_location('check_go', ROOT / 'scripts/check_go.py')
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        source = (ROOT / 'api/internal/app/lifecycle_test.go').read_text(encoding='utf-8')
        self.assertEqual(runner.strict_cases(source), runner.STRICT_CASES)
        for replacement in ('future_negative_case', 'in_flight_request'):
            with self.assertRaises(ValueError):
                runner.strict_cases(source.replace('"usage_present", func(', f'"{replacement}", func('))
        for extra in ('future_case_2', 'FutureCase', 'future-case'):
            with self.assertRaises(ValueError):
                runner.strict_cases(source.replace('{"usage_present", func(',
                    '{"' + extra + '", func(map[string]any) {}, false},\n{"usage_present", func('))
        leaf = runner.STRICT_CASES[-1]
        event = {'Package': 'crackrag/api/internal/app', 'Test': runner.STRICT_PARENT + '/' + leaf, 'Action': 'pass'}
        runner.require_leaf_pass([event], leaf)
        for events in ([], [event, event], [{**event, 'Action': 'skip'}], [{**event, 'Action': 'fail'}],
                       [event, {**event, 'Test': runner.STRICT_PARENT + '/in_flight_request'}]):
            with self.assertRaises(ValueError):
                runner.require_leaf_pass(events, leaf)
        summary = runner.summarize([event, {**event, 'Action': 'fail'}, event])
        self.assertEqual(summary['unique_tests_including_subtests'], {'pass': 0, 'skip': 0, 'fail': 1})


if __name__ == '__main__':
    unittest.main()
