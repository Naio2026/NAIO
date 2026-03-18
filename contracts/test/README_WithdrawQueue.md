# Withdraw / Proxy Burn / Queue Test Notes

## Existing cases (WithdrawQueue.t.sol)

| Test | Coverage |
|------|----------|
| `test_withdraw_epoch0_initializes_quota_and_pays` | Epoch0 withdraw before poke: init current quota, pay USDT and burn NAIO immediately |
| `test_withdraw_immediate_before_poke` | Epoch1 withdraw before poke: pay USDT and burn NAIO immediately, quota consumed |
| `test_withdraw_after_poke_same_day_reverts` | Withdraw on poke day: not executed, only queued; no USDT increase |
| `test_queue_fifo_process_one_by_one` | Queue FIFO: after same-day poke two users queue; next day process 1 step only processes head (alice), bob still queued |
| `test_unlock_tiers_still_match_principal_cap` | Unlock tiers by epoch: 0–1 epoch 40%, 1–2 epoch 60%, 2+ epoch 80% (test config: 1 month = 1 epoch) |

## New cases (aligned with requirements)

| Test | Coverage |
|------|----------|
| `test_next_epoch_after_poke_has_quota_immediate_withdraw` | **Poke pre-fills next epoch quota**: after epoch1 poke, enter epoch2; first withdraw pays USDT and burns NAIO immediately, quota for epoch2, not queued |
| `test_deflation_burn_minus_withdraw_consumed_to_blackhole` | **Deflation: withdraw consumed deducted from burn, remainder to blackhole**: epoch1 withdraw first (consumes quota), then poke; snapshot `withdrawBurnConsumed` = amount burned for withdraw; blackhole delta = `snapBurn - snapWithdrawBurnConsumed` |

## Related cases in other files

- **PokeCatchup.t.sol**: `test_poke_status_snapshot_and_detailed_event` checks deflation snapshot and event include `withdrawBurnConsumed` (0 in that scenario).
- **BlackholeAccounting.t.sol**: `test_burn_increases_on_poke_total_supply_unchanged` deflation increases blackhole; `test_withdraw_does_not_burn_naio` is “same-day poke then withdraw only queued” so blackhole unchanged.

## Run

```bash
cd contracts && forge test --match-contract WithdrawQueueTest -v
```
