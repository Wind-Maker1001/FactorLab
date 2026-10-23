"""Load immutable upstream math with only two reviewed import-name adapters.

The original source and its hash stay untouched. No Qbot package is imported,
and the temporary import hook exists only while this frozen test module loads.
"""
import builtins
import hashlib
import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch

PATH = Path(__file__).parent/'reference/factor_reference.py'
FROZEN_SHA256 = '160c9869f962b28434ff7124418463e7e2530bad3f006d3879660ce1cfdab2e9'
assert hashlib.sha256(PATH.read_bytes()).hexdigest()==FROZEN_SHA256
IMPORTS = {'qbot.research.data.calendar':'factorlab.data.calendar',
           'qbot.research.errors':'factorlab.errors'}
original_import = builtins.__import__


def adapted_import(name, globals=None, locals=None, fromlist=(), level=0):
    return original_import(IMPORTS.get(name,name), globals, locals, fromlist, level)


name = 'tests.research._immutable_factor_reference'
spec = importlib.util.spec_from_file_location(name,PATH)
reference = importlib.util.module_from_spec(spec)
sys.modules[name] = reference
with patch('builtins.__import__',adapted_import):
    spec.loader.exec_module(reference)
