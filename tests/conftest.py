import pytest
import sys


@pytest.fixture
def direct_deploy(direct_deploy):
    def _wrapped(*args, **kwargs):
        contract = direct_deploy(*args, **kwargs)
        for name, mod in list(sys.modules.items()):
            if name.endswith("vm") and hasattr(mod, "run_nondet") and hasattr(mod, "run_nondet_unsafe"):
                mod.run_nondet_unsafe = mod.run_nondet
            if name == "genlayer" or name.endswith(".gl") or name == "gl":
                if hasattr(mod, "get_contract_at") and not hasattr(mod, "ContractAt"):
                    mod.ContractAt = mod.get_contract_at
                elif hasattr(mod, "contract") and hasattr(mod.contract, "get_at") and not hasattr(mod, "ContractAt"):
                    mod.ContractAt = mod.contract.get_at
        return contract

    return _wrapped


@pytest.fixture
def direct_vm(direct_vm):
    # Hook for gl_call to handle cross-contract calls / emit_transfer for EOA test addresses cleanly
    def _gl_call_hook(vm, request):
        if isinstance(request, dict):
            if "PostMessage" in request:
                post_data = request["PostMessage"]
                val = post_data.get("value", 0)
                recipient = post_data.get("address")
                if val and recipient:
                    addr_bytes = bytes(recipient) if hasattr(recipient, "__bytes__") else recipient
                    if hasattr(recipient, "as_bytes"):
                        addr_bytes = recipient.as_bytes
                    vm._balances[addr_bytes] = vm._balances.get(addr_bytes, 0) + int(val)
                return {"ok": None}
            if "EmitInternalMessage" in request:
                return {"ok": None}
        return None

    direct_vm._gl_call_hook = _gl_call_hook
    direct_vm.value = 0
    return direct_vm
