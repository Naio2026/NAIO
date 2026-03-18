// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

import {KeeperCouncil} from "../src/KeeperCouncil.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {NAIOToken} from "../src/NAIOToken.sol";
import {DepositWitnessRuleEngine} from "../src/DepositWitnessRuleEngine.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract KeeperCouncilTest is Test {
    MockERC20 usdt;
    NAIOToken naio;
    NAIOController controller;
    DepositWitnessRuleEngine engine;
    KeeperCouncil council;

    address[5] members;
    address newKeeper = vm.addr(2001);
    address newValidator = vm.addr(2002);
    address replacement = vm.addr(3001);
    address signer1 = vm.addr(4001);
    address signer2 = vm.addr(4002);
    address signer3 = vm.addr(4003);
    address signer4 = vm.addr(4004);

    function setUp() public {
        usdt = new MockERC20("USDT", "USDT", 18);
        naio = new NAIOToken("NAIO Token", "NAIO", 100_000_000e18, address(0xdead), address(0xC0FFEE));
        controller = new NAIOController(address(usdt), address(naio));

        for (uint256 i = 0; i < 5; i++) {
            members[i] = vm.addr(100 + uint256(i));
        }

        address[] memory signers = new address[](3);
        signers[0] = signer1;
        signers[1] = signer2;
        signers[2] = signer3;
        engine = new DepositWitnessRuleEngine(address(controller), signers, 3, 0);
        controller.setDepositRuleEngine(address(engine));

        council = new KeeperCouncil(address(controller), members);

        controller.setKeeperGovernor(address(council), true);
        controller.transferOwnership(address(council));
    }

    function _approveByMembers(uint256 proposalId, uint256 count, address proposer) internal {
        uint256 approved = 1; // proposer auto-approves on create
        for (uint256 i = 0; i < members.length && approved < count; i++) {
            if (members[i] == proposer) continue;
            vm.prank(members[i]);
            council.approveProposal(proposalId);
            approved++;
        }
    }

    function test_keeper_op_requires_3_of_5() public {
        bytes memory data = abi.encodeWithSelector(controller.setKeeperByGovernor.selector, newKeeper);
        vm.prank(members[0]);
        uint256 proposalId = council.createKeeperProposal(data);

        // only 2 approvals: should fail
        vm.prank(members[1]);
        council.approveProposal(proposalId);
        vm.prank(members[0]);
        vm.expectRevert(bytes("INSUFFICIENT_APPROVALS"));
        council.executeProposal(proposalId);

        // third approval unlocks execution
        vm.prank(members[2]);
        council.approveProposal(proposalId);
        vm.prank(members[0]);
        council.executeProposal(proposalId);

        assertTrue(controller.keepers(newKeeper), "new keeper should be enabled");
        assertEq(controller.keeper(), newKeeper, "keeper slot should be updated");
    }

    function test_keeper_op_can_set_validator_and_pause() public {
        bytes memory setValidator = abi.encodeWithSelector(controller.setValidatorGuardianByGovernor.selector, newValidator);
        vm.prank(members[0]);
        uint256 p1 = council.createKeeperProposal(setValidator);
        _approveByMembers(p1, 3, members[0]);
        vm.prank(members[0]);
        council.executeProposal(p1);
        assertEq(controller.validatorGuardian(), newValidator);

        bytes memory pause = abi.encodeWithSelector(controller.setKeeperAccountingPausedByGovernor.selector, true);
        vm.prank(members[1]);
        uint256 p2 = council.createKeeperProposal(pause);
        _approveByMembers(p2, 3, members[1]);
        vm.prank(members[1]);
        council.executeProposal(p2);
        assertTrue(controller.keeperAccountingPaused());
    }

    function test_member_replace_requires_5_of_5() public {
        vm.prank(members[0]);
        uint256 proposalId = council.createMemberReplaceProposal(0, replacement);

        for (uint256 i = 1; i < 4; i++) {
            vm.prank(members[i]);
            council.approveProposal(proposalId);
        }
        vm.prank(members[0]);
        vm.expectRevert(bytes("INSUFFICIENT_APPROVALS"));
        council.executeProposal(proposalId);

        vm.prank(members[4]);
        council.approveProposal(proposalId);

        vm.prank(members[0]);
        council.executeProposal(proposalId);

        assertTrue(!council.isMember(members[0]), "old member should be removed");
        assertTrue(council.isMember(replacement), "new member should be active");
        assertEq(council.members(0), replacement);
    }

    function test_replace_witness_signer_requires_5_of_5() public {
        assertTrue(engine.isWitnessSigner(signer1), "old signer should exist");
        assertTrue(!engine.isWitnessSigner(signer4), "new signer should not exist");

        bytes memory data =
            abi.encodeWithSelector(controller.replaceWitnessSignerByGovernor.selector, signer1, signer4);

        vm.prank(members[0]);
        uint256 proposalId = council.createKeeperProposal(data);

        for (uint256 i = 1; i < 4; i++) {
            vm.prank(members[i]);
            council.approveProposal(proposalId);
        }

        vm.prank(members[0]);
        vm.expectRevert(bytes("INSUFFICIENT_APPROVALS"));
        council.executeProposal(proposalId);

        vm.prank(members[4]);
        council.approveProposal(proposalId);

        vm.prank(members[0]);
        council.executeProposal(proposalId);

        assertTrue(!engine.isWitnessSigner(signer1), "old signer should be removed");
        assertTrue(engine.isWitnessSigner(signer4), "new signer should be added");
        assertEq(engine.witnessSignerCount(), 3, "signer count should stay unchanged");
        assertEq(engine.witnessThreshold(), 3, "threshold should stay unchanged");
    }

    function test_rejects_unsupported_keeper_selector() public {
        bytes memory bad = abi.encodeWithSelector(controller.transferOwnership.selector, vm.addr(4040));
        vm.prank(members[0]);
        vm.expectRevert(bytes("UNSUPPORTED_SELECTOR"));
        council.createKeeperProposal(bad);
    }

    function test_set_controller_requires_5_of_5() public {
        NAIOController controller2 = new NAIOController(address(usdt), address(naio));

        vm.prank(members[0]);
        uint256 proposalId = council.createSetControllerProposal(address(controller2));

        vm.prank(members[0]);
        vm.expectRevert(bytes("INSUFFICIENT_APPROVALS"));
        council.executeProposal(proposalId);

        vm.prank(members[1]);
        council.approveProposal(proposalId);
        vm.prank(members[2]);
        council.approveProposal(proposalId);
        vm.prank(members[3]);
        council.approveProposal(proposalId);

        vm.prank(members[0]);
        vm.expectRevert(bytes("INSUFFICIENT_APPROVALS"));
        council.executeProposal(proposalId);

        vm.prank(members[4]);
        council.approveProposal(proposalId);

        vm.prank(members[0]);
        council.executeProposal(proposalId);
        assertEq(council.controller(), address(controller2));
    }

}
