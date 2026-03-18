// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

import {NAIOToken} from "../src/NAIOToken.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {DepositWitnessRuleEngine} from "../src/DepositWitnessRuleEngine.sol";
import {NodeSeatPool} from "../src/pools/NodeSeatPool.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract DepositorAuthTest is Test {
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

    address nodeOwner = vm.addr(101); // seatId=1
    address alice = vm.addr(201);
    address keeper = vm.addr(301);
    address validator = vm.addr(302);
    address keeperGovernor = vm.addr(303);
    address attacker = vm.addr(999);
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
        controller.setKeeperGovernor(keeperGovernor, true);
        controller.setValidatorGuardian(validator);
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
        vm.deal(attacker, 10 ether);
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
        assertEq(nodePool.seatOf(nodeOwner), 1);
    }

    function _sig(uint256 pk, bytes32 digest) internal returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, v);
    }

    function _sigs(address user, uint256 amt, bytes32 txHash, uint256 deadline) internal returns (bytes[] memory out) {
        bytes32 digest = engine.witnessDigest(user, amt, txHash, deadline);
        out = new bytes[](3);
        out[0] = _sig(w1Pk, digest);
        out[1] = _sig(w2Pk, digest);
        out[2] = _sig(w3Pk, digest);
    }

    function _depositAs(address caller, address user, uint256 amt, bytes32 txHash) internal {
        vm.prank(user);
        usdt.transfer(address(controller), amt);
        uint256 deadline = block.timestamp + 600;
        bytes[] memory sigs = _sigs(user, amt, txHash, deadline);
        vm.prank(caller);
        controller.depositFromTransferWitness(user, amt, txHash, deadline, sigs);
    }

    function test_depositFromTransfer_rejects_unauthorized_caller() public {
        vm.prank(alice);
        usdt.transfer(address(controller), 100e18);

        uint256 deadline = block.timestamp + 600;
        bytes[] memory sigs = _sigs(alice, 100e18, bytes32(uint256(6001)), deadline);
        vm.prank(attacker);
        vm.expectRevert(NAIOController.NotKeeper.selector);
        controller.depositFromTransferWitness(alice, 100e18, bytes32(uint256(6001)), deadline, sigs);
    }

    function test_depositFromTransfer_allows_keeper() public {
        _depositAs(keeper, alice, 100e18, bytes32(uint256(6002)));
    }

    function test_depositFromTransfer_rejects_without_fresh_inflow() public {
        uint256 deadline = block.timestamp + 600;
        bytes[] memory sigs = _sigs(alice, 100e18, bytes32(uint256(60021)), deadline);
        vm.prank(keeper);
        vm.expectRevert(NAIOController.NoFreshInflow.selector);
        controller.depositFromTransferWitness(alice, 100e18, bytes32(uint256(60021)), deadline, sigs);
    }

    function test_depositFromTransfer_works_after_usdt_outflow() public {
        // Process a normal deposit first to create market/ops pendingUsdt.
        _depositAs(keeper, alice, 100e18, bytes32(uint256(60022)));

        // Fixed receiver claiming USDT causes Controller USDT outflow.
        vm.prank(marketE);
        controller.claimUsdt();

        // A new deposit should still be processed correctly (not impacted by the outflow).
        _depositAs(keeper, alice, 100e18, bytes32(uint256(60023)));
    }

    function test_validator_can_veto_pause_keeper_accounting_and_force_refund() public {
        vm.prank(validator);
        controller.validatorVetoPause();
        assertTrue(controller.keeperAccountingPaused(), "keeper accounting should be paused");

        vm.prank(alice);
        usdt.transfer(address(controller), 100e18);

        bytes32 txHash = bytes32(uint256(60024));
        vm.expectEmit(true, true, true, true);
        emit DepositRefunded(alice, 100e18, txHash, 3);

        uint256 deadline = block.timestamp + 600;
        bytes[] memory sigs = _sigs(alice, 100e18, txHash, deadline);
        vm.prank(keeper);
        controller.depositFromTransferWitness(alice, 100e18, txHash, deadline, sigs);

        (uint256 principalUsdt, , , , , , , , , , ,) = controller.users(alice);
        assertEq(principalUsdt, 0, "paused mode should not book principal");
        assertTrue(controller.processedTransfers(txHash), "paused-mode refund should mark tx processed");
    }

    function test_owner_can_resume_after_veto() public {
        vm.prank(validator);
        controller.validatorVetoPause();
        assertTrue(controller.keeperAccountingPaused());

        controller.setKeeperAccountingPaused(false);
        assertTrue(!controller.keeperAccountingPaused(), "owner should be able to resume");

        _depositAs(keeper, alice, 100e18, bytes32(uint256(60025)));
    }

    function test_keeper_governor_can_rotate_keeper_after_owner_renounced() public {
        address newKeeper = vm.addr(304);

        // Simulate ownership renounced after deployment
        controller.renounceOwnership();

        vm.prank(keeperGovernor);
        controller.setKeeperStatusByGovernor(keeper, false);
        vm.prank(keeperGovernor);
        controller.setKeeperByGovernor(newKeeper);

        assertTrue(!controller.keepers(keeper), "old keeper should be disabled");
        assertTrue(controller.keepers(newKeeper), "new keeper should be enabled");
        assertEq(controller.keeper(), newKeeper, "keeper slot should point to new keeper");
    }

    function test_depositFromTransfer_rejects_node_seat_holder_when_flag_disabled() public {
        // Disable keeper to ensure it does not pass keeper allowlist checks
        controller.setKeeperStatus(keeper, false);

        vm.prank(alice);
        usdt.transfer(address(controller), 100e18);

        // Default allowSeatDepositors=false: seat holder cannot call
        uint256 deadline = block.timestamp + 600;
        bytes[] memory sigs = _sigs(alice, 100e18, bytes32(uint256(6003)), deadline);
        vm.prank(nodeOwner);
        vm.expectRevert(NAIOController.NotKeeper.selector);
        controller.depositFromTransferWitness(alice, 100e18, bytes32(uint256(6003)), deadline, sigs);
    }

    function test_depositFromTransfer_allows_node_seat_holder_when_flag_enabled() public {
        // Disable keeper to ensure authorization relies on seat ownership
        controller.setKeeperStatus(keeper, false);
        controller.setAllowSeatDepositors(true);

        _depositAs(nodeOwner, alice, 100e18, bytes32(uint256(6004)));
    }
}

