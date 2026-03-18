// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

import {NAIOToken} from "../src/NAIOToken.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {DepositWitnessRuleEngine} from "../src/DepositWitnessRuleEngine.sol";
import {NodeSeatPool} from "../src/pools/NodeSeatPool.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract PokeCatchupTest is Test {
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

    function _rateBps(uint32 epoch) internal pure returns (uint256) {
        uint256 monthIndex = uint256(epoch) / 5;
        uint256 steps = monthIndex >= 10 ? 10 : monthIndex;
        return 200 + (steps * 10);
    }

    function test_poke_catchup_rollover() public {
        // Epoch0 deposit to start system
        _depositByTransfer(alice, 100e18, bytes32(uint256(9001)));

        // Move to epoch4 so one poke can settle through epoch3 (settle up to currentEpoch-1)
        vm.warp(block.timestamp + 4 * controller.EPOCH_SECONDS());
        controller.poke();

        // Rollover should end at the last settled epoch when no eligible users exist
        assertEq(controller.newUserRewardNaioByDay(0), 0, "epoch0 reward should be rolled");
        assertEq(controller.newUserRewardNaioByDay(1), 0, "epoch1 reward should be rolled");
        assertEq(controller.newUserRewardNaioByDay(2), 0, "epoch2 reward should be rolled");
        assertGt(controller.newUserRewardNaioByDay(3), 0, "epoch3 reward should exist");

        // Catch-up should settle to latest epoch
        assertEq(controller.lastPokeEpoch(), 3, "last epoch should catch up to epoch3");
    }

    function test_poke_catchup_respects_max_epochs() public {
        _depositByTransfer(alice, 100e18, bytes32(uint256(9002)));

        uint32 maxEpochs = controller.MAX_POKE_CATCHUP_EPOCHS();
        vm.warp(block.timestamp + (uint256(maxEpochs) + 5) * controller.EPOCH_SECONDS());

        controller.poke();
        // Sentinel start (-1): at most MAX_POKE_CATCHUP_EPOCHS epochs settled => last settled = maxEpochs-1
        assertEq(controller.lastPokeEpoch(), maxEpochs - 1, "should only catch up to max epochs");
    }

    function test_poke_status_snapshot_and_detailed_event() public {
        _depositByTransfer(alice, 100e18, bytes32(uint256(9003)));

        // move to epoch3 so one poke can settle through epoch2 (settle up to currentEpoch-1)
        uint256 epochSec = controller.EPOCH_SECONDS();
        vm.warp(block.timestamp + 3 * epochSec);

        uint64 startTs = controller.systemStartTs();
        uint32 currentEpochBefore = controller.getCurrentEpoch();
        uint32 lastEpochBefore = controller.lastPokeEpoch();
        // lastPokeEpoch sentinel is uint32.max
        assertEq(lastEpochBefore, type(uint32).max, "last epoch before poke");
        int256 lastEpochBeforeSigned = int256(uint256(lastEpochBefore)) == int256(uint256(type(uint32).max)) ? -1 : int256(uint256(lastEpochBefore));
        uint256 nextPokeTsBefore = uint256(startTs) + uint256(int256(lastEpochBeforeSigned) + 2) * epochSec;
        bool pokeReadyBefore = (currentEpochBefore > 0 && (int256(uint256(currentEpochBefore)) - 1) > lastEpochBeforeSigned);
        uint32 catchupBefore = pokeReadyBefore ? uint32(uint256(int256(uint256(currentEpochBefore)) - 1 - lastEpochBeforeSigned)) : 0;
        uint256 currentRateBefore = currentEpochBefore > 0 ? _rateBps(currentEpochBefore - 1) : 0;
        assertEq(currentEpochBefore, 3, "current epoch before poke");
        assertEq(nextPokeTsBefore, uint256(startTs) + epochSec, "next poke ts before");
        assertTrue(pokeReadyBefore, "poke should be ready");
        assertEq(catchupBefore, 3, "catchup epochs before");
        assertEq(currentRateBefore, _rateBps(2), "rate before");

        vm.recordLogs();
        controller.poke();
        Vm.Log[] memory logs = vm.getRecordedLogs();

        uint32 latestEpoch = controller.lastDeflationSnapshotEpoch();
        (
            uint32 snapEpoch,
            uint64 snapTs,
            uint256 snapRate,
            uint256 snapPrice,
            uint256 snapPoolToken,
            uint256 snapDeflation,
            uint256 snapBurn,
            uint256 snapEco,
            uint256 snapNewUser,
            uint256 snapNode,
            uint256 snapIndependent,
            uint256 snapReferral,
            uint256 snapStatic,
            uint256 snapWithdrawBurnConsumed
        ) = controller.deflationSnapshots(latestEpoch);

        assertEq(snapEpoch, 2, "latest snapshot epoch");
        assertEq(uint256(snapTs), block.timestamp, "snapshot timestamp");
        assertEq(snapRate, _rateBps(2), "snapshot rate");
        assertGt(snapPrice, 0, "snapshot price should be positive");
        assertGt(snapPoolToken, 0, "snapshot pool token should be positive");
        assertGt(snapDeflation, 0, "snapshot deflation should be positive");
        assertEq(
            snapBurn + snapEco + snapNewUser + snapNode + snapIndependent + snapReferral + snapStatic,
            snapDeflation,
            "snapshot split sum mismatch"
        );

        bytes32 sig = keccak256(
            "DeflationExecutedDetailed(uint32,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256)"
        );
        bool found = false;
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics.length == 0 || logs[i].topics[0] != sig) continue;
            uint32 e = uint32(uint256(logs[i].topics[1]));
            if (e != snapEpoch) continue;

            (
                uint256 evRate,
                uint256 evPrice,
                uint256 evPoolToken,
                uint256 evDeflation,
                uint256 evBurn,
                uint256 evEco,
                uint256 evNewUser,
                uint256 evNode,
                uint256 evIndependent,
                uint256 evReferral,
                uint256 evStatic,
                uint256 evWithdrawBurnConsumed
            ) = abi.decode(
                logs[i].data,
                (uint256, uint256, uint256, uint256, uint256, uint256, uint256, uint256, uint256, uint256, uint256, uint256)
            );

            assertEq(evRate, snapRate, "event rate");
            assertEq(evPrice, snapPrice, "event price");
            assertEq(evPoolToken, snapPoolToken, "event pool token");
            assertEq(evDeflation, snapDeflation, "event deflation");
            assertEq(evBurn, snapBurn, "event burn");
            assertEq(evEco, snapEco, "event eco");
            assertEq(evNewUser, snapNewUser, "event new user");
            assertEq(evNode, snapNode, "event node");
            assertEq(evIndependent, snapIndependent, "event independent");
            assertEq(evReferral, snapReferral, "event referral");
            assertEq(evStatic, snapStatic, "event static");
            assertEq(evWithdrawBurnConsumed, snapWithdrawBurnConsumed, "event withdraw burn consumed");
            found = true;
            break;
        }
        assertTrue(found, "missing DeflationExecutedDetailed for latest epoch");

        uint32 currentEpochAfter = controller.getCurrentEpoch();
        uint32 lastEpochAfter = controller.lastPokeEpoch();
        uint256 nextPokeTsAfter = uint256(startTs) + (uint256(lastEpochAfter) + 2) * epochSec;
        bool pokeReadyAfter = (currentEpochAfter > 0 && (currentEpochAfter - 1) > lastEpochAfter);
        uint32 catchupAfter = pokeReadyAfter ? (currentEpochAfter - 1 - lastEpochAfter) : 0;
        uint256 currentRateAfter = currentEpochAfter > 0 ? _rateBps(currentEpochAfter - 1) : 0;

        assertEq(currentEpochAfter, 3, "current epoch after");
        assertEq(lastEpochAfter, 2, "last epoch after");
        assertEq(nextPokeTsAfter, uint256(startTs) + 4 * epochSec, "next poke ts after");
        assertFalse(pokeReadyAfter, "poke should not be ready after");
        assertEq(catchupAfter, 0, "catchup after");
        assertEq(currentRateAfter, _rateBps(2), "rate after");
    }
}
