import pytest
import sys


@pytest.fixture
def direct_deploy(direct_deploy):
    def _wrapped(*args, **kwargs):
        contract = direct_deploy(*args, **kwargs)
        for name, mod in list(sys.modules.items()):
            if name.endswith("vm") and hasattr(mod, "run_nondet") and hasattr(mod, "run_nondet_unsafe"):
                mod.run_nondet_unsafe = mod.run_nondet
        return contract

    return _wrapped
