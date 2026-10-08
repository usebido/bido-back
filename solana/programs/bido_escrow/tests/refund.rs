//! `refund` (spec 01 section 3.5 row `refund`, section 3.8 `PoRefunded`; T18; ESC-AC-13; I2-I4):
//! once `refund_after` has passed, the buyer takes back exactly `total − released` into
//! ATA(buyer, mint), every pending milestone is cancelled and the escrow is `Refunded` for good,
//! so a later `release` of any milestone fails with `EscrowClosed`. A refund one second early, by
//! anyone but the buyer, of an unfunded, completed or refunded escrow, with nothing left, or into
//! any account but the buyer's ATA fails with its own error and moves no tokens.

mod common;

use {
    anchor_lang::{
        error::ErrorCode,
        prelude::{Clock, Pubkey},
        AccountSerialize,
    },
    bido_escrow::{EscrowError, EscrowStatus, MilestoneStatus, PoEscrow, PoRefunded},
    common::*,
    solana_signer::Signer,
};

/// Tool start's amount: 30% of the fixture total, 360,000 tUSD in base units.
const TOOL_START_AMOUNT: u64 = 360_000_000_000;
/// Slot at which the fixture refund lands.
const REFUND_SLOT: u64 = 5_000;

/// The account bytes `escrow` serializes to.
fn bytes(escrow: &PoEscrow) -> Vec<u8> {
    let mut out = Vec::new();
    escrow.try_serialize(&mut out).expect("serialize PoEscrow");
    out
}

/// Rewrites the stored `escrow` account with `stored`, to reach a state no instruction produces.
fn set_escrow(h: &mut Harness, escrow: &Pubkey, stored: &PoEscrow) {
    let mut account = h.svm.get_account(escrow).expect("escrow account");
    let mut data = account.data.as_mut_slice();
    stored.try_serialize(&mut data).expect("serialize PoEscrow");
    h.svm.set_account(*escrow, account).expect("set escrow");
}

/// Moves the clock to `escrow`'s `refund_after`, the first second a refund is allowed.
fn warp_to_deadline(h: &mut Harness, escrow: &Pubkey) {
    let refund_after = h.escrow(escrow).refund_after;
    let now = h.now();
    h.warp(refund_after - now);
    assert_eq!(h.now(), refund_after);
}

/// The fixture PO, funded, with Tool start released by the buyer's gate-0 approval.
fn funded_with_tool_start_released(h: &mut Harness) -> Pubkey {
    let escrow = h.create_po();
    h.fund(&escrow);
    h.approve(&escrow, 0);
    let stored = h.escrow(&escrow);
    assert_eq!(stored.milestones[0].status, MilestoneStatus::Released);
    assert_eq!(stored.released, TOOL_START_AMOUNT);
    escrow
}

#[test]
fn t18_refund_before_the_deadline_fails_with_refund_too_early() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    let funded = h.escrow(&escrow);
    let buyer = h.buyer.pubkey();
    let buyer_token = h.ata(&buyer);
    let vault = h.ata(&escrow);

    // Right after funding, then one second before `refund_after`.
    let tx = h.refund_tx(h.refund_accounts(&escrow, &buyer), &h.buyer);
    h.expect_err(tx, EscrowError::RefundTooEarly);
    h.warp(funded.refund_after - h.now() - 1);
    let tx = h.refund_tx(h.refund_accounts(&escrow, &buyer), &h.buyer);
    h.expect_err(tx, EscrowError::RefundTooEarly);
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&funded));
    assert_eq!(h.token_balance(&vault), TOTAL);
    assert_eq!(h.token_balance(&buyer_token), 0);

    // At `refund_after` itself the refund goes through (`now >= refund_after`).
    h.warp(1);
    assert_eq!(h.now(), funded.refund_after);
    let tx = h.refund_tx(h.refund_accounts(&escrow, &buyer), &h.buyer);
    h.send_ok(tx);
    assert_eq!(h.escrow(&escrow).status, EscrowStatus::Refunded);
    assert_eq!(h.token_balance(&vault), 0);
    assert_eq!(h.token_balance(&buyer_token), TOTAL);
}

