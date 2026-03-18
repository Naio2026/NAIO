// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

import {NAIOToken} from "../src/NAIOToken.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {DepositWitnessRuleEngine} from "../src/DepositWitnessRuleEngine.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract TransferTaxTest is Test {
    MockERC20 usdt;
    NAIOToken naio;
    NAIOController controller;
    DepositWitnessRuleEngine engine;

    address taxReceiver = address(0xC0FFEE);
    address alice = vm.addr(201);
    address bob = vm.addr(202);
    uint256 w1Pk = 0xA11;

    function setUp() public {
        vm.warp(1_700_000_000);

        usdt = new MockERC20("USDT", "USDT", 18);
        naio = new NAIOToken("NAIO Token", "NAIO", 100_000_000e18, address(0xdead), taxReceiver);
        controller = new NAIOController(address(usdt), address(naio));
        controller.setPools(address(0x1001), address(0x1002), address(0x1003));

        // Transfer NAIO into the controller first, then setController (avoid being treated as a sell)
        naio.transfer(address(controller), 100_000_000e18);
        naio.setController(address(controller));

        // Provide the controller with some USDT so "transfer NAIO to controller triggers sell" won't revert due to PRICE_ZERO / insufficient pool.
        usdt.mint(address(controller), 1_000_000e18);
        address[] memory signers = new address[](1);
        signers[0] = vm.addr(w1Pk);
        engine = new DepositWitnessRuleEngine(address(controller), signers, 1, 1_000_000e18);
        controller.setDepositRuleEngine(address(engine));

        vm.deal(alice, 1 ether);
        vm.deal(bob, 1 ether);

        // Distribute some NAIO to Bob (from controller balance; sender is a contract, so EOA->EOA transfer tax does not apply)
        vm.prank(address(controller));
        naio.transfer(bob, 1_000e18);
    }

    function test_eoa_to_eoa_transfer_tax_5_percent() public {
        uint256 bobBefore = naio.balanceOf(bob);
        uint256 aliceBefore = naio.balanceOf(alice);
        uint256 taxBefore = naio.balanceOf(taxReceiver);

        vm.prank(bob);
        naio.transfer(alice, 100e18);

        assertEq(naio.balanceOf(bob), bobBefore - 100e18);
        assertEq(naio.balanceOf(alice), aliceBefore + 95e18);
        assertEq(naio.balanceOf(taxReceiver), taxBefore + 5e18);
    }

    function test_transfer_to_controller_is_sell_and_no_transfer_tax() public {
        uint256 taxBefore = naio.balanceOf(taxReceiver);
        uint256 bobUsdtBefore = usdt.balanceOf(bob);

        // Sell is triggered by transferring NAIO to the controller: NAIOToken returns early on this path, bypassing the transfer-tax branch.
        vm.prank(bob);
        naio.transfer(address(controller), 1e18);

        assertEq(naio.balanceOf(taxReceiver), taxBefore, "sell path should not charge transfer tax");
        assertGt(usdt.balanceOf(bob), bobUsdtBefore, "seller should receive USDT (even tiny)");
    }
}

