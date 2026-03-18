// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

import {NAIOToken} from "../src/NAIOToken.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {DepositWitnessRuleEngine} from "../src/DepositWitnessRuleEngine.sol";
import {NodeSeatPool} from "../src/pools/NodeSeatPool.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract DepositRefundTest is Test {
    event DepositRefunded(address indexed user, uint256 usdtAmount, bytes32 indexed txHash, uint8 reason);

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
    address validator = vm.addr(302);
    uint256 w1Pk = 0xA11;
    uint256 w2Pk = 0xA12;
    uint256 w3Pk = 0xA13;

    function setUp() public {
        // Ensure timestamp is initialized for future poke/withdraw tests
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
        controller.setValidatorGuardian(validator);
        address[] memory signers = new address[](3);
        signers[0] = vm.addr(w1Pk);
        signers[1] = vm.addr(w2Pk);
        signers[2] = vm.addr(w3Pk);
        engine = new DepositWitnessRuleEngine(address(controller), signers, 3, 0);
        controller.setDepositRuleEngine(address(engine));

        naio.setController(address(controller));

        usdt.mint(alice, 2_500_000e18);
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

    function _userPrincipal(address user) internal view returns (uint256 principalUsdt) {
        (principalUsdt, , , , , , , , , , ,) = controller.users(user);
    }

    function _sig(uint256 pk, bytes32 digest) internal returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, v);
    }

    function _depositByWitness(address user, uint256 amt, bytes32 txHash) internal {
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

    function test_refund_when_amount_lt_100() public {
        bytes32 txHash = bytes32(uint256(1001));

        uint256 balBefore = usdt.balanceOf(alice);
        vm.prank(alice);
        usdt.transfer(address(controller), 99e18);

        vm.expectEmit(true, true, true, true);
        emit DepositRefunded(alice, 99e18, txHash, 1);

        uint256 deadline = block.timestamp + 600;
        bytes32 digest = engine.witnessDigest(alice, 99e18, txHash, deadline);
        bytes[] memory sigs = new bytes[](3);
        sigs[0] = _sig(w1Pk, digest);
        sigs[1] = _sig(w2Pk, digest);
        sigs[2] = _sig(w3Pk, digest);
        vm.prank(keeper);
        controller.depositFromTransferWitness(alice, 99e18, txHash, deadline, sigs);

        assertEq(usdt.balanceOf(alice), balBefore, "refund should restore balance");
        assertEq(_userPrincipal(alice), 0, "no deposit recorded");
        assertTrue(controller.processedTransfers(txHash), "txHash should be marked processed");

        vm.prank(keeper);
        vm.expectRevert(NAIOController.AlreadyProcessed.selector);
        controller.depositFromTransferWitness(alice, 99e18, txHash, deadline, sigs);
    }

    function test_refund_when_amount_gt_1000_and_poolBefore_lt_1m() public {
        bytes32 txHash = bytes32(uint256(1002));

        uint256 balBefore = usdt.balanceOf(alice);
        vm.prank(alice);
        usdt.transfer(address(controller), 1001e18);

        vm.expectEmit(true, true, true, true);
        emit DepositRefunded(alice, 1001e18, txHash, 2);

        uint256 deadline = block.timestamp + 600;
        bytes32 digest = engine.witnessDigest(alice, 1001e18, txHash, deadline);
        bytes[] memory sigs = new bytes[](3);
        sigs[0] = _sig(w1Pk, digest);
        sigs[1] = _sig(w2Pk, digest);
        sigs[2] = _sig(w3Pk, digest);
        vm.prank(keeper);
        controller.depositFromTransferWitness(alice, 1001e18, txHash, deadline, sigs);

        assertEq(usdt.balanceOf(alice), balBefore, "refund should restore balance");
        assertEq(_userPrincipal(alice), 0, "no deposit recorded");
        assertTrue(controller.processedTransfers(txHash), "txHash should be marked processed");
    }

    function test_refund_when_accounting_paused_by_validator_veto() public {
        bytes32 txHash = bytes32(uint256(1009));

        vm.prank(validator);
        controller.validatorVetoPause();
        assertTrue(controller.keeperAccountingPaused(), "keeper accounting should be paused");

        uint256 balBefore = usdt.balanceOf(alice);
        vm.prank(alice);
        usdt.transfer(address(controller), 100e18);

        vm.expectEmit(true, true, true, true);
        emit DepositRefunded(alice, 100e18, txHash, 3);

        uint256 deadline = block.timestamp + 600;
        bytes32 digest = engine.witnessDigest(alice, 100e18, txHash, deadline);
        bytes[] memory sigs = new bytes[](3);
        sigs[0] = _sig(w1Pk, digest);
        sigs[1] = _sig(w2Pk, digest);
        sigs[2] = _sig(w3Pk, digest);
        vm.prank(keeper);
        controller.depositFromTransferWitness(alice, 100e18, txHash, deadline, sigs);

        assertEq(usdt.balanceOf(alice), balBefore, "refund should restore balance");
        assertEq(_userPrincipal(alice), 0, "paused mode should not record deposit");
        assertTrue(controller.processedTransfers(txHash), "txHash should be marked processed");
    }

    function test_allow_small_topup_after_reaching_100_before_1m() public {
        bytes32 txHash1 = bytes32(uint256(1005));
        bytes32 txHash2 = bytes32(uint256(1006));

        // First deposit 100U should succeed
        _depositByWitness(alice, 100e18, txHash1);

        // Subsequent small top-up 50U should be allowed (cumulative >=100 and <=1000)
        _depositByWitness(alice, 50e18, txHash2);

        assertEq(_userPrincipal(alice), 150e18, "cumulative deposits should be recorded");
    }

    function test_refund_when_cumulative_gt_1000_before_1m() public {
        bytes32 txHash1 = bytes32(uint256(1007));
        bytes32 txHash2 = bytes32(uint256(1008));

        // First deposit 900U should succeed
        _depositByWitness(alice, 900e18, txHash1);

        // Second deposit 200U makes cumulative >1000, should be refunded
        uint256 balBefore = usdt.balanceOf(alice);
        vm.prank(alice);
        usdt.transfer(address(controller), 200e18);

        vm.expectEmit(true, true, true, true);
        emit DepositRefunded(alice, 200e18, txHash2, 2);

        uint256 deadline = block.timestamp + 600;
        bytes32 digest = engine.witnessDigest(alice, 200e18, txHash2, deadline);
        bytes[] memory sigs = new bytes[](3);
        sigs[0] = _sig(w1Pk, digest);
        sigs[1] = _sig(w2Pk, digest);
        sigs[2] = _sig(w3Pk, digest);
        vm.prank(keeper);
        controller.depositFromTransferWitness(alice, 200e18, txHash2, deadline, sigs);

        assertEq(usdt.balanceOf(alice), balBefore, "refund should restore balance");
        assertEq(_userPrincipal(alice), 900e18, "principal should not increase after refund");
    }

    function test_allow_amount_gt_1000_after_pool_reaches_1m() public {
        // To avoid initial funding being offset by market/ops allocations, route deposit allocation 100% to the pool first.
        controller.setDepositBps(0, 0, 0, 10000);

        // Use owner initial funding to raise rulePool as well (minting to controller alone does not update rulePoolUsdt).
        address bootstrapUser = vm.addr(9090);
        usdt.mint(address(controller), 1_000_000e18);
        controller.depositInitialFunding(bootstrapUser, 1_000_000e18);

        bytes32 txHash = bytes32(uint256(1003));

        _depositByWitness(alice, 5000e18, txHash);

        assertEq(_userPrincipal(alice), 5000e18, "deposit should be recorded after pool>=1m");
        assertTrue(controller.processedTransfers(txHash), "txHash should be marked processed");
    }

    function test_poolBefore_guard_prevents_bypass() public {
        // poolBefore < 1m, but poolNow may become >=1m due to this transfer; must still apply the "before1m" cap (refund).
        usdt.mint(address(controller), 999_900e18);

        bytes32 txHash = bytes32(uint256(1004));

        uint256 balBefore = usdt.balanceOf(alice);
        vm.prank(alice);
        usdt.transfer(address(controller), 2000e18);

        vm.expectEmit(true, true, true, true);
        emit DepositRefunded(alice, 2000e18, txHash, 2);

        uint256 deadline = block.timestamp + 600;
        bytes32 digest = engine.witnessDigest(alice, 2000e18, txHash, deadline);
        bytes[] memory sigs = new bytes[](3);
        sigs[0] = _sig(w1Pk, digest);
        sigs[1] = _sig(w2Pk, digest);
        sigs[2] = _sig(w3Pk, digest);
        vm.prank(keeper);
        controller.depositFromTransferWitness(alice, 2000e18, txHash, deadline, sigs);

        assertEq(usdt.balanceOf(alice), balBefore, "refund should restore balance");
        assertEq(_userPrincipal(alice), 0, "no deposit recorded");
        assertTrue(controller.processedTransfers(txHash), "txHash should be marked processed");
    }
}

