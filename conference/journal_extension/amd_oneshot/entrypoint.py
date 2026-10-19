"""Isolated portable entry point; never import a collaborator's shadow package."""
from pathlib import Path
import runpy
import sys
import types


def main():
    root = Path(__file__).resolve().parents[3]
    # The wrapper uses -I: caller cwd/PYTHONPATH/user site are not code inputs.
    # Pin the namespace too: an installed regular 'conference' package must not
    # supersede our bundled PEP420 source/dependency directory.
    sys.path.insert(0, str(root))
    namespace = types.ModuleType('conference')
    namespace.__path__ = [str(root / 'conference')]
    namespace.__package__ = 'conference'
    sys.modules['conference'] = namespace
    runpy.run_module('conference.journal_extension.amd_oneshot.cli', run_name='__main__')


if __name__ == '__main__':
    main()
