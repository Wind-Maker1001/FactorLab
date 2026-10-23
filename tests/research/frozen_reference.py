"""Load immutable upstream math with only two reviewed import-name adapters.

The original source and its hash stay untouched. No Qbot package is imported,
and the temporary import hook exists only while this frozen test module loads.
"""
import builtins
import hashlib
from pathlib import Path
import sys
from unittest.mock import patch
from types import ModuleType

PATH = Path(__file__).parent/'reference/factor_reference.py'
FROZEN_SHA256 = '160c9869f962b28434ff7124418463e7e2530bad3f006d3879660ce1cfdab2e9'
body = PATH.read_bytes()
if hashlib.sha256(body).hexdigest()!=FROZEN_SHA256:
    raise ValueError('frozen reference source identity mismatch')
IMPORTS = {'qbot.research.data.calendar':'factorlab.data.calendar',
           'qbot.research.errors':'factorlab.errors'}
original_import = builtins.__import__


def adapted_import(name, globals=None, locals=None, fromlist=(), level=0):
    if (name=='qbot' or name.startswith('qbot.')) and name not in IMPORTS:
        raise ImportError('unreviewed upstream reference import: '+name)
    return original_import(IMPORTS.get(name,name), globals, locals, fromlist, level)


name = 'tests.research._immutable_factor_reference'
reference = ModuleType(name)
reference.__file__ = str(PATH)
sys.modules[name] = reference
with patch('builtins.__import__',adapted_import):
    exec(compile(body,str(PATH),'exec'),reference.__dict__)
