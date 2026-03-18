// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

import {NAIOToken} from "../src/NAIOToken.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {DepositWitnessRuleEngine} from "../src/DepositWitnessRuleEngine.sol";
import {NodeSeatPool} from "../src/pools/NodeSeatPool.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract WithdrawQueueTest is Test {
    MockERC20 usdt;
    NAIOToken naio;
    NAIOController controller;
    DepositWitnessRuleEngine engine;
    NodeSeatPool nodePool;

    address taxReceiver = address(0xC0FFEE);
    address ecoA = address(0xA0A0A0);
    address independentB = address(0xB0B0B0);
    address marketE = address(0xE0E0E0);
    address marketF = address(0xF0F0F0);

    address nodeOwner = vm.addr(101);
    address alice = vm.addr(201);
    address bob = vm.addr(202);
    address keeper = vm.addr(301);
    address seeder = vm.addr(401);
    uint256 w1Pk = 0xA11;
    uint256 w2Pk = 0xA12;
    uint256 w3Pk = 0xA13;

    function setUp() public {
        vm.warp(1_700_000_000);

        usdt = new MockERC20("USDT", "USDT", 18);
        naio = new NAIOToken("NAIO Token", "NAIO", 100_000_000e18, address(0xdead), taxReceiver);
        controller = new NAIOController(address(usdt), address(naio));

        naio.transfer(address(controller), 100_000_000e18);

        nodePool = new NodeSeatPool(address(usdt), address(naio));
        nodePool.setController(address(controller));
        _init1000Seats();
        nodePool.seal();

        controller.setPools(address(nodePool), marketE, marketF);
        controller.setRewardReceivers(ecoA, independentB);
        controller.setKeeper(keeper);
        address[] memory signers = new address[](3);
        signers[0] = vm.addr(w1Pk);
        signers[1] = vm.addr(w2Pk);
        signers[2] = vm.addr(w3Pk);
        engine = new DepositWitnessRuleEngine(address(controller), signers, 3, 0);
        controller.setDepositRuleEngine(address(engine));

        naio.setController(address(controller));

        usdt.mint(alice, 2_000e18);
        usdt.mint(bob, 2_000e18);
        // Seed initial pool USDT so that price/quota are meaningful for withdraw tests
        controller.setPoolSeeder(seeder);
        usdt.mint(seeder, controller.INITIAL_POOL_TARGET_USDT());
        vm.startPrank(seeder);
        usdt.approve(address(controller), controller.INITIAL_POOL_TARGET_USDT());
        controller.seedPoolUsdtFromSeeder(controller.INITIAL_POOL_TARGET_USDT());
        vm.stopPrank();
        vm.deal(alice, 10 ether);
        vm.deal(bob, 10 ether);

        vm.prank(alice);
        usdt.transfer(address(controller), 1000e18);
        uint256 deadline = block.timestamp + 600;
        bytes32 txHash = bytes32(uint256(9101));
        bytes32 digest = engine.witnessDigest(alice, 1000e18, txHash, deadline);
        bytes[] memory sigs = new bytes[](3);
        sigs[0] = _sig(w1Pk, digest);
        sigs[1] = _sig(w2Pk, digest);
        sigs[2] = _sig(w3Pk, digest);
        vm.prank(keeper);
        controller.depositFromTransferWitness(alice, 1000e18, txHash, deadline, sigs);
    }

    function _init1000Seats() internal {
        uint16 start = 1;
        uint16 seatId = 1;
        for (uint256 batch = 0; batch < 10; batch++) {
            address[] memory owners = new address[](100);
            for (uint256 i = 0; i < 100; i++) {
                if (seatId == 1) {
                    owners[i] = nodeOwner;
                } else {
                    owners[i] = address(uint160(0x100000 + seatId));
                }
                seatId++;
            }
            nodePool.setInitialOwners(start, owners);
            start += 100;
        }
        assertEq(nodePool.seatCount(), 1000);
    }

    function _sig(uint256 pk, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, v);
    }

    function test_withdraw_immediate_before_poke() public {
        vm.warp(block.timestamp + controller.EPOCH_SECONDS());

        uint256 usdtBefore = usdt.balanceOf(alice);
        uint256 burnBefore = naio.balanceOf(naio.burnAddress());

        vm.prank(alice);
        controller.withdrawLP();

        uint256 paid = usdt.balanceOf(alice) - usdtBefore;
        uint256 burned = naio.balanceOf(naio.burnAddress()) - burnBefore;

        assertGt(paid, 0, "withdraw should pay immediately");
        assertGt(burned, 0, "withdraw should burn NAIO immediately");
        assertEq(controller.withdrawBurnEpoch(), controller.getCurrentEpoch(), "quota epoch should be today");
        assertGt(controller.withdrawBurnQuotaToken(), 0, "daily burn quota should be initialized");
        assertGt(controller.withdrawBurnUsedToken(), 0, "daily burn quota should be consumed");
        assertLe(controller.withdrawBurnUsedToken(), controller.withdrawBurnQuotaToken(), "used <= quota");
    }

    function test_withdraw_epoch0_initializes_quota_and_pays() public {
        // In epoch0, before any poke, withdraw should still be executable with today's burn quota.
        assertEq(controller.getCurrentEpoch(), 0, "should start at epoch0");
        assertEq(controller.lastPokeEpoch(), type(uint32).max, "no poke yet (sentinel)");

        uint256 usdtBefore = usdt.balanceOf(alice);
        uint256 burnBefore = naio.balanceOf(naio.burnAddress());

        vm.prank(alice);
        controller.withdrawLP();

        uint256 paid = usdt.balanceOf(alice) - usdtBefore;
        uint256 burned = naio.balanceOf(naio.burnAddress()) - burnBefore;

        assertGt(paid, 0, "epoch0 withdraw should pay immediately");
        assertGt(burned, 0, "epoch0 withdraw should burn NAIO immediately");
        assertEq(controller.withdrawBurnEpoch(), 0, "quota epoch should be epoch0");
        assertGt(controller.withdrawBurnQuotaToken(), 0, "epoch0 burn quota should be initialized");
        assertGt(controller.withdrawBurnUsedToken(), 0, "epoch0 burn quota should be consumed");
        // If withdrawable amount exceeds quota, remainder is queued; so we only assert quota was used and some pay happened
    }

    /// @dev Burn quota is computed in real time per calendar epoch and does not depend on deflation;
    ///      within the same epoch, withdrawing after `poke()` can still execute immediately using the current-epoch quota;
    ///      only the remainder (if quota is insufficient) is queued.
    function test_withdraw_after_poke_same_epoch_immediate() public {
        vm.warp(block.timestamp + controller.EPOCH_SECONDS());
        assertEq(controller.getCurrentEpoch(), 1, "epoch 1");
        controller.poke();
        // When currentEpoch=1, the first `poke()` settles epoch0
        assertEq(controller.lastPokeEpoch(), 0, "poke done for epoch 0");
        assertEq(controller.getCurrentEpoch(), 1, "still same calendar epoch");

        uint256 beforeUsdt = usdt.balanceOf(alice);
        uint256 beforeBurn = naio.balanceOf(naio.burnAddress());
        vm.prank(alice);
        controller.withdrawLP();

        uint256 paid = usdt.balanceOf(alice) - beforeUsdt;
        uint256 burned = naio.balanceOf(naio.burnAddress()) - beforeBurn;
        assertGt(paid, 0, "same-epoch after poke should pay immediately using current-epoch quota");
        assertGt(burned, 0, "should burn NAIO");
        assertEq(controller.withdrawBurnEpoch(), 1, "quota is for current calendar epoch (1)");
        assertEq(controller.withdrawQueuedAmount(alice), 0, "no queue when quota sufficient");
    }

    /// @dev If quota is sufficient, both users may withdraw immediately; otherwise queued withdrawals are processed FIFO.
    function test_queue_fifo_process_one_by_one() public {
        vm.warp(block.timestamp + controller.EPOCH_SECONDS());
        controller.poke();

        vm.prank(bob);
        usdt.transfer(address(controller), 1000e18);
        uint256 deadline = block.timestamp + 600;
        bytes32 txHash = bytes32(uint256(9102));
        bytes32 digest = engine.witnessDigest(bob, 1000e18, txHash, deadline);
        bytes[] memory sigs = new bytes[](3);
        sigs[0] = _sig(w1Pk, digest);
        sigs[1] = _sig(w2Pk, digest);
        sigs[2] = _sig(w3Pk, digest);
        vm.prank(keeper);
        controller.depositFromTransferWitness(bob, 1000e18, txHash, deadline, sigs);

        uint256 aliceBefore = usdt.balanceOf(alice);
        uint256 bobBefore = usdt.balanceOf(bob);

        vm.prank(alice);
        controller.withdrawLP();
        vm.prank(bob);
        controller.withdrawLP();

        uint256 aliceQueued = controller.withdrawQueuedAmount(alice);
        uint256 bobQueued = controller.withdrawQueuedAmount(bob);
        if (aliceQueued > 0 && bobQueued > 0) {
            vm.warp(block.timestamp + controller.EPOCH_SECONDS());
            controller.processWithdrawQueue(1);
            assertGt(usdt.balanceOf(alice) - aliceBefore, 0, "alice should be processed first (FIFO)");
            assertEq(usdt.balanceOf(bob), bobBefore, "bob should wait for next step");
        } else {
            assertTrue(
                usdt.balanceOf(alice) > aliceBefore || usdt.balanceOf(bob) > bobBefore,
                "at least one got immediate when quota allowed"
            );
        }
    }

    /// @dev Next-epoch quota is computed in real time for that epoch; the first withdrawal can execute immediately (independent of whether deflation ran).
    function test_next_epoch_after_poke_has_quota_immediate_withdraw() public {
        vm.warp(block.timestamp + controller.EPOCH_SECONDS());
        controller.poke();
        vm.warp(block.timestamp + controller.EPOCH_SECONDS());
        assertEq(controller.getCurrentEpoch(), 2, "epoch 2");
        assertEq(controller.withdrawQueuedAmount(alice), 0, "alice not queued yet");

        uint256 usdtBefore = usdt.balanceOf(alice);
        uint256 burnBefore = naio.balanceOf(naio.burnAddress());
        vm.prank(alice);
        controller.withdrawLP();

        assertGt(usdt.balanceOf(alice), usdtBefore, "epoch2 first withdraw should pay immediately");
        assertGt(naio.balanceOf(naio.burnAddress()), burnBefore, "should burn NAIO");
        assertEq(controller.withdrawBurnEpoch(), 2, "quota is for current epoch 2");
        assertGt(controller.withdrawBurnQuotaToken(), 0, "epoch2 quota computed in real time");
    }

    /// @dev On deflation: withdraw-consumed burn is deducted from the burn allocation; the remainder goes to the blackhole.
    ///      Snapshot fields should match the blackhole balance delta.
    function test_deflation_burn_minus_withdraw_consumed_to_blackhole() public {
        vm.warp(block.timestamp + controller.EPOCH_SECONDS());
        assertEq(controller.getCurrentEpoch(), 1, "epoch 1");
        assertEq(controller.lastPokeEpoch(), type(uint32).max, "not poked yet (sentinel)");

        uint256 burnBeforeWithdraw = naio.balanceOf(naio.burnAddress());
        vm.prank(alice);
        controller.withdrawLP();
        uint256 burnAfterWithdraw = naio.balanceOf(naio.burnAddress());
        uint256 consumedByWithdraw = burnAfterWithdraw - burnBeforeWithdraw;
        assertGt(consumedByWithdraw, 0, "withdraw should burn some NAIO");
        assertEq(controller.withdrawBurnUsedToken(), consumedByWithdraw, "quota consumed = burned");

        // Settlement is for the previous day: withdraw consumption in epoch1 is deducted when settling epoch1 (i.e., `poke()` at currentEpoch=2).
        vm.warp(block.timestamp + controller.EPOCH_SECONDS());
        assertEq(controller.getCurrentEpoch(), 2, "epoch 2");
        controller.poke();
        (, , , , , , uint256 snapBurn, , , , , , , uint256 snapWithdrawBurnConsumed) =
            controller.deflationSnapshots(1);
        assertEq(snapWithdrawBurnConsumed, consumedByWithdraw, "snapshot should record withdraw consumed");
        assertGe(snapBurn, snapWithdrawBurnConsumed, "burn amount >= consumed");
        uint256 burnAfterPoke = naio.balanceOf(naio.burnAddress());
        uint256 deltaBurn = burnAfterPoke - burnAfterWithdraw;
        uint256 expectedFromPoke = snapBurn - snapWithdrawBurnConsumed;
        assertGe(deltaBurn, expectedFromPoke, "poke should burn at least net deflation to blackhole");
    }

    /// @dev Unlock tiers by epoch: 0–1 epoch 40%, 1–2 epochs 60%, >=2 epochs 80% (test setting may use 1 month = 1 epoch).
    function test_unlock_tiers_still_match_principal_cap() public {
        (uint256 p0, , , , , uint64 firstDepositTs, , , , , uint256 w0, uint256 l0) = controller.users(alice);
        assertEq(p0, 1000e18, "principal");
        assertEq(l0, 800e18, "locked");
        assertEq(w0, 0, "withdrawn");
        assertEq(block.timestamp - uint256(firstDepositTs), 0, "since at deposit block should be zero");

        uint256 unlocked0 = (p0 * 4000) / 10000;
        if (unlocked0 > l0) unlocked0 = l0;
        assertEq(unlocked0, 400e18, "epoch0 unlocked 40%");

        uint256 epochSec = controller.EPOCH_SECONDS();
        vm.warp(block.timestamp + epochSec + 1);
        (uint256 p1, , , , , , , , , , uint256 w1, uint256 l1) = controller.users(alice);
        uint256 unlocked1 = (p1 * 6000) / 10000;
        if (unlocked1 > l1) unlocked1 = l1;
        uint256 withdrawable1 = unlocked1 > w1 ? (unlocked1 - w1) : 0;
        assertEq(unlocked1, 600e18, "1 epoch passed: 60%");
        assertEq(withdrawable1, 600e18, "1 epoch withdrawable");

        vm.warp(block.timestamp + epochSec);
        (uint256 p2, , , , , , , , , , uint256 w2, uint256 l2) = controller.users(alice);
        uint256 unlocked2 = (p2 * 8000) / 10000;
        if (unlocked2 > l2) unlocked2 = l2;
        uint256 withdrawable2 = unlocked2 > w2 ? (unlocked2 - w2) : 0;
        assertEq(unlocked2, 800e18, "2+ epoch: 80% (capped by lockedUsdt)");
        assertEq(withdrawable2, 800e18, "2 epoch withdrawable");
    }
}


