"""Run offline regressions with user-data paths isolated before app imports."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix='slt-regression-') as scratch:
        os.environ.update(HOME=scratch, USERPROFILE=scratch, XDG_DATA_HOME=scratch,
                          XDG_CONFIG_HOME=scratch, APPDATA=scratch, LOCALAPPDATA=scratch,
                          STREAMLIT_BROWSER_GATHER_USAGE_STATS='false')
        with patch('requests.adapters.HTTPAdapter.send', side_effect=AssertionError('Real HTTP forbidden in regression tests')), patch('urllib.request.urlopen', side_effect=AssertionError('Real HTTP forbidden in regression tests')):
            suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
            result = unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