#[test]
fn t18_refund_by_a_non_buyer_fails_with_unauthorized_buyer() {
    let mut h = Harness::new();
    let escrow = funded_with_tool_start_released(&mut h);
    let before = h.escrow(&escrow);
    let vault = h.ata(&escrow);
    warp_to_deadline(&mut h, &escrow);

    // The SQE signs as `buyer` and passes its own ATA.
    let sqe = h.sqe.pubkey();
    let mint = h.mint;
    let sqe_token = h.create_ata(&sqe, &mint);
    let tx = h.refund_tx(h.refund_accounts(&escrow, &sqe), &h.sqe);
    h.expect_err(tx, EscrowError::UnauthorizedBuyer);
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&before));
    assert_eq!(h.token_balance(&vault), TOTAL - TOOL_START_AMOUNT);
    assert_eq!(h.token_balance(&sqe_token), 0);
}

#[test]
fn t18_refund_after_the_deadline_returns_the_unreleased_balance_to_the_buyer() {
    let mut h = Harness::new();
    let escrow = funded_with_tool_start_released(&mut h);
    let before = h.escrow(&escrow);
    let buyer = h.buyer.pubkey();
    let buyer_token = h.ata(&buyer);
    let payout_token = h.ata(&h.payout_owner.pubkey());
    let vault = h.ata(&escrow);
    assert_eq!(h.token_balance(&vault), TOTAL - TOOL_START_AMOUNT);
    assert_eq!(h.token_balance(&buyer_token), 0);

    warp_to_deadline(&mut h, &escrow);
    h.svm.warp_to_slot(REFUND_SLOT);
    let tx = h.refund_tx(h.refund_accounts(&escrow, &buyer), &h.buyer);
    assert_eq!(tx.signatures.len(), 2, "relayer and buyer");
    h.cu(&tx);
    size(&tx);
    let meta = h.send_ok(tx.clone());
    assert_eq!(h.svm.get_sysvar::<Clock>().slot, REFUND_SLOT);

    // Exactly `total − 360,000 tUSD` returns to ATA(buyer); the payout keeps Tool start.
    assert_eq!(h.token_balance(&buyer_token), TOTAL - TOOL_START_AMOUNT);
    assert_eq!(h.token_balance(&vault), 0);
    assert_eq!(h.token_balance(&payout_token), TOOL_START_AMOUNT);

    // Only the status and the pending milestones change; `released` keeps Tool start.
    let stored = h.escrow(&escrow);
    assert_eq!(stored.status, EscrowStatus::Refunded);
    assert_eq!(stored.released, TOOL_START_AMOUNT);
    let statuses: Vec<_> = stored.milestones.iter().map(|m| m.status).collect();
    assert_eq!(
        statuses,
        [
            MilestoneStatus::Released,
            MilestoneStatus::Cancelled,
            MilestoneStatus::Cancelled,
        ]
    );
    let mut expected = before;
    expected.status = EscrowStatus::Refunded;
    expected.milestones[1].status = MilestoneStatus::Cancelled;
    expected.milestones[2].status = MilestoneStatus::Cancelled;
    assert_eq!(bytes(&stored), bytes(&expected));

    let events: Vec<PoRefunded> = emitted(&tx, &meta);
    assert_eq!(events.len(), 1, "exactly one PoRefunded");
    let event = &events[0];
    assert_eq!(event.escrow, escrow);
    assert_eq!(event.chain_po_id, PO_ID);
    assert_eq!(event.amount, TOTAL - TOOL_START_AMOUNT);
    assert_eq!(event.to, buyer_token);
    assert_eq!(event.slot, REFUND_SLOT);
}

#[test]
fn t18_release_after_refund_fails_with_escrow_closed() {
    let mut h = Harness::new();
    let escrow = funded_with_tool_start_released(&mut h);
    let buyer = h.buyer.pubkey();
    warp_to_deadline(&mut h, &escrow);
    let tx = h.refund_tx(h.refund_accounts(&escrow, &buyer), &h.buyer);
    h.send_ok(tx);
    let refunded = h.escrow(&escrow);
    let buyer_token = h.ata(&buyer);
    let payout_token = h.ata(&h.payout_owner.pubkey());
    let vault = h.ata(&escrow);

    // The SQE's gate-1 attestation is valid, but the escrow is closed for every milestone.
    let attestation_1 = h.attest(&escrow, 1);
    let attestation_0 = refunded.gates[0].attestation;
    for (milestone, attestation) in [(0, attestation_0), (1, attestation_1), (2, attestation_1)] {
        println!("case: release({milestone})");
        let tx = h.release_with(&escrow, milestone, &attestation);
        h.expect_err(tx, EscrowError::EscrowClosed);
    }
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&refunded));
    assert_eq!(h.token_balance(&vault), 0);
    assert_eq!(h.token_balance(&buyer_token), TOTAL - TOOL_START_AMOUNT);
    assert_eq!(h.token_balance(&payout_token), TOOL_START_AMOUNT);
}

