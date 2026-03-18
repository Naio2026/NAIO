// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

import {NAIOToken} from "../src/NAIOToken.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {DepositWitnessRuleEngine} from "../src/DepositWitnessRuleEngine.sol";
import {NodeSeatPool} from "../src/pools/NodeSeatPool.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract ClaimAllTest is Test {
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

        naio.transfer(address(controller), 100_000_000e18);

        nodePool = new NodeSeatPool(address(usdt), address(naio));
        nodePool.setController(address(controller));
        _init1000Seats();
        nodePool.seal();

        controller.setPools(address(nodePool), marketE, marketF);

        // Set ecoPool to Alice to create pendingNaio[alice] (claimAll will invoke claimDynamicFor)
        controller.setRewardReceivers(alice, independentB);

        controller.setKeeper(keeper);
        address[] memory signers = new address[](3);
        signers[0] = vm.addr(w1Pk);
        signers[1] = vm.addr(w2Pk);
        signers[2] = vm.addr(w3Pk);
        engine = new DepositWitnessRuleEngine(address(controller), signers, 3, 0);
        controller.setDepositRuleEngine(address(engine));
        naio.setController(address(controller));

        usdt.mint(alice, 1_000e18);
        vm.deal(alice, 10 ether);
        vm.deal(nodeOwner, 10 ether);
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

    function _sig(uint256 pk, bytes32 digest) internal returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, v);
    }

    function test_claimAll_claims_static_and_pendingNaio_and_is_idempotent() public {
        _depositByTransfer(alice, 100e18, bytes32(uint256(7001)));

        // poke: generate static rewards + ecoPool(Alice) pendingNaio
        vm.warp(block.timestamp + controller.EPOCH_SECONDS());
        controller.poke();

        assertGt(controller.pendingNaio(alice), 0, "alice should have pendingNaio from eco allocation");

        uint256 aliceNaioBefore = naio.balanceOf(alice);
        vm.prank(alice);
        controller.claimAll();

        assertEq(controller.pendingNaio(alice), 0, "claimAll should clear pendingNaio via claimDynamicFor");
        assertGt(naio.balanceOf(alice), aliceNaioBefore, "claimAll should increase alice NAIO");

        // Second call should not revert (and should not pay more; pendingNaio should already be 0)
        uint256 aliceNaioMid = naio.balanceOf(alice);
        vm.prank(alice);
        controller.claimAll();
        assertEq(controller.pendingNaio(alice), 0);
        assertEq(naio.balanceOf(alice), aliceNaioMid, "second claimAll should be no-op here");
    }

    function test_claimAll_claims_node_dividends_for_node_owner() public {
        // Alice deposits first to fund the node pool with USDT; then `poke()` funds the node pool with NAIO
        _depositByTransfer(alice, 100e18, bytes32(uint256(7002)));
        vm.warp(block.timestamp + controller.EPOCH_SECONDS());
        controller.poke();

        uint256 usdtBefore = usdt.balanceOf(nodeOwner);
        uint256 naioBefore = naio.balanceOf(nodeOwner);

        vm.prank(nodeOwner);
        controller.claimAll();

        assertGt(usdt.balanceOf(nodeOwner), usdtBefore, "nodeOwner should receive USDT node dividend");
        assertGt(naio.balanceOf(nodeOwner), naioBefore, "nodeOwner should receive NAIO node dividend");
    }
}

