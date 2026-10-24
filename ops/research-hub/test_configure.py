"""Private API helper: isolated files, no real credential, Docker or network."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import configure


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory, self.project = self.root/'config', self.root/'project'
        self.directory.mkdir(); self.project.mkdir()
        self.original_env = '# preserved comment\nOTHER="literal$with#quotes"\nEIA_API_KEY=old-fixture\n'
        self.original_settings = '{"sources":{"eia":false,"brave":true},"execution_mode":"paper"}\n'
        (self.directory/'hub.env').write_text(self.original_env)
        (self.directory/'hub.env').chmod(0o600)
        (self.directory/'settings.json').write_text(self.original_settings)
        (self.directory/'settings.json').chmod(0o640)
        self.payload = {'key': 'EIA_API_KEY', 'value': 'private-fixture-never-real'}
        # CI is non-root; the ARM64 isolated root container additionally checks
        # the real chown operation. No production path is mounted in either.
        owner = patch('configure.os.chown') if os.geteuid() else contextlib.nullcontext()
        owner.__enter__(); self.addCleanup(owner.__exit__, None, None, None)
        self.real_recreate = configure.recreate
        for name in ('ensure_idle', 'recreate', 'healthy'):
            p = patch('configure.'+name)
            setattr(self, name, p.start()); self.addCleanup(p.stop)

    def apply(self):
        return configure.apply(self.payload, self.directory, self.project)

    def assert_originals(self):
        self.assertEqual((self.directory/'hub.env').read_text(), self.original_env)
        self.assertEqual((self.directory/'settings.json').read_text(), self.original_settings)
        self.assertEqual((self.directory/'hub.env').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.directory/'settings.json').stat().st_mode & 0o777, 0o640)

    def test_success_preserves_other_credentials_and_backs_up_originals_privately(self):
        result = self.apply()
        self.assertIn('OTHER="literal$with#quotes"\n', (self.directory/'hub.env').read_text())
        settings = json.loads((self.directory/'settings.json').read_text())
        self.assertEqual(settings, {'sources': {'eia': True, 'brave': True}, 'execution_mode': 'paper'})
        backup = self.directory/'backups'/result['backup']
        self.assertEqual((backup/'hub.env').read_text(), self.original_env)
        self.assertEqual((backup/'settings.json').read_text(), self.original_settings)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual((backup/'hub.env').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.ensure_idle.call_count, 2)
        self.recreate.assert_called_once_with(self.project)
        self.healthy.assert_called_once()
        self.assertIs(result['service_healthy'], True)
        self.assertIs(result['external_access_validated'], False)
        self.assertNotIn(self.payload['value'], json.dumps(result))

    def test_busy_work_refuses_before_backup_or_mutation(self):
        self.ensure_idle.side_effect = RuntimeError('research_busy_retry_later')
        with self.assertRaisesRegex(RuntimeError, 'research_busy_retry_later'):
            self.apply()
        self.assert_originals()
        self.assertFalse((self.directory/'backups').exists())
        self.recreate.assert_not_called()

    def test_second_guard_keeps_files_unchanged_if_work_started_during_backup(self):
        self.ensure_idle.side_effect = [None, RuntimeError('research_busy_retry_later')]
        with self.assertRaises(configure.ConfigurationFailure) as failure:
            self.apply()
        self.assert_originals()
        self.assertIs(failure.exception.result['configuration_applied'], False)
        self.assertIs(failure.exception.result['previous_files_restored'], True)
        self.recreate.assert_not_called()

    def test_partial_file_write_restores_both_original_files(self):
        real_atomic = configure.atomic
        failed = False
        def write(path, *args):
            nonlocal failed
            if path.name == 'settings.json' and not failed:
                failed = True
                raise OSError('private-fixture-never-real')
            return real_atomic(path, *args)
        with patch('configure.atomic', side_effect=write), self.assertRaises(configure.ConfigurationFailure) as failure:
            self.apply()
        self.assert_originals()
        self.recreate.assert_not_called()
        self.assertNotIn(self.payload['value'], json.dumps(failure.exception.result))

    def test_recreate_failure_restores_files_then_recreates_previous_service(self):
        self.recreate.side_effect = [RuntimeError(self.payload['value']), None]
        with self.assertRaises(configure.ConfigurationFailure) as failure:
            self.apply()
        self.assert_originals()
        self.assertEqual(self.recreate.call_count, 2)
        self.healthy.assert_called_once()
        self.assertIs(failure.exception.result['service_restored'], True)
        self.assertNotIn(self.payload['value'], str(failure.exception))

    def test_unhealthy_new_service_rolls_back_and_checks_previous_service_health(self):
        self.healthy.side_effect = [RuntimeError('not_healthy'), None]
        with self.assertRaises(configure.ConfigurationFailure) as failure:
            self.apply()
        self.assert_originals()
        self.assertEqual(self.recreate.call_count, 2)
        self.assertEqual(self.healthy.call_count, 2)
        self.assertIs(failure.exception.result['service_restored'], True)

    def test_failed_service_rollback_is_reported_separately_from_file_restore(self):
        self.recreate.side_effect = RuntimeError('private-fixture-never-real')
        with self.assertRaises(configure.ConfigurationFailure) as failure:
            self.apply()
        self.assert_originals()
        self.assertIs(failure.exception.result['previous_files_restored'], True)
        self.assertIs(failure.exception.result['service_restored'], False)

    def test_unsupported_keys_formats_and_duplicate_env_fields_never_mutate(self):
        for value in [None, [], {'key': 'PRIVATE_KEY', 'value': 'x'},
                      {'key': 'EIA_API_KEY', 'value': 'x\nINJECT=value'},
                      {'key': 'EIA_API_KEY', 'value': ''}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                configure.apply(value, self.directory, self.project)
            self.assert_originals()
        self.ensure_idle.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'duplicate_configuration_key'):
            configure.env_update('EIA_API_KEY=a\nEIA_API_KEY=b\n', 'EIA_API_KEY', 'c')

    def test_recreate_targets_only_hub_and_preserves_executor_overlay(self):
        (self.project/'compose.executor.yaml').write_text('services: {}')
        with patch('configure.subprocess.run') as run:
            run.return_value.returncode = 0
            self.real_recreate(self.project)
        args = run.call_args.args[0]
        self.assertIn(str(self.project/'compose.executor.yaml'), args)
        self.assertEqual(args[-5:], ['up', '-d', '--no-deps', '--no-build', 'hub'])


if __name__ == '__main__':
    unittest.main()
