// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

import {NAIOToken} from "../src/NAIOToken.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {DepositWitnessRuleEngine} from "../src/DepositWitnessRuleEngine.sol";
import {NodeSeatPool} from "../src/pools/NodeSeatPool.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract DepositWitnessRuleEngineTest is Test {
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

    address keeper = vm.addr(301);
    address alice = vm.addr(201);
    address bob = vm.addr(202);

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
        naio.setController(address(controller));

        address[] memory signers = new address[](3);
        signers[0] = vm.addr(w1Pk);
        signers[1] = vm.addr(w2Pk);
        signers[2] = vm.addr(w3Pk);
        engine = new DepositWitnessRuleEngine(address(controller), signers, 3, 0);
        controller.setDepositRuleEngine(address(engine));

        usdt.mint(alice, 5_000_000e18);
        usdt.mint(bob, 5_000_000e18);
    }

    function _init1000Seats() internal {
        uint16 start = 1;
        uint16 seatId = 1;
        for (uint256 batch = 0; batch < 10; batch++) {
            address[] memory owners = new address[](100);
            for (uint256 i = 0; i < 100; i++) {
                owners[i] = address(uint160(0x100000 + seatId));
                seatId++;
            }
            nodePool.setInitialOwners(start, owners);
            start += 100;
        }
        assertEq(nodePool.seatCount(), 1000);
    }

    function _sig(uint256 pk, bytes32 digest) internal returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, v);
    }

    function _build3Sigs(bytes32 digest) internal returns (bytes[] memory sigs) {
        sigs = new bytes[](3);
        sigs[0] = _sig(w1Pk, digest);
        sigs[1] = _sig(w2Pk, digest);
        sigs[2] = _sig(w3Pk, digest);
    }

    function _principal(address user) internal view returns (uint256 principalUsdt) {
        (principalUsdt, , , , , , , , , , ,) = controller.users(user);
    }

    // depositFromTransfer was removed (EIP-170 code-size limit); production uses depositFromTransferWitness.

    function test_rule_engine_can_only_be_set_once() public {
        address[] memory signers = new address[](3);
        signers[0] = vm.addr(0xB11);
        signers[1] = vm.addr(0xB12);
        signers[2] = vm.addr(0xB13);
        DepositWitnessRuleEngine engine2 = new DepositWitnessRuleEngine(address(controller), signers, 3, 0);

        vm.expectRevert(NAIOController.RuleEngineImmutable.selector);
        controller.setDepositRuleEngine(address(engine2));
    }

    function test_witness_3of3_deposit_success() public {
        bytes32 txHash = bytes32(uint256(9101));
        uint256 amount = 100e18;
        uint256 deadline = block.timestamp + 600;

        vm.prank(alice);
        usdt.transfer(address(controller), amount);

        bytes32 digest = engine.witnessDigest(alice, amount, txHash, deadline);
        bytes[] memory sigs = _build3Sigs(digest);

        vm.prank(keeper);
        controller.depositFromTransferWitness(alice, amount, txHash, deadline, sigs);

        assertEq(_principal(alice), amount, "witness deposit should be booked");
    }

    function test_same_block_unprocessed_inflow_cannot_open_gt1000() public {
        // Bob first sends a huge inflow in the same block (not booked), attempting to skew balance-based checks.
        vm.prank(bob);
        usdt.transfer(address(controller), 2_000_000e18);

        bytes32 txHash = bytes32(uint256(9201));
        uint256 amount = 2_000e18;
        uint256 deadline = block.timestamp + 600;

        uint256 balBefore = usdt.balanceOf(alice);
        vm.prank(alice);
        usdt.transfer(address(controller), amount);

        bytes32 digest = engine.witnessDigest(alice, amount, txHash, deadline);
        bytes[] memory sigs = _build3Sigs(digest);

        vm.expectEmit(true, true, true, true);
        emit DepositRefunded(alice, amount, txHash, 2);
        vm.prank(keeper);
        controller.depositFromTransferWitness(alice, amount, txHash, deadline, sigs);

        assertEq(usdt.balanceOf(alice), balBefore, "should refund when >1000 before rule-pool reaches 1m");
        assertEq(_principal(alice), 0, "principal must remain zero after refund");
    }
}

