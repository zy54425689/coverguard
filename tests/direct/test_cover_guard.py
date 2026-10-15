# -*- coding: utf-8 -*-
"""CoverGuard direct-mode tests — parametric cover with real custody and real payouts.

Run: pytest tests/direct/ -v
"""
import json


def _deploy(direct_deploy):
    import pathlib
    import tempfile

    src = pathlib.Path(__file__).resolve().parents[2] / "contracts" / "cover_guard.py"
    code = src.read_text(encoding="utf-8")
    tmp = pathlib.Path(tempfile.gettempdir()) / "cover_guard_local.py"
    tmp.write_text(code, encoding="utf-8")
    return direct_deploy(str(tmp))


def _addr(v):
    from genlayer import Address
    return Address(v)


def _hex(v) -> str:
    if isinstance(v, str):
        return v.lower().replace("0x", "")
    for attr in ("as_bytes", "_as_bytes"):
        if hasattr(v, attr):
            b = getattr(v, attr)
            if callable(b):
                b = b()
            return b.hex()
    return bytes(v).hex()


def _open_policy(direct_vm, contract, holder, underwriter, premium=100,
                 url="https://example.com/rainfall", cond="the page reports rainfall above 50mm"):
    direct_vm.sender = holder
    direct_vm.value = premium
    pid = contract.create_policy(_addr(underwriter), url, cond)
    direct_vm.value = 0
    return pid


def _activate(direct_vm, contract, underwriter, pid, collateral=500):
    direct_vm.sender = underwriter
    direct_vm.value = collateral
    contract.underwrite_policy(pid)
    direct_vm.value = 0


# ------------------------------------------------- 1. premium custody ----

