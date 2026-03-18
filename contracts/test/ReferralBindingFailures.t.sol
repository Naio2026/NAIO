// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

import {NAIOToken} from "../src/NAIOToken.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {NodeSeatPool} from "../src/pools/NodeSeatPool.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract ReferralBindingFailuresTest is Test {
    MockERC20 usdt;
    NAIOToken naio;
    NAIOController controller;
    NodeSeatPool nodePool;

    address taxReceiver = address(0xC0FFEE);
    address ecoA = address(0xA0A0A0);
    address independentB = address(0xB0B0B0);
    address marketE = address(0xE0E0E0);
    address marketF = address(0xF0F0F0);

    address nodeOwner = vm.addr(101);
    address bob = vm.addr(202);
    address alice = vm.addr(203);
    address charlie = vm.addr(204);
    address dave = vm.addr(205);
    address keeper = vm.addr(301);

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

        naio.setController(address(controller));
        // Distribute a small amount of NAIO for establishing referral relations (NAIO transfer triggers binding)
        vm.prank(address(controller));
        naio.transfer(bob, 1e16);
        vm.prank(address(controller));
        naio.transfer(charlie, 1e16);
        vm.prank(address(controller));
        naio.transfer(dave, 1e16);
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

    function _referrerOf(address user) internal view returns (address ref) {
        (, , ref, , , , , , , , ,) = controller.users(user);
    }

    function test_referral_binding_is_one_time_only() public {
        // Bob binds Alice
        vm.prank(bob);
        naio.transfer(alice, 1e15);
        assertEq(_referrerOf(alice), bob);

        // Charlie transfers to Alice again; referrer should not change
        vm.prank(charlie);
        naio.transfer(alice, 1e15);
        assertEq(_referrerOf(alice), bob);
    }

    function test_referral_binding_self_is_ignored() public {
        vm.prank(dave);
        naio.transfer(dave, 1e15);
        assertEq(_referrerOf(dave), address(0));
    }

    function test_referral_binding_to_contract_is_ignored() public {
        vm.prank(bob);
        naio.transfer(address(this), 1e15);
        assertEq(_referrerOf(address(this)), address(0));
    }
}
