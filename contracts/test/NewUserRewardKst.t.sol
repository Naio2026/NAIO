// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

import {NAIOToken} from "../src/NAIOToken.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {DepositWitnessRuleEngine} from "../src/DepositWitnessRuleEngine.sol";
import {NodeSeatPool} from "../src/pools/NodeSeatPool.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract NewUserRewardEpochTest is Test {
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
    address keeper = vm.addr(301);
    uint256 w1Pk = 0xA11;
    uint256 w2Pk = 0xA12;
    uint256 w3Pk = 0xA13;

    function _epoch(uint256 ts, uint256 startTs, uint256 epochSeconds) internal pure returns (uint32) {
        if (ts <= startTs || epochSeconds == 0) return 0;
        return uint32((ts - startTs) / epochSeconds);
    }

    function setUp() public {
        vm.warp(1_700_000_000);

        usdt = new MockERC20("USDT", "USDT", 18);
        naio = new NAIOToken("NAIO Token", "NAIO", 100_000_000e18, address(0xdead), taxReceiver);
        controller = new NAIOController(address(usdt), address(naio));

        // Transfer NAIO into the controller first, then setController (avoid being treated as a sell)
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

        usdt.mint(alice, 10_000e18);
        vm.deal(alice, 10 ether);
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

    function _depositByTransfer(address user, uint256 amt, bytes32 txHash) internal {
        vm.prank(user);
        usdt.transfer(address(controller), amt);
        uint256 deadline = block.timestamp + 600;
        bytes32 digest = engine.witnessDigest(user, amt, txHash, deadline);
        bytes[] memory sigs = new bytes[](3);
        sigs[0] = _sig(w1Pk, digest);
        sigs[1] = _sig(w2Pk, digest);
        sigs[2] = _sig(w3Pk, digest);
        vm.prank(keeper);
        controller.depositFromTransferWitness(user, amt, txHash, deadline, sigs);
    }

    function _sig(uint256 pk, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, v);
    }

    function _op(address user, uint256 opValue) internal {
        vm.prank(user);
        (bool ok, bytes memory data) = address(controller).call{value: opValue}("");
        if (!ok) {
            assembly {
                revert(add(data, 0x20), mload(data))
            }
        }
    }

    function test_new_user_reward_rollover_when_prev_epoch_no_eligible() public {
        uint256 epochSec = controller.EPOCH_SECONDS();
        // Epoch0: first deposit starts systemStartTs; the first deposit does not count toward reinvest weight
        _depositByTransfer(alice, 100e18, bytes32(uint256(8001)));
        uint32 e0 = _epoch(block.timestamp, controller.systemStartTs(), epochSec);

        assertEq(controller.newUserTotalPowerByDay(e0), 0, "epoch0 should have 0 eligible total power");

        // Epoch1: first time `poke()` is available (settles epoch0)
        vm.warp(block.timestamp + epochSec);
        uint32 e1 = _epoch(block.timestamp, controller.systemStartTs(), epochSec);
        assertEq(e1, e0 + 1, "epoch should advance by 1");
        controller.poke();

        uint256 e0Reward = controller.newUserRewardNaioByDay(e0);
        assertGt(e0Reward, 0, "epoch0 new user reward should be >0 after first poke");

        // Epoch2: `poke()` is allowed even without deposits; when settling epoch1 it can roll over the previous epoch's (epoch0) unclaimed 1% into epoch1
        vm.warp(block.timestamp + epochSec);
        uint32 e2 = _epoch(block.timestamp, controller.systemStartTs(), epochSec);
        assertEq(e2, e1 + 1, "epoch should advance by 1 again");

        controller.poke();

        assertEq(controller.newUserRewardNaioByDay(e0), 0, "epoch0 reward should be rolled and cleared");
        assertGe(controller.newUserRewardNaioByDay(e1), e0Reward, "epoch1 reward should include rolled amount");
    }

    function test_new_user_reward_eligible_only_uses_same_day_deposit_power() public {
        uint256 epochSec = controller.EPOCH_SECONDS();
        // Epoch0 deposit to get power
        _depositByTransfer(alice, 100e18, bytes32(uint256(8101)));
        (, uint256 powerAfterE0,,,,,,,,,,) = controller.users(alice);

        // Epoch1: only power generated by today's deposits should count into eligible power
        vm.warp(block.timestamp + epochSec);
        uint32 e1 = _epoch(block.timestamp, controller.systemStartTs(), epochSec);
        (, uint256 powerBeforeE1,,,,,,,,,,) = controller.users(alice);

        _depositByTransfer(alice, 100e18, bytes32(uint256(8102)));
        _depositByTransfer(alice, 200e18, bytes32(uint256(8103)));
        (, uint256 powerAfterE1,,,,,,,,,,) = controller.users(alice);

        uint256 eligible = controller.newUserEligiblePower(e1, alice);
        uint256 sameDayAdded = powerAfterE1 - powerBeforeE1;
        assertGt(eligible, 0, "eligible power should be >0 on epoch1 after deposit");
        assertEq(eligible, sameDayAdded, "eligible power should equal only same-day power added");
        assertLt(eligible, powerAfterE1, "eligible power should not include historical power");
        assertGt(powerAfterE0, 0, "historical power should exist");
        assertEq(controller.newUserTotalPowerByDay(e1), eligible, "only alice eligible => total=eligible");

        // Epoch2: `poke()` settles epoch1 and accrues newUserRewardNaioByDay[e1]
        vm.warp(block.timestamp + epochSec);
        uint32 e2 = _epoch(block.timestamp, controller.systemStartTs(), epochSec);
        assertEq(e2, e1 + 1, "epoch should advance by 1 again");
        controller.poke();
        uint256 pool = controller.newUserRewardNaioByDay(e1);
        assertGt(pool, 0, "epoch1 new user reward pool should be >0");

        // Epoch3: epoch1 reward becomes claimable
        vm.warp(block.timestamp + epochSec);
        uint32 e3 = _epoch(block.timestamp, controller.systemStartTs(), epochSec);
        assertEq(e3, e2 + 1, "epoch should advance by 1 again");

        uint256 aliceBefore = naio.balanceOf(alice);
        uint256 op = controller.OP_CLAIM_NEWUSER();
        _op(alice, op);
        assertGt(naio.balanceOf(alice), aliceBefore, "claim should transfer NAIO to alice");

        // Re-claim should fail (nothing left to claim)
        vm.expectRevert(NAIOController.NoReward.selector);
        _op(alice, op);
    }

    function test_op_claim_new_user_reward_can_claim_historical_epoch() public {
        uint256 epochSec = controller.EPOCH_SECONDS();

        // Epoch0: first deposit (does not count toward newUserEligiblePower)
        _depositByTransfer(alice, 100e18, bytes32(uint256(8201)));

        // Epoch1: second deposit creates eligibility; reward pool is accrued via `poke()` in this epoch
        vm.warp(block.timestamp + epochSec);
        uint32 e1 = _epoch(block.timestamp, controller.systemStartTs(), epochSec);
        _depositByTransfer(alice, 100e18, bytes32(uint256(8202)));
        // Epoch2: `poke()` settles epoch1 and generates epoch1 reward pool
        vm.warp(block.timestamp + epochSec);
        uint32 e2 = _epoch(block.timestamp, controller.systemStartTs(), epochSec);
        assertEq(e2, e1 + 1, "epoch should advance by 1 again");
        controller.poke();
        assertGt(controller.newUserRewardNaioByDay(e1), 0, "epoch1 new user reward pool should be >0");

        // Jump to a later epoch (simulate forgetting to claim in time)
        vm.warp(block.timestamp + epochSec * 9);
        assertEq(controller.getCurrentEpoch(), e2 + 9, "epoch advanced");

        uint256 aliceBefore = naio.balanceOf(alice);
        uint256 op = controller.OP_CLAIM_NEWUSER();
        _op(alice, op);
        assertGt(naio.balanceOf(alice), aliceBefore, "OP should claim historical new user reward");

        // Trigger again should have nothing to claim (reverts NoReward)
        vm.expectRevert(NAIOController.NoReward.selector);
        _op(alice, op);
    }
}

