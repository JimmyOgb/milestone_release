import json
import pytest


def test_partial_eval_dispute_lock_and_appellate_upgrade(direct_vm, direct_deploy, direct_alice, direct_bob):
    """
    Scenario 1:
    - Alice deposits 10000 and creates a milestone with 3 criteria (C1=50 mandatory, C2=25, C3=25).
    - Bob submits deliverable.
    - Mock web and LLM so C1 and C2 pass (score=75, PARTIAL_75, payout_bps=7500).
    - Assert direct_vm.run_validator() is True.
    - Assert finalize_and_settle reverts with 'Dispute window is still active' before epoch advancement.
    - Bob calls challenge_verdict with rebuttal URL.
    - Re-mock web + LLM so C1, C2, and C3 pass (score=100, FULL, payout_bps=10000).
    - Resolve dispute, settle immediately, and verify Bob's balance is 10000 and Alice's is 0.
    """
    contract = direct_deploy("contracts/milestone_release.py")

    # Alice deposits escrow
    direct_vm.sender = direct_alice
    contract.deposit_escrow(10000)
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

    # Verify final payout balances
    assert contract.get_balance(direct_bob) == 10000
    assert contract.get_balance(direct_alice) == 0


def test_mandatory_failure_rework_cycle_and_refund(direct_vm, direct_deploy, direct_alice, direct_bob):
    """
    Scenario 2:
    - Milestone where optional criteria pass (50 weight) but mandatory C1 fails.
    - Verifies it forces REJECTED (0 bps).
    - Advance epoch past dispute window -> REWORK_REQUIRED when attempts remain.
    - Contributor resubmits, fails again, window passes -> REFUNDED to Alice when attempts exhausted.
    """
    contract = direct_deploy("contracts/milestone_release.py")

    direct_vm.sender = direct_alice
    contract.deposit_escrow(10000)

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

    # Advance epoch past dispute window (3 blocks)
    contract.advance_epoch(4)

    # Attempt 1 settlement -> REWORK_REQUIRED (attempt 1 of 2)
    status1 = contract.finalize_and_settle(m_id)
    assert status1 == "REWORK_REQUIRED"
    record1 = contract.get_milestone(m_id)
    assert record1["status"] == "REWORK_REQUIRED"
    assert record1["dispute_used"] is False

    # Attempt 2: Bob resubmits
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
    contract.advance_epoch(4)

    # Attempt 2 settlement -> REFUNDED (all attempts exhausted)
    status2 = contract.finalize_and_settle(m_id)
    assert status2 == "REFUNDED"
    record_final = contract.get_milestone(m_id)
    assert record_final["status"] == "REFUNDED"

    # Escrow refunded to client Alice
    assert contract.get_balance(direct_alice) == 10000
    assert contract.get_balance(direct_bob) == 0


def test_validator_disagreement(direct_vm, direct_deploy, direct_alice, direct_bob):
    """
    Scenario 3:
    - Verify direct_vm.run_validator() returns False if the validator's mocked LLM output
      produces a different payout tier than the leader's result.
    """
    contract = direct_deploy("contracts/milestone_release.py")

    direct_vm.sender = direct_alice
    contract.deposit_escrow(10000)

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
    Scenario 4:
    - Input validation and deterministic guardrails.
    """
    contract = direct_deploy("contracts/milestone_release.py")
    direct_vm.sender = direct_alice

    # Deposit with 0 amount reverts
    with direct_vm.expect_revert("Deposit amount must be greater than 0"):
        contract.deposit_escrow(0)

    contract.deposit_escrow(5000)

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

    # Create valid milestone
    m_id = contract.create_milestone(direct_bob, "Valid milestone project", valid_rubric, 5000, 2, 5)

    # Only contributor can submit deliverable
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Only contributor can submit deliverable"):
        contract.submit_deliverable(m_id, "https://github.com/deliverable", "Notes")

    # Invalid URL scheme reverts
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Evidence URL must start with https://"):
        contract.submit_deliverable(m_id, "http://insecure-site.com", "Notes")