def test_create_requires_premium(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with direct_vm.expect_revert("Premium must be funded"):
        contract.create_policy(_addr(direct_bob), "https://example.com/x", "condition")


def test_premium_recorded_as_custody(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _open_policy(direct_vm, contract, direct_alice, direct_bob, premium=250)
    p = contract.get_policy(pid)
    assert int(p["premium"]) == 250
    assert int(p["coverage"]) == 0
    assert int(p["state"]) == 0            # awaiting underwriter
    assert p["paid"] is False
    assert _hex(p["holder"]) == direct_alice.hex()


def test_create_validation(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = 100
    with direct_vm.expect_revert("A valid hazard data-source URL is required"):
        contract.create_policy(_addr(direct_bob), "not-a-url", "condition")
    with direct_vm.expect_revert("A trigger condition is required"):
        contract.create_policy(_addr(direct_bob), "https://example.com/x", "  ")
    with direct_vm.expect_revert("Holder and underwriter must differ"):
        contract.create_policy(_addr(direct_alice), "https://example.com/x", "condition")
    direct_vm.value = 0


# ------------------------------------------------- 2. underwriting gate ----

def test_only_named_underwriter_can_underwrite(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    contract = _deploy(direct_deploy)
    pid = _open_policy(direct_vm, contract, direct_alice, direct_bob)

    direct_vm.sender = direct_charlie
    direct_vm.value = 500
    with direct_vm.expect_revert("Only the named underwriter can take this policy"):
        contract.underwrite_policy(pid)
    direct_vm.value = 0

    direct_vm.sender = direct_bob
    direct_vm.value = 500
    contract.underwrite_policy(pid)
    direct_vm.value = 0

    p = contract.get_policy(pid)
    assert int(p["coverage"]) == 500
    assert int(p["state"]) == 1            # ACTIVE


def test_collateral_must_exceed_premium(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _open_policy(direct_vm, contract, direct_alice, direct_bob, premium=1000)

    direct_vm.sender = direct_bob
    direct_vm.value = 900                  # less than premium
    with direct_vm.expect_revert("Collateral must exceed the premium collected"):
        contract.underwrite_policy(pid)
    direct_vm.value = 0


def test_holder_refund_when_not_underwritten(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _open_policy(direct_vm, contract, direct_alice, direct_bob, premium=300)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the holder can refund"):
        contract.refund_unaccepted(pid)

    direct_vm.sender = direct_alice
    contract.refund_unaccepted(pid)
    p = contract.get_policy(pid)
    assert int(p["state"]) == 4            # CANCELLED
    assert int(p["premium"]) == 0
    assert p["paid"] is True


# ------------------------------------------------- 3. judging + payouts ----

def test_trigger_pays_the_holder(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _open_policy(direct_vm, contract, direct_alice, direct_bob, premium=100)
    _activate(direct_vm, contract, direct_bob, pid, collateral=900)

    direct_vm.mock_web(r".*example\.com/rainfall.*",
                       {"status": 200, "body": "<html>Rainfall today: 78mm - flood warning in effect</html>"})
    direct_vm.mock_llm(r".*", {"triggered": True,
                               "reason": "The fetched page reports 78mm of rainfall, which exceeds the 50mm trigger.",
                               "confidence": "high"})

    contract.judge_claim(pid)
    p = contract.get_policy(pid)
    assert p["triggered"] is True
    assert int(p["state"]) == 2
    assert "78mm" in p["ruling_reason"]

    contract.settle(pid)
    p = contract.get_policy(pid)
    assert int(p["state"]) == 3            # SETTLED
    assert p["paid"] is True
    assert int(p["coverage"]) == 0         # escrow released, not metadata
    assert int(p["premium"]) == 0


def test_no_trigger_pays_the_underwriter(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _open_policy(direct_vm, contract, direct_alice, direct_bob, premium=100)
    _activate(direct_vm, contract, direct_bob, pid, collateral=900)

    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>Sunny all day. Rainfall: 0mm</html>"})
    direct_vm.mock_llm(r".*", {"triggered": False,
                               "reason": "The page reports 0mm of rain, so the 50mm trigger did not fire.",
                               "confidence": "high"})
    contract.judge_claim(pid)

    p = contract.get_policy(pid)
    assert p["triggered"] is False

    contract.settle(pid)
    p = contract.get_policy(pid)
    assert int(p["state"]) == 3
    assert p["paid"] is True
    assert int(p["coverage"]) == 0
    assert int(p["premium"]) == 0


def test_unreachable_source_means_no_trigger(direct_vm, direct_deploy, direct_alice, direct_bob):
    """A dead/empty source must not pay out - conservative by design."""
    contract = _deploy(direct_deploy)
    pid = _open_policy(direct_vm, contract, direct_alice, direct_bob)
    _activate(direct_vm, contract, direct_bob, pid)

    direct_vm.mock_web(r".*", {"status": 502, "body": ""})
    direct_vm.mock_llm(r".*", {"triggered": False,
                               "reason": "The source could not be read, so the trigger cannot be confirmed.",
                               "confidence": "medium"})
    contract.judge_claim(pid)
    assert contract.get_policy(pid)["triggered"] is False


# ------------------------------------------------- 4. guards ----

def test_settle_guards(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _open_policy(direct_vm, contract, direct_alice, direct_bob)
    _activate(direct_vm, contract, direct_bob, pid)

    with direct_vm.expect_revert("Claim has not been judged yet"):
        contract.settle(pid)

    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>ok</html>"})
    direct_vm.mock_llm(r".*", {"triggered": True, "reason": "trigger met", "confidence": "low"})
    contract.judge_claim(pid)
    contract.settle(pid)

    with direct_vm.expect_revert("Policy already settled"):
        contract.settle(pid)


def test_cannot_judge_inactive_policy(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _open_policy(direct_vm, contract, direct_alice, direct_bob)
    with direct_vm.expect_revert("Policy is not active"):
        contract.judge_claim(pid)


def test_cannot_underwrite_twice(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _open_policy(direct_vm, contract, direct_alice, direct_bob)
    _activate(direct_vm, contract, direct_bob, pid)

    direct_vm.sender = direct_bob
    direct_vm.value = 800
    with direct_vm.expect_revert("Policy is not awaiting an underwriter"):
        contract.underwrite_policy(pid)
    direct_vm.value = 0
