import json
import pytest


def _hex_str(addr) -> str:
    if hasattr(addr, "as_hex"):
        return addr.as_hex.lower()
    if isinstance(addr, bytes):
        return ("0x" + addr.hex()).lower()
    return str(addr).lower()


def test_partial_eval_dispute_lock_and_appellate_upgrade(direct_vm, direct_deploy, direct_alice, direct_bob):
    """
    Scenario 1:
    - Alice deposits 10000 native value via payable deposit_escrow() and creates a milestone.
    - Bob submits deliverable.
    - Mock web and LLM so C1 and C2 pass (score=75, PARTIAL_75, payout_bps=7500).
    - Assert direct_vm.run_validator() is True.
    - Assert finalize_and_settle reverts with 'Dispute window is still active' before epoch advancement.
    - Bob calls challenge_verdict with rebuttal URL.
    - Re-mock web + LLM so C1, C2, and C3 pass (score=100, FULL, payout_bps=10000).
    - Resolve dispute, settle immediately, and verify Bob's balance is 10000 and Alice's is 0.
    - Verify Bob executes native on-chain withdrawal via withdraw(amount) with CEI and balance updates.
    """
    direct_vm.sender = direct_alice
    contract = direct_deploy("contracts/milestone_release.py")

    # Alice deposits payable escrow
    direct_vm.sender = direct_alice
    direct_vm.value = 10000
    contract.deposit_escrow()
    direct_vm.value = 0
    assert contract.get_balance(direct_alice) == 10000

    rubric = [
        {"id": "C1", "description": "Mandatory smart contract code", "weight": 50, "mandatory": True},
        {"id": "C2", "description": "Unit test suite with full coverage", "weight": 25, "mandatory": False},
        {"id": "C3", "description": "Documentation and setup instructions", "weight": 25, "mandatory": False},
    ]

    m_id = contract.create_milestone(
        direct_bob,
        "Build Milestone Release primitive",
        json.dumps(rubric),
        10000,
        3,  # max_attempts
        5,  # dispute_window_blocks
    )
    assert m_id == 1
    assert contract.get_balance(direct_alice) == 0

    vault_summary = contract.get_vault_summary()
    assert vault_summary["total_escrow_locked"] == 10000
    assert vault_summary["milestone_count"] == 1

    # Bob submits deliverable
    direct_vm.sender = direct_bob
    contract.submit_deliverable(
        m_id,
        "https://github.com/example/milestone-release/pull/1",
        "Initial implementation ready",
    )

    # Mock web and LLM for partial evaluation (C1 and C2 pass)
    direct_vm.mock_web(
        r".*github\.com/example/milestone-release/pull/1.*",
        {"status": 200, "body": "Contract and test files committed in pull request."},
    )
    direct_vm.mock_llm(
        r".*objective.*milestone evaluation auditor.*",
        json.dumps({"met_ids": ["C1", "C2"], "rationale": "C1 and C2 verified. C3 documentation not found."}),
    )

    eval_result = contract.evaluate_deliverable(m_id)
    assert eval_result["tier"] == "PARTIAL_75"
    assert eval_result["payout_bps"] == 7500
    assert eval_result["score"] == 75
    assert eval_result["mandatory_met"] is True

    # Consensus validation succeeds
    assert direct_vm.run_validator() is True

    # finalize_and_settle must revert while dispute window is active
    with direct_vm.expect_revert("Dispute window is still active"):
        contract.finalize_and_settle(m_id)

    # Bob challenges the provisional verdict with rebuttal evidence
    rebuttal_url = "https://explorer-studio.genlayer.com/tx/0xabc123"
    contract.challenge_verdict(
        m_id,
        rebuttal_url,
        "Rebuttal: Documentation is in docs/ folder referenced in tx metadata",
    )

    m_record = contract.get_milestone(m_id)
    assert m_record["status"] == "DISPUTED"
    assert m_record["dispute_used"] is True
    assert m_record["rebuttal_url"] == rebuttal_url

    # Re-mock web & LLM for appellate dispute resolution: all criteria met
    direct_vm.clear_mocks()
    direct_vm.mock_web(
        r".*github\.com/example/milestone-release/pull/1.*",
        {"status": 200, "body": "Contract and test files committed in pull request."},
    )
    direct_vm.mock_web(
        r".*explorer-studio\.genlayer\.com/tx/0xabc123.*",
        {"status": 200, "body": "On-chain verification metadata proving documentation completeness."},
    )
    direct_vm.mock_llm(
        r".*objective.*milestone evaluation auditor.*",
        json.dumps({"met_ids": ["C1", "C2", "C3"], "rationale": "Rebuttal confirms documentation requirement met."}),
    )

    dispute_result = contract.resolve_dispute(m_id)
    assert dispute_result["tier"] == "FULL"
    assert dispute_result["payout_bps"] == 10000
    assert dispute_result["score"] == 100
    assert dispute_result["mandatory_met"] is True

    # Settle immediately (milestone is FINALIZED)
    settle_status = contract.finalize_and_settle(m_id)
    assert settle_status == "SETTLED"

    # Verify final payout balances in contract
    assert contract.get_balance(direct_bob) == 10000
    assert contract.get_balance(direct_alice) == 0
    assert contract.get_vault_summary()["total_escrow_locked"] == 0

    # Real payout withdrawal by Bob
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Withdrawal amount must be greater than 0"):
        contract.withdraw(0)

    with direct_vm.expect_revert("Insufficient balance to withdraw"):
        contract.withdraw(12000)

    # Partial withdrawal
    withdraw_res1 = contract.withdraw(4000)
    assert withdraw_res1["amount"] == 4000
    assert withdraw_res1["remaining_balance"] == 6000
    assert withdraw_res1["total_withdrawn"] == 4000
    assert contract.get_balance(direct_bob) == 6000
    assert contract.get_withdrawn_total(direct_bob) == 4000

    # Complete withdrawal
    withdraw_res2 = contract.withdraw(6000)
    assert withdraw_res2["amount"] == 6000
    assert withdraw_res2["remaining_balance"] == 0
    assert withdraw_res2["total_withdrawn"] == 10000
    assert contract.get_balance(direct_bob) == 0
    assert contract.get_withdrawn_total(direct_bob) == 10000


