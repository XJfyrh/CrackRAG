"""Run public offline suites; print exact skipped cases instead of counting them as passes."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'ai-runtime/src'), str(ROOT)]
suite = unittest.TestSuite()
for folder in ['tests', 'eval/m1/tests', 'eval/m2/tests', 'eval/m3/tests', 'eval/m4/tests', 'eval/release/tests']:
    # Separate loaders allow non-package directories with historical test module names.
    loader = unittest.TestLoader()
    suite.addTests(loader.discover(str(ROOT / folder), pattern='test_*.py'))
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(f'Executed={result.testsRun}; passed={result.testsRun-len(result.errors)-len(result.failures)-len(result.skipped)}; skipped={len(result.skipped)}')
sys.exit(not result.wasSuccessful())
