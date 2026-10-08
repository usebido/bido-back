//! `fund` (spec 01 section 3.5 row `fund`, section 3.8 `PoFunded`; T14, T15 fund cases;
//! ESC-AC-08): the buyer moves exactly the PO total into the vault once, which sets `Funded` and
//! emits `PoFunded`; a donation already in the vault cannot block it (the delta check); a second
//! `fund`, a non-buyer signer and a completed or refunded escrow fail with their own errors and
//! move no tokens; a gate approval before `fund` fails with `NotFunded` (T15's release case) and
//! leaves no attestation behind.

mod common;

use {
    anchor_lang::{
        prelude::{Clock, Pubkey},
        AccountSerialize,
    },
    anchor_spl::token,
    bido_escrow::{EscrowError, EscrowStatus, PoEscrow, PoFunded},
    common::*,
    solana_signer::Signer,
};

/// The account bytes `escrow` serializes to.
fn bytes(escrow: &PoEscrow) -> Vec<u8> {
    let mut out = Vec::new();
    escrow.try_serialize(&mut out).expect("serialize PoEscrow");
    out
}

/// Rewrites the stored status of `escrow`: `Completed` and `Refunded` are only reachable through
/// `release` and `refund`, so this places an escrow in either state directly.
fn set_status(h: &mut Harness, escrow: &Pubkey, status: EscrowStatus) {
    let mut stored = h.escrow(escrow);
    stored.status = status;
    let mut account = h.svm.get_account(escrow).expect("escrow account");
    let mut data = account.data.as_mut_slice();
    stored.try_serialize(&mut data).expect("serialize PoEscrow");
    h.svm.set_account(*escrow, account).expect("set escrow");
}

#[test]
fn funds_the_fixture_po() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    let before = h.escrow(&escrow);
    let buyer = h.buyer.pubkey();
    let buyer_token = h.mint_to(&buyer, TOTAL);
    let vault = h.ata(&escrow);

    let tx = h.fund_tx(h.fund_accounts(&escrow, &buyer), &h.buyer);
    h.cu(&tx);
    size(&tx);
    let meta = h.send_ok(tx.clone());
    let slot = h.svm.get_sysvar::<Clock>().slot;

    assert_eq!(h.token_balance(&vault), TOTAL);
    assert_eq!(h.token_balance(&buyer_token), 0);

    // Only the status changes.
    let stored = h.escrow(&escrow);
    assert_eq!(stored.status, EscrowStatus::Funded);
    let mut expected = before;
    expected.status = EscrowStatus::Funded;
    assert_eq!(bytes(&stored), bytes(&expected));

    let events: Vec<PoFunded> = emitted(&tx, &meta);
    assert_eq!(events.len(), 1, "exactly one PoFunded");
    let event = &events[0];
    assert_eq!(event.escrow, escrow);
    assert_eq!(event.chain_po_id, PO_ID);
    assert_eq!(event.buyer, buyer);
    assert_eq!(event.amount, TOTAL);
    assert_eq!(event.vault, vault);
    assert_eq!(event.slot, slot);
}

#[test]
fn t14_a_donation_in_the_vault_does_not_block_fund() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    let vault = h.ata(&escrow);

    // An attacker sends 1 base unit to the vault before the buyer funds.
    let attacker = test_keypair("attacker");
    let attacker_token = h.mint_to(&attacker.pubkey(), 1);
    let donate = token::spl_token::instruction::transfer_checked(
        &token::ID,
        &attacker_token,
        &h.mint,
        &vault,
        &attacker.pubkey(),
        &[],
        1,
        MINT_DECIMALS,
    )
    .expect("transfer_checked");
    let tx = h.tx(&[donate], &[&attacker]);
    h.send_ok(tx);
    assert_eq!(h.token_balance(&vault), 1);

    let buyer = h.buyer.pubkey();
    let buyer_token = h.mint_to(&buyer, TOTAL);
    let tx = h.fund_tx(h.fund_accounts(&escrow, &buyer), &h.buyer);
    let meta = h.send_ok(tx.clone());
    assert_eq!(h.escrow(&escrow).status, EscrowStatus::Funded);
    assert_eq!(h.token_balance(&vault), TOTAL + 1);
    assert_eq!(h.token_balance(&buyer_token), 0);
    // The event reports the funded total, not the vault balance.
    let events: Vec<PoFunded> = emitted(&tx, &meta);
    assert_eq!(events.len(), 1, "exactly one PoFunded");
    assert_eq!(events[0].amount, TOTAL);
}

#[test]
fn t15_a_second_fund_fails_with_already_funded() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    let buyer = h.buyer.pubkey();
    let buyer_token = h.mint_to(&buyer, TOTAL);
    let vault = h.ata(&escrow);

    let tx = h.fund_tx(h.fund_accounts(&escrow, &buyer), &h.buyer);
    h.expect_err(tx, EscrowError::AlreadyFunded);
    assert_eq!(h.escrow(&escrow).status, EscrowStatus::Funded);
    assert_eq!(h.token_balance(&vault), TOTAL);
    assert_eq!(h.token_balance(&buyer_token), TOTAL);
}

#[test]
fn t15_fund_by_a_non_buyer_fails_with_unauthorized_buyer() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    let vault = h.ata(&escrow);
    // The SQE holds the total in its own ATA and signs as `buyer`.
    let sqe = h.sqe.pubkey();
    let sqe_token = h.mint_to(&sqe, TOTAL);

    let tx = h.fund_tx(h.fund_accounts(&escrow, &sqe), &h.sqe);
    h.expect_err(tx, EscrowError::UnauthorizedBuyer);
    assert_eq!(h.escrow(&escrow).status, EscrowStatus::Created);
    assert_eq!(h.token_balance(&vault), 0);
    assert_eq!(h.token_balance(&sqe_token), TOTAL);
}

#[test]
fn fund_of_a_completed_or_refunded_escrow_fails_with_escrow_closed() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    let buyer = h.buyer.pubkey();
    let buyer_token = h.mint_to(&buyer, TOTAL);
    let vault = h.ata(&escrow);

    for status in [EscrowStatus::Completed, EscrowStatus::Refunded] {
        println!("case: {status:?}");
        set_status(&mut h, &escrow, status);
        let tx = h.fund_tx(h.fund_accounts(&escrow, &buyer), &h.buyer);
        h.expect_err(tx, EscrowError::EscrowClosed);
        assert_eq!(h.escrow(&escrow).status, status);
        assert_eq!(h.token_balance(&vault), 0);
        assert_eq!(h.token_balance(&buyer_token), TOTAL);
    }
}

#[test]
fn t15_release_before_fund_fails_with_not_funded() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    let created = h.escrow(&escrow);
    let vault = h.ata(&escrow);
    let payout_token = h.ata(&h.payout_owner.pubkey());

    // The buyer approves Tool start before Reserve: the attestation would be valid, but the
    // escrow holds nothing, so the whole approval transaction reverts.
    let tx = h.approve_tx(&escrow, 0, &h.buyer, &fixture_evidence_hash(0));
    h.expect_err(tx, EscrowError::NotFunded);
    assert!(
        h.svm.get_account(&created.gates[0].attestation).is_none(),
        "no attestation survives the failed approval"
    );
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&created));
    assert_eq!(h.token_balance(&vault), 0);
    assert_eq!(h.token_balance(&payout_token), 0);
}