def test_mandatory_failure_rework_cycle_and_refund(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """
    Scenario 2:
    - Alice deploys contract and sets Charlie as non-partisan timelock keeper.
    - Milestone where optional criteria pass (50 weight) but mandatory C1 fails.
    - Verifies it forces REJECTED (0 bps).
    - Advance epoch past dispute window via non-partisan keeper -> REWORK_REQUIRED when attempts remain.
    - Contributor resubmits, fails again, window passes -> REFUNDED to Alice when attempts exhausted.
    - Alice withdraws refunded native balance.
    """
    direct_vm.sender = direct_alice
    contract = direct_deploy("contracts/milestone_release.py")

    # Set non-partisan timelock keeper
    contract.set_timelock_keeper(direct_charlie)
    assert contract.get_vault_summary()["timelock_keeper"].lower() == _hex_str(direct_charlie)

    # Alice deposits escrow
    direct_vm.sender = direct_alice
    direct_vm.value = 10000
    contract.deposit_escrow()
    direct_vm.value = 0

    rubric = [
        {"id": "C1", "description": "Mandatory security audit", "weight": 50, "mandatory": True},
        {"id": "C2", "description": "Performance benchmark", "weight": 25, "mandatory": False},
        {"id": "C3", "description": "API documentation", "weight": 25, "mandatory": False},
    ]

    m_id = contract.create_milestone(
        direct_bob,
        "Security-critical module",
        json.dumps(rubric),
        10000,
        2,  # max_attempts = 2
        3,  # dispute_window_blocks = 3
    )

    # Attempt 1: Bob submits
    direct_vm.sender = direct_bob
    contract.submit_deliverable(
        m_id,
        "https://github.com/example/security-module/pull/1",
        "First attempt with benchmarks and docs",
    )

    # Mock web & LLM: C2 and C3 pass (50 weight), but mandatory C1 fails
    direct_vm.mock_web(
        r".*github\.com/example/security-module/pull/.*",
        {"status": 200, "body": "Benchmarks and docs only, no audit report."},
    )
    direct_vm.mock_llm(
        r".*objective.*milestone evaluation auditor.*",
        json.dumps({"met_ids": ["C2", "C3"], "rationale": "Mandatory security audit C1 is missing."}),
    )

    res1 = contract.evaluate_deliverable(m_id)
    assert res1["mandatory_met"] is False
    assert res1["tier"] == "REJECTED"
    assert res1["payout_bps"] == 0

    # Advance epoch past dispute window (3 blocks) via keeper Charlie
    direct_vm.sender = direct_charlie
    contract.advance_epoch(4)

    # Attempt 1 settlement -> REWORK_REQUIRED (attempt 1 of 2)
    direct_vm.sender = direct_alice
    status1 = contract.finalize_and_settle(m_id)
    assert status1 == "REWORK_REQUIRED"
    record1 = contract.get_milestone(m_id)
    assert record1["status"] == "REWORK_REQUIRED"
    assert record1["dispute_used"] is False

    # Attempt 2: Bob resubmits
    direct_vm.sender = direct_bob
    contract.submit_deliverable(
        m_id,
        "https://github.com/example/security-module/pull/2",
        "Second attempt still missing audit",
    )
    record2 = contract.get_milestone(m_id)
    assert record2["attempts_used"] == 2

    # Re-evaluate attempt 2: fails again
    res2 = contract.evaluate_deliverable(m_id)
    assert res2["mandatory_met"] is False
    assert res2["tier"] == "REJECTED"
    assert res2["payout_bps"] == 0

    # Advance epoch past dispute window
    direct_vm.sender = direct_charlie
    contract.advance_epoch(4)

    # Attempt 2 settlement -> REFUNDED (all attempts exhausted)
    direct_vm.sender = direct_alice
    status2 = contract.finalize_and_settle(m_id)
    assert status2 == "REFUNDED"
    record_final = contract.get_milestone(m_id)
    assert record_final["status"] == "REFUNDED"

    # Escrow refunded to client Alice
    assert contract.get_balance(direct_alice) == 10000
    assert contract.get_balance(direct_bob) == 0

    # Alice withdraws her refunded escrow
    withdraw_res = contract.withdraw(10000)
    assert withdraw_res["amount"] == 10000
    assert contract.get_balance(direct_alice) == 0
    assert contract.get_withdrawn_total(direct_alice) == 10000


def test_advance_epoch_restrictions_and_waive_dispute_window(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """
    Scenario 3:
    - Tests access control on advance_epoch (only keeper can call).
    - Tests partisan keeper restriction: keeper cannot advance epoch if party to an active PROVISIONAL milestone.
    - Tests steps bounds (1 <= steps <= 50).
    - Tests early finalization via counterparty/mutual waiver (waive_dispute_window).
    """
    direct_vm.sender = direct_alice
    contract = direct_deploy("contracts/milestone_release.py")

    # Deployer is initial keeper
    summary = contract.get_vault_summary()
    assert summary["owner"].lower() == _hex_str(direct_alice)
    assert summary["timelock_keeper"].lower() == _hex_str(direct_alice)

    # Arbitrary caller (Bob) cannot advance epoch
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only timelock keeper can advance epoch"):
        contract.advance_epoch(1)

    # Alice deposits escrow and creates milestone
    direct_vm.sender = direct_alice
    direct_vm.value = 10000
    contract.deposit_escrow()
    direct_vm.value = 0

    rubric = [{"id": "C1", "description": "Core protocol", "weight": 100, "mandatory": True}]
    m_id = contract.create_milestone(direct_bob, "Core Protocol", json.dumps(rubric), 10000, 2, 10)

    # Bob submits deliverable
    direct_vm.sender = direct_bob
    contract.submit_deliverable(m_id, "https://github.com/example/core", "Code completed")

    direct_vm.mock_web(r".*github\.com/example/core.*", {"status": 200, "body": "Core protocol code"})
    direct_vm.mock_llm(r".*objective.*milestone evaluation auditor.*", json.dumps({"met_ids": ["C1"], "rationale": "C1 complete"}))

    contract.evaluate_deliverable(m_id)
    assert contract.get_milestone(m_id)["status"] == "PROVISIONAL"

    # Partisan keeper check: Alice is keeper AND client on milestone 1 (PROVISIONAL)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Partisan keeper cannot advance epoch while party to an active PROVISIONAL milestone"):
        contract.advance_epoch(5)

    # Alice sets Charlie as non-partisan keeper
    contract.set_timelock_keeper(direct_charlie)

    # Keeper bounds check
    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("Steps must be between 1 and 50"):
        contract.advance_epoch(0)
    with direct_vm.expect_revert("Steps must be between 1 and 50"):
        contract.advance_epoch(51)

    # Non-partisan keeper can advance epoch
    new_epoch = contract.advance_epoch(2)
    assert new_epoch >= 2

    # Milestone 1 is FULL payout (10000 bps) -> counterparty (client Alice) waiver allows early finalization
    # Contributor (Bob) waiving records waiver but doesn't finalize alone on 100% payout
    direct_vm.sender = direct_bob
    w_status1 = contract.waive_dispute_window(m_id)
    assert w_status1 == "WAIVER_RECORDED"
    assert contract.get_milestone(m_id)["status"] == "PROVISIONAL"

    # Client (Alice) waives -> triggers WAIVED_AND_FINALIZED
    direct_vm.sender = direct_alice
    w_status2 = contract.waive_dispute_window(m_id)
    assert w_status2 == "WAIVED_AND_FINALIZED"
    assert contract.get_milestone(m_id)["status"] == "FINALIZED"

    # Immediate settlement succeeds without having to advance epoch past window
    settle_res = contract.finalize_and_settle(m_id)
    assert settle_res == "SETTLED"
    assert contract.get_balance(direct_bob) == 10000


def test_value_backed_escrow_and_direct_creation(direct_vm, direct_deploy, direct_alice, direct_bob):
    """
    Scenario 4:
    - Deposit with 0 attached value reverts.
    - Deposit with attached value succeeds and increments balance.
    - Create milestone with attached value directly (without prior deposit) credits and locks escrow.
    """
    direct_vm.sender = direct_alice
    contract = direct_deploy("contracts/milestone_release.py")

    # Deposit with 0 attached value reverts
    direct_vm.value = 0
    with direct_vm.expect_revert("Must attach positive native value (gl.message.value > 0) to deposit_escrow"):
        contract.deposit_escrow()

    # Deposit with positive attached value succeeds
    direct_vm.value = 5000
    new_bal = contract.deposit_escrow()
    direct_vm.value = 0
    assert new_bal == 5000
    assert contract.get_balance(direct_alice) == 5000

    # Create milestone directly attaching additional native value
    rubric = [{"id": "C1", "description": "Deliverable", "weight": 100, "mandatory": True}]
    # Alice has 5000 in balance, attaches 5000 more with create_milestone to lock 10000 total escrow
    direct_vm.value = 5000
    m_id = contract.create_milestone(
        direct_bob,
        "Direct Value Milestone",
        json.dumps(rubric),
        10000,
        2,
        5,
    )
    direct_vm.value = 0
    assert m_id == 1
    assert contract.get_balance(direct_alice) == 0
    assert contract.get_vault_summary()["total_escrow_locked"] == 10000


def test_validator_disagreement(direct_vm, direct_deploy, direct_alice, direct_bob):
    """
    Scenario 5:
    - Verify direct_vm.run_validator() returns False if the validator's mocked LLM output
      produces a different payout tier than the leader's result.
    """
    direct_vm.sender = direct_alice
    contract = direct_deploy("contracts/milestone_release.py")

    direct_vm.value = 10000
    contract.deposit_escrow()
    direct_vm.value = 0

    rubric = [
        {"id": "C1", "description": "Core engine implementation", "weight": 50, "mandatory": True},
        {"id": "C2", "description": "Comprehensive tests", "weight": 50, "mandatory": False},
    ]

    m_id = contract.create_milestone(
        direct_bob,
        "Engine implementation",
        json.dumps(rubric),
        10000,
        3,
        5,
    )

    direct_vm.sender = direct_bob
    contract.submit_deliverable(
        m_id,
        "https://github.com/example/engine/pull/1",
        "Engine code and tests",
    )

    # Leader mock: C1 and C2 pass (score=100, FULL, 10000 bps)
    direct_vm.mock_web(
        r".*github\.com/example/engine/pull/1.*",
        {"status": 200, "body": "Engine codebase"},
    )
    direct_vm.mock_llm(
        r".*objective.*milestone evaluation auditor.*",
        json.dumps({"met_ids": ["C1", "C2"], "rationale": "Leader: Full implementation verified."}),
    )

    eval_result = contract.evaluate_deliverable(m_id)
    assert eval_result["tier"] == "FULL"
    assert eval_result["payout_bps"] == 10000

    # Swap LLM mock so the validator sees only C1 (score=50, PARTIAL_50, 5000 bps)
    direct_vm.clear_mocks()
    direct_vm.mock_web(
        r".*github\.com/example/engine/pull/1.*",
        {"status": 200, "body": "Engine codebase"},
    )
    direct_vm.mock_llm(
        r".*objective.*milestone evaluation auditor.*",
        json.dumps({"met_ids": ["C1"], "rationale": "Validator: Tests are missing, C2 failed."}),
    )

    # Validator must reject the leader's result due to tier and payout_bps mismatch
    validator_agreed = direct_vm.run_validator()
    assert validator_agreed is False


def test_guardrails_and_reverts(direct_vm, direct_deploy, direct_alice, direct_bob):
    """
    Scenario 6:
    - Input validation and deterministic guardrails.
    """
    direct_vm.sender = direct_alice
    contract = direct_deploy("contracts/milestone_release.py")

    # Deposit with 0 attached value reverts
    direct_vm.value = 0
    with direct_vm.expect_revert("Must attach positive native value (gl.message.value > 0) to deposit_escrow"):
        contract.deposit_escrow()

    # Deposit valid value
    direct_vm.value = 5000
    contract.deposit_escrow()
    direct_vm.value = 0

    # Client cannot be contributor
    with direct_vm.expect_revert("Contributor cannot be client"):
        contract.create_milestone(direct_alice, "Self-bounty", "[]", 5000, 3, 5)

    # Escrow amount greater than balance reverts
    valid_rubric = json.dumps([{"id": "C1", "description": "Mandatory task", "weight": 100, "mandatory": True}])
    with direct_vm.expect_revert("Insufficient balance in escrow deposit"):
        contract.create_milestone(direct_bob, "Overdrawn task", valid_rubric, 10000, 3, 5)

    # Rubric weights != 100 reverts
    bad_rubric = json.dumps([{"id": "C1", "description": "Task 1", "weight": 60, "mandatory": True}])
    with direct_vm.expect_revert("Rubric weights must sum to exactly 100"):
        contract.create_milestone(direct_bob, "Bad rubric milestone", bad_rubric, 5000, 3, 5)

    # Duplicate criterion IDs revert
    dup_rubric = json.dumps([
        {"id": "C1", "description": "Task 1", "weight": 50, "mandatory": True},
        {"id": "C1", "description": "Task 2", "weight": 50, "mandatory": False},
    ])
    with direct_vm.expect_revert("unique non-empty string"):
        contract.create_milestone(direct_bob, "Duplicate rubric milestone", dup_rubric, 5000, 3, 5)

    # Only owner can set timelock keeper
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only owner can set timelock keeper"):
        contract.set_timelock_keeper(direct_bob)

    # Create valid milestone
    direct_vm.sender = direct_alice
    m_id = contract.create_milestone(direct_bob, "Valid milestone project", valid_rubric, 5000, 2, 5)

    # Only contributor can submit deliverable
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Only contributor can submit deliverable"):
        contract.submit_deliverable(m_id, "https://github.com/deliverable", "Notes")

    # Invalid URL scheme reverts
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Evidence URL must start with https://"):
        contract.submit_deliverable(m_id, "http://insecure-site.com", "Notes")
