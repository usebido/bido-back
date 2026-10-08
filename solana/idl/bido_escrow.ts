/**
 * Program IDL in camelCase format in order to be used in JS/TS.
 *
 * Note that this is only a type helper and is not the actual IDL. The original
 * IDL can be found at `target/idl/bido_escrow.json`.
 */
export type BidoEscrow = {
  "address": "7992GKVq51ChvsVJQ9GFto2aBozUGuMcuY93SUSxexJw",
  "metadata": {
    "name": "bidoEscrow",
    "version": "0.1.0",
    "spec": "0.1.0",
    "description": "Bido escrow: pays a purchase order's fixed payout by milestones, only against SAS gate attestations (Solana devnet)"
  },
  "instructions": [
    {
      "name": "createPo",
      "docs": [
        "Creates an unfunded escrow for a PO, signed by the operator; the relayer pays."
      ],
      "discriminator": [
        70,
        241,
        65,
        62,
        199,
        136,
        179,
        245
      ],
      "accounts": [
        {
          "name": "config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  99,
                  111,
                  110,
                  102,
                  105,
                  103
                ]
              }
            ]
          }
        },
        {
          "name": "operator",
          "signer": true
        },
        {
          "name": "payer",
          "docs": [
            "The relayer: pays the fee and the rent of the escrow and both ATAs."
          ],
          "writable": true,
          "signer": true
        },
        {
          "name": "buyer"
        },
        {
          "name": "payout"
        },
        {
          "name": "mint"
        },
        {
          "name": "escrow",
          "writable": true,
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  112,
                  111
                ]
              },
              {
                "kind": "account",
                "path": "buyer"
              },
              {
                "kind": "arg",
                "path": "args.chainPoId"
              }
            ]
          }
        },
        {
          "name": "vault",
          "writable": true,
          "pda": {
            "seeds": [
              {
                "kind": "account",
                "path": "escrow"
              },
              {
                "kind": "account",
                "path": "tokenProgram"
              },
              {
                "kind": "account",
                "path": "mint"
              }
            ],
            "program": {
              "kind": "const",
              "value": [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89
              ]
            }
          }
        },
        {
          "name": "payoutToken",
          "writable": true,
          "pda": {
            "seeds": [
              {
                "kind": "account",
                "path": "payout"
              },
              {
                "kind": "account",
                "path": "tokenProgram"
              },
              {
                "kind": "account",
                "path": "mint"
              }
            ],
            "program": {
              "kind": "const",
              "value": [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89
              ]
            }
          }
        },
        {
          "name": "sasCredential"
        },
        {
          "name": "tokenProgram"
        },
        {
          "name": "associatedTokenProgram",
          "address": "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
        },
        {
          "name": "systemProgram",
          "address": "11111111111111111111111111111111"
        },
        {
          "name": "eventAuthority"
        },
        {
          "name": "program"
        }
      ],
      "args": [
        {
          "name": "args",
          "type": {
            "defined": {
              "name": "createPoArgs"
            }
          }
        }
      ]
    },
    {
      "name": "fund",
      "docs": [
        "Moves exactly the PO total from the buyer into the vault, signed by the buyer."
      ],
      "discriminator": [
        218,
        188,
        111,
        221,
        152,
        113,
        174,
        7
      ],
      "accounts": [
        {
          "name": "buyer",
          "signer": true
        },
        {
          "name": "escrow",
          "writable": true
        },
        {
          "name": "buyerToken",
          "writable": true
        },
        {
          "name": "vault",
          "writable": true
        },
        {
          "name": "mint"
        },
        {
          "name": "tokenProgram"
        },
        {
          "name": "eventAuthority"
        },
        {
          "name": "program"
        }
      ],
      "args": []
    },
    {
      "name": "initConfig",
      "docs": [
        "One-shot setup of `Config`, signed by the program upgrade authority."
      ],
      "discriminator": [
        23,
        235,
        115,
        232,
        168,
        96,
        1,
        231
      ],
      "accounts": [
        {
          "name": "admin",
          "writable": true,
          "signer": true
        },
        {
          "name": "config",
          "writable": true,
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  99,
                  111,
                  110,
                  102,
                  105,
                  103
                ]
              }
            ]
          }
        },
        {
          "name": "program",
          "address": "7992GKVq51ChvsVJQ9GFto2aBozUGuMcuY93SUSxexJw"
        },
        {
          "name": "programData"
        },
        {
          "name": "sasCredential"
        },
        {
          "name": "sasSchema"
        },
        {
          "name": "systemProgram",
          "address": "11111111111111111111111111111111"
        }
      ],
      "args": [
        {
          "name": "args",
          "type": {
            "defined": {
              "name": "initConfigArgs"
            }
          }
        }
      ]
    },
    {
      "name": "refund",
      "docs": [
        "Returns the unreleased balance to the buyer's ATA after `refund_after`, signed by the buyer."
      ],
      "discriminator": [
        2,
        96,
        183,
        251,
        63,
        208,
        46,
        46
      ],
      "accounts": [
        {
          "name": "buyer",
          "signer": true
        },
        {
          "name": "escrow",
          "writable": true
        },
        {
          "name": "vault",
          "writable": true
        },
        {
          "name": "buyerToken",
          "docs": [
            "ATA(buyer, mint); must already exist."
          ],
          "writable": true,
          "pda": {
            "seeds": [
              {
                "kind": "account",
                "path": "buyer"
              },
              {
                "kind": "account",
                "path": "tokenProgram"
              },
              {
                "kind": "account",
                "path": "mint"
              }
            ],
            "program": {
              "kind": "const",
              "value": [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89
              ]
            }
          }
        },
        {
          "name": "mint"
        },
        {
          "name": "tokenProgram"
        },
        {
          "name": "eventAuthority"
        },
        {
          "name": "program"
        }
      ],
      "args": []
    },
    {
      "name": "release",
      "docs": [
        "Pays one milestone to the fixed payout against its gate's SAS attestation. Permissionless."
      ],
      "discriminator": [
        253,
        249,
        15,
        206,
        28,
        127,
        193,
        241
      ],
      "accounts": [
        {
          "name": "escrow",
          "writable": true
        },
        {
          "name": "vault",
          "writable": true
        },
        {
          "name": "payoutToken",
          "writable": true
        },
        {
          "name": "mint"
        },
        {
          "name": "attestation"
        },
        {
          "name": "tokenProgram"
        },
        {
          "name": "eventAuthority"
        },
        {
          "name": "program"
        }
      ],
      "args": [
        {
          "name": "milestone",
          "type": "u8"
        }
      ]
    },
    {
      "name": "updateConfig",
      "docs": [
        "Updates `Config`, signed by the current upgrade authority; never touches a `PoEscrow`."
      ],
      "discriminator": [
        29,
        158,
        252,
        191,
        10,
        83,
        219,
        99
      ],
      "accounts": [
        {
          "name": "admin",
          "signer": true
        },
        {
          "name": "config",
          "writable": true,
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  99,
                  111,
                  110,
                  102,
                  105,
                  103
                ]
              }
            ]
          }
        },
        {
          "name": "program",
          "address": "7992GKVq51ChvsVJQ9GFto2aBozUGuMcuY93SUSxexJw"
        },
        {
          "name": "programData"
        },
        {
          "name": "sasCredential"
        },
        {
          "name": "sasSchema"
        }
      ],
      "args": [
        {
          "name": "args",
          "type": {
            "defined": {
              "name": "updateConfigArgs"
            }
          }
        }
      ]
    }
  ],
  "accounts": [
    {
      "name": "config",
      "discriminator": [
        155,
        12,
        170,
        224,
        30,
        250,
        204,
        130
      ]
    },
    {
      "name": "poEscrow",
      "discriminator": [
        117,
        6,
        174,
        16,
        212,
        15,
        177,
        143
      ]
    }
  ],
  "events": [
    {
      "name": "milestoneReleased",
      "discriminator": [
        49,
        225,
        91,
        223,
        34,
        165,
        109,
        181
      ]
    },
    {
      "name": "poCompleted",
      "discriminator": [
        132,
        114,
        50,
        200,
        168,
        217,
        132,
        67
      ]
    },
    {
      "name": "poCreated",
      "discriminator": [
        120,
        196,
        83,
        130,
        196,
        134,
        127,
        248
      ]
    },
    {
      "name": "poFunded",
      "discriminator": [
        17,
        204,
        119,
        236,
        246,
        211,
        137,
        89
      ]
    },
    {
      "name": "poRefunded",
      "discriminator": [
        106,
        55,
        152,
        245,
        251,
        16,
        22,
        117
      ]
    }
  ],
  "errors": [
    {
      "code": 6000,
      "name": "unauthorizedAdmin",
      "msg": "Signer is not the program upgrade authority"
    },
    {
      "code": 6001,
      "name": "unauthorizedOperator",
      "msg": "Signer is not the configured operator"
    },
    {
      "code": 6002,
      "name": "unauthorizedBuyer",
      "msg": "Signer is not this escrow's buyer"
    },
    {
      "code": 6003,
      "name": "invalidSasConfig",
      "msg": "SAS credential or schema is not valid for Bido gate approvals"
    },
    {
      "code": 6004,
      "name": "mintNotAllowed",
      "msg": "Mint is not allowed or does not match the escrow"
    },
    {
      "code": 6005,
      "name": "invalidPoId",
      "msg": "Invalid on-chain PO id"
    },
    {
      "code": 6006,
      "name": "invalidMilestoneCount",
      "msg": "A PO needs 1 to 4 milestones"
    },
    {
      "code": 6007,
      "name": "nameTooLong",
      "msg": "Milestone name must be 1 to 24 bytes"
    },
    {
      "code": 6008,
      "name": "zeroBps",
      "msg": "Milestone share must be greater than zero"
    },
    {
      "code": 6009,
      "name": "bpsSumNot10000",
      "msg": "Milestone shares must add up to 100%"
    },
    {
      "code": 6010,
      "name": "zeroAmount",
      "msg": "Total and every milestone amount must be greater than zero"
    },
    {
      "code": 6011,
      "name": "mathOverflow",
      "msg": "Arithmetic overflow"
    },
    {
      "code": 6012,
      "name": "invalidGate",
      "msg": "Invalid or unused approval gate"
    },
    {
      "code": 6013,
      "name": "invalidAttester",
      "msg": "Gate attester is empty or equals the payout"
    },
    {
      "code": 6014,
      "name": "gateAttesterMismatch",
      "msg": "Gate attester is not an authorized signer of the credential"
    },
    {
      "code": 6015,
      "name": "payoutIsEscrow",
      "msg": "Payout cannot be the escrow or its vault"
    },
    {
      "code": 6016,
      "name": "payoutIsBuyer",
      "msg": "Payout cannot be the buyer"
    },
    {
      "code": 6017,
      "name": "vaultAccountInvalid",
      "msg": "Vault token account is not the escrow's clean vault"
    },
    {
      "code": 6018,
      "name": "invalidRefundDeadline",
      "msg": "Refund deadline must be in the future"
    },
    {
      "code": 6019,
      "name": "alreadyFunded",
      "msg": "Escrow is already funded"
    },
    {
      "code": 6020,
      "name": "notFunded",
      "msg": "Escrow is not funded"
    },
    {
      "code": 6021,
      "name": "escrowClosed",
      "msg": "Escrow is completed or refunded"
    },
    {
      "code": 6022,
      "name": "fundDeltaMismatch",
      "msg": "Vault did not receive exactly the PO total"
    },
    {
      "code": 6023,
      "name": "invalidMilestone",
      "msg": "Milestone index out of range"
    },
    {
      "code": 6024,
      "name": "milestoneAlreadyReleased",
      "msg": "Milestone already released"
    },
    {
      "code": 6025,
      "name": "attestationAddressMismatch",
      "msg": "Attestation does not belong to this escrow and gate"
    },
    {
      "code": 6026,
      "name": "attestationMissing",
      "msg": "No approval attestation exists for this gate"
    },
    {
      "code": 6027,
      "name": "attestationMalformed",
      "msg": "Attestation account is malformed"
    },
    {
      "code": 6028,
      "name": "attestationCredentialMismatch",
      "msg": "Attestation credential mismatch"
    },
    {
      "code": 6029,
      "name": "attestationSchemaMismatch",
      "msg": "Attestation schema mismatch"
    },
    {
      "code": 6030,
      "name": "attestationNonceMismatch",
      "msg": "Attestation nonce mismatch"
    },
    {
      "code": 6031,
      "name": "attestationPoHashMismatch",
      "msg": "Attestation is for a different PO document"
    },
    {
      "code": 6032,
      "name": "attestationGateMismatch",
      "msg": "Attestation is for a different gate"
    },
    {
      "code": 6033,
      "name": "attesterMismatch",
      "msg": "Attestation was not signed by this gate's approver"
    },
    {
      "code": 6034,
      "name": "attestationExpired",
      "msg": "Attestation has expired"
    },
    {
      "code": 6035,
      "name": "payoutMismatch",
      "msg": "Destination is not the escrow's fixed payout account"
    },
    {
      "code": 6036,
      "name": "refundTooEarly",
      "msg": "Refund deadline has not passed"
    },
    {
      "code": 6037,
      "name": "nothingToRefund",
      "msg": "Nothing left to refund"
    }
  ],
  "types": [
    {
      "name": "config",
      "docs": [
        "Program-wide settings. PDA `[\"config\"]`."
      ],
      "type": {
        "kind": "struct",
        "fields": [
          {
            "name": "admin",
            "docs": [
              "Program upgrade authority at the last `init_config` or `update_config`."
            ],
            "type": "pubkey"
          },
          {
            "name": "operator",
            "docs": [
              "The only key allowed to call `create_po`."
            ],
            "type": "pubkey"
          },
          {
            "name": "allowedMints",
            "docs": [
              "`[tUSD, Circle devnet USDC]`; `Pubkey::default()` marks an empty slot."
            ],
            "type": {
              "array": [
                "pubkey",
                2
              ]
            }
          },
          {
            "name": "sasCredential",
            "docs": [
              "SAS credential \"bido-approvals\"."
            ],
            "type": "pubkey"
          },
          {
            "name": "sasSchema",
            "docs": [
              "SAS schema \"bido-gate-approval\" v1."
            ],
            "type": "pubkey"
          },
          {
            "name": "bump",
            "type": "u8"
          }
        ]
      }
    },
    {
      "name": "createPoArgs",
      "type": {
        "kind": "struct",
        "fields": [
          {
            "name": "chainPoId",
            "type": "string"
          },
          {
            "name": "poHash",
            "type": {
              "array": [
                "u8",
                32
              ]
            }
          },
          {
            "name": "cadHash",
            "type": {
              "array": [
                "u8",
                32
              ]
            }
          },
          {
            "name": "total",
            "type": "u64"
          },
          {
            "name": "gateAttesters",
            "type": {
              "vec": "pubkey"
            }
          },
          {
            "name": "milestones",
            "type": {
              "vec": {
                "defined": {
                  "name": "milestoneInput"
                }
              }
            }
          },
          {
            "name": "refundAfter",
            "type": "i64"
          }
        ]
      }
    },
    {
      "name": "escrowStatus",
      "type": {
        "kind": "enum",
        "variants": [
          {
            "name": "created"
          },
          {
            "name": "funded"
          },
          {
            "name": "completed"
          },
          {
            "name": "refunded"
          }
        ]
      }
    },
    {
      "name": "gate",
      "docs": [
        "An approval point with one pinned attester."
      ],
      "type": {
        "kind": "struct",
        "fields": [
          {
            "name": "attester",
            "type": "pubkey"
          },
          {
            "name": "attestation",
            "docs": [
              "Expected SAS attestation PDA `[\"attestation\", credential, schema, gate nonce]`."
            ],
            "type": "pubkey"
          }
        ]
      }
    },
    {
      "name": "initConfigArgs",
      "type": {
        "kind": "struct",
        "fields": [
          {
            "name": "operator",
            "type": "pubkey"
          },
          {
            "name": "allowedMints",
            "type": {
              "array": [
                "pubkey",
                2
              ]
            }
          }
        ]
      }
    },
    {
      "name": "milestone",
      "type": {
        "kind": "struct",
        "fields": [
          {
            "name": "name",
            "type": "string"
          },
          {
            "name": "bps",
            "type": "u16"
          },
          {
            "name": "amount",
            "docs": [
              "Fixed at `create_po`."
            ],
            "type": "u64"
          },
          {
            "name": "gate",
            "type": "u8"
          },
          {
            "name": "status",
            "type": {
              "defined": {
                "name": "milestoneStatus"
              }
            }
          },
          {
            "name": "releasedSlot",
            "docs": [
              "0 until released."
            ],
            "type": "u64"
          },
          {
            "name": "approvedBy",
            "docs": [
              "Attestation signer; default until released."
            ],
            "type": "pubkey"
          },
          {
            "name": "evidenceHash",
            "docs": [
              "From the attestation; zero until released."
            ],
            "type": {
              "array": [
                "u8",
                32
              ]
            }
          }
        ]
      }
    },
    {
      "name": "milestoneInput",
      "type": {
        "kind": "struct",
        "fields": [
          {
            "name": "name",
            "type": "string"
          },
          {
            "name": "bps",
            "type": "u16"
          },
          {
            "name": "gate",
            "type": "u8"
          }
        ]
      }
    },
    {
      "name": "milestoneReleased",
      "type": {
        "kind": "struct",
        "fields": [
          {
            "name": "escrow",
            "type": "pubkey"
          },
          {
            "name": "chainPoId",
            "type": "string"
          },
          {
            "name": "milestone",
            "type": "u8"
          },
          {
            "name": "gate",
            "type": "u8"
          },
          {
            "name": "amount",
            "type": "u64"
          },
          {
            "name": "releasedTotal",
            "type": "u64"
          },
          {
            "name": "payout",
            "type": "pubkey"
          },
          {
            "name": "payoutToken",
            "type": "pubkey"
          },
          {
            "name": "attestation",
            "type": "pubkey"
          },
          {
            "name": "approvedBy",
            "type": "pubkey"
          },
          {
            "name": "evidenceHash",
            "type": {
              "array": [
                "u8",
                32
              ]
            }
          },
          {
            "name": "slot",
            "type": "u64"
          }
        ]
      }
    },
    {
      "name": "milestoneStatus",
      "type": {
        "kind": "enum",
        "variants": [
          {
            "name": "pending"
          },
          {
            "name": "released"
          },
          {
            "name": "cancelled"
          }
        ]
      }
    },
    {
      "name": "poCompleted",
      "type": {
        "kind": "struct",
        "fields": [
          {
            "name": "escrow",
            "type": "pubkey"
          },
          {
            "name": "chainPoId",
            "type": "string"
          },
          {
            "name": "total",
            "type": "u64"
          },
          {
            "name": "slot",
            "type": "u64"
          }
        ]
      }
    },
    {
      "name": "poCreated",
      "type": {
        "kind": "struct",
        "fields": [
          {
            "name": "escrow",
            "type": "pubkey"
          },
          {
            "name": "chainPoId",
            "type": "string"
          },
          {
            "name": "buyer",
            "type": "pubkey"
          },
          {
            "name": "payout",
            "type": "pubkey"
          },
          {
            "name": "payoutToken",
            "type": "pubkey"
          },
          {
            "name": "mint",
            "type": "pubkey"
          },
          {
            "name": "vault",
            "type": "pubkey"
          },
          {
            "name": "poHash",
            "type": {
              "array": [
                "u8",
                32
              ]
            }
          },
          {
            "name": "cadHash",
            "type": {
              "array": [
                "u8",
                32
              ]
            }
          },
          {
            "name": "total",
            "type": "u64"
          },
          {
            "name": "credential",
            "type": "pubkey"
          },
          {
            "name": "schema",
            "type": "pubkey"
          },
          {
            "name": "gateAttesters",
            "type": {
              "vec": "pubkey"
            }
          },
          {
            "name": "gateAttestations",
            "type": {
              "vec": "pubkey"
            }
          },
          {
            "name": "milestoneNames",
            "type": {
              "vec": "string"
            }
          },
          {
            "name": "milestoneBps",
            "type": {
              "vec": "u16"
            }
          },
          {
            "name": "milestoneGates",
            "type": "bytes"
          },
          {
            "name": "milestoneAmounts",
            "type": {
              "vec": "u64"
            }
          },
          {
            "name": "refundAfter",
            "type": "i64"
          },
          {
            "name": "slot",
            "type": "u64"
          }
        ]
      }
    },
    {
      "name": "poEscrow",
      "docs": [
        "One purchase order's escrow and audit record. PDA `[\"po\", buyer, chain_po_id]`; never closed."
      ],
      "type": {
        "kind": "struct",
        "fields": [
          {
            "name": "chainPoId",
            "type": "string"
          },
          {
            "name": "buyer",
            "docs": [
              "Signs `fund` and `refund` (the buyer approver's passkey wallet)."
            ],
            "type": "pubkey"
          },
          {
            "name": "payout",
            "docs": [
              "Payout owner wallet from the verified registry; immutable."
            ],
            "type": "pubkey"
          },
          {
            "name": "payoutToken",
            "docs": [
              "ATA(payout, mint), created at `create_po`."
            ],
            "type": "pubkey"
          },
          {
            "name": "mint",
            "type": "pubkey"
          },
          {
            "name": "vault",
            "docs": [
              "ATA(escrow, mint); its authority is this PDA."
            ],
            "type": "pubkey"
          },
          {
            "name": "poHash",
            "docs": [
              "sha256 of the PO PDF bytes."
            ],
            "type": {
              "array": [
                "u8",
                32
              ]
            }
          },
          {
            "name": "cadHash",
            "docs": [
              "sha256 of the drawing 2D PDF (rev C)."
            ],
            "type": {
              "array": [
                "u8",
                32
              ]
            }
          },
          {
            "name": "total",
            "docs": [
              "Base units (6 decimals)."
            ],
            "type": "u64"
          },
          {
            "name": "released",
            "type": "u64"
          },
          {
            "name": "status",
            "type": {
              "defined": {
                "name": "escrowStatus"
              }
            }
          },
          {
            "name": "credential",
            "docs": [
              "SAS credential copied from `Config` at creation."
            ],
            "type": "pubkey"
          },
          {
            "name": "schema",
            "docs": [
              "SAS schema copied from `Config` at creation."
            ],
            "type": "pubkey"
          },
          {
            "name": "gates",
            "type": {
              "vec": {
                "defined": {
                  "name": "gate"
                }
              }
            }
          },
          {
            "name": "milestones",
            "type": {
              "vec": {
                "defined": {
                  "name": "milestone"
                }
              }
            }
          },
          {
            "name": "refundAfter",
            "docs": [
              "Unix seconds after which the buyer may refund."
            ],
            "type": "i64"
          },
          {
            "name": "bump",
            "type": "u8"
          }
        ]
      }
    },
    {
      "name": "poFunded",
      "type": {
        "kind": "struct",
        "fields": [
          {
            "name": "escrow",
            "type": "pubkey"
          },
          {
            "name": "chainPoId",
            "type": "string"
          },
          {
            "name": "buyer",
            "type": "pubkey"
          },
          {
            "name": "amount",
            "type": "u64"
          },
          {
            "name": "vault",
            "type": "pubkey"
          },
          {
            "name": "slot",
            "type": "u64"
          }
        ]
      }
    },
    {
      "name": "poRefunded",
      "type": {
        "kind": "struct",
        "fields": [
          {
            "name": "escrow",
            "type": "pubkey"
          },
          {
            "name": "chainPoId",
            "type": "string"
          },
          {
            "name": "amount",
            "type": "u64"
          },
          {
            "name": "to",
            "type": "pubkey"
          },
          {
            "name": "slot",
            "type": "u64"
          }
        ]
      }
    },
    {
      "name": "updateConfigArgs",
      "docs": [
        "`Some` fields replace the stored values; `None` keeps them."
      ],
      "type": {
        "kind": "struct",
        "fields": [
          {
            "name": "operator",
            "type": {
              "option": "pubkey"
            }
          },
          {
            "name": "allowedMints",
            "type": {
              "option": {
                "array": [
                  "pubkey",
                  2
                ]
              }
            }
          },
          {
            "name": "sasCredential",
            "type": {
              "option": "pubkey"
            }
          },
          {
            "name": "sasSchema",
            "type": {
              "option": "pubkey"
            }
          }
        ]
      }
    }
  ]
};