#[test]
fn refund_of_an_unfunded_escrow_fails_with_not_funded() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    let created = h.escrow(&escrow);
    let buyer = h.buyer.pubkey();
    let mint = h.mint;
    let buyer_token = h.create_ata(&buyer, &mint);
    warp_to_deadline(&mut h, &escrow);

    let tx = h.refund_tx(h.refund_accounts(&escrow, &buyer), &h.buyer);
    h.expect_err(tx, EscrowError::NotFunded);
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&created));
    assert_eq!(h.token_balance(&buyer_token), 0);
}

#[test]
fn refund_of_a_completed_or_refunded_escrow_fails_with_escrow_closed() {
    let mut h = Harness::new();
    let buyer = h.buyer.pubkey();
    let buyer_token = h.ata(&buyer);

    // Completed: both gates approved, the payout holds the whole total.
    let completed = h.create_po();
    h.fund(&completed);
    h.approve(&completed, 0);
    h.approve(&completed, 1);
    assert_eq!(h.escrow(&completed).status, EscrowStatus::Completed);

    // Refunded: a second PO refunded once.
    let refunded = h.create_po_with_id("PO-1043-9WXR");
    h.fund(&refunded);
    warp_to_deadline(&mut h, &refunded);
    let tx = h.refund_tx(h.refund_accounts(&refunded, &buyer), &h.buyer);
    h.send_ok(tx);
    assert_eq!(h.escrow(&refunded).status, EscrowStatus::Refunded);
    assert_eq!(h.token_balance(&buyer_token), TOTAL);

    for escrow in [completed, refunded] {
        let before = h.escrow(&escrow);
        println!("case: {:?}", before.status);
        let tx = h.refund_tx(h.refund_accounts(&escrow, &buyer), &h.buyer);
        h.expect_err(tx, EscrowError::EscrowClosed);
        assert_eq!(bytes(&h.escrow(&escrow)), bytes(&before));
        assert_eq!(h.token_balance(&h.ata(&escrow)), 0);
        assert_eq!(h.token_balance(&buyer_token), TOTAL);
    }
}

#[test]
fn refund_with_nothing_left_fails_with_nothing_to_refund() {
    let mut h = Harness::new();
    let escrow = funded_with_tool_start_released(&mut h);
    let buyer = h.buyer.pubkey();
    let buyer_token = h.ata(&buyer);
    let vault = h.ata(&escrow);
    warp_to_deadline(&mut h, &escrow);

    // `Funded` with `released == total` is unreachable through `release` (the last release sets
    // `Completed`), so the guard is reached by rewriting the stored escrow.
    let mut stored = h.escrow(&escrow);
    stored.released = stored.total;
    set_escrow(&mut h, &escrow, &stored);

    let tx = h.refund_tx(h.refund_accounts(&escrow, &buyer), &h.buyer);
    h.expect_err(tx, EscrowError::NothingToRefund);
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&stored));
    assert_eq!(h.token_balance(&vault), TOTAL - TOOL_START_AMOUNT);
    assert_eq!(h.token_balance(&buyer_token), 0);
}

#[test]
fn a_refund_into_any_account_but_the_buyer_ata_fails() {
    let mut h = Harness::new();
    let escrow = funded_with_tool_start_released(&mut h);
    let before = h.escrow(&escrow);
    let buyer = h.buyer.pubkey();
    let payout_token = h.ata(&h.payout_owner.pubkey());
    let vault = h.ata(&escrow);
    warp_to_deadline(&mut h, &escrow);

    // The buyer signs but names the payout's ATA as `buyer_token`.
    let mut accounts = h.refund_accounts(&escrow, &buyer);
    accounts.buyer_token = payout_token;
    let tx = h.refund_tx(accounts, &h.buyer);
    h.expect_err(tx, ErrorCode::ConstraintTokenOwner);
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&before));
    assert_eq!(h.token_balance(&vault), TOTAL - TOOL_START_AMOUNT);
    assert_eq!(h.token_balance(&payout_token), TOOL_START_AMOUNT);
}
