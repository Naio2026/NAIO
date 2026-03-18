// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

import {NAIOToken} from "../src/NAIOToken.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {DepositWitnessRuleEngine} from "../src/DepositWitnessRuleEngine.sol";
import {NodeSeatPool} from "../src/pools/NodeSeatPool.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract SellBusinessTaxTest is Test {
    MockERC20 usdt;
    NAIOToken naio;
    NAIOController controller;
    DepositWitnessRuleEngine engine;
    NodeSeatPool nodePool;

    address taxReceiver = address(0xC0FFEE);
    address ecoA = address(0xA0A0A0);
    address independentB = address(0xB0B0B0);
    address marketE = address(0xE0E0E0);
    address marketF = address(0xF0F0F0); // opsPool (business-tax USDT receiver)

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
        engine = new DepositWitnessRuleEngine(address(controller), signers, 3, 10_000_000e18);
        controller.setDepositRuleEngine(address(engine));

        naio.setController(address(controller));

        usdt.mint(alice, 1_000e18);
        vm.deal(alice, 10 ether);

        // Alice deposits 100U (sets principalUsdt=100 for business-tax tiering)
        vm.prank(alice);
        usdt.transfer(address(controller), 100e18);
        uint256 deadline = block.timestamp + 600;
        bytes32 txHash = bytes32(uint256(9001));
        bytes32 digest = engine.witnessDigest(alice, 100e18, txHash, deadline);
        bytes[] memory sigs = new bytes[](3);
        sigs[0] = _sig(w1Pk, digest);
        sigs[1] = _sig(w2Pk, digest);
        sigs[2] = _sig(w3Pk, digest);
        vm.prank(keeper);
        controller.depositFromTransferWitness(alice, 100e18, txHash, deadline, sigs);

        // Increase pool USDT to raise price; helps test business-tax tiers when total supply is large
        usdt.mint(address(controller), 10_000_000e18);
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

    function _sig(uint256 pk, bytes32 digest) internal returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, v);
    }

    function test_business_tax_10_percent_when_multiple_lt_20x() public {
        uint256 price = controller.getPrice();
        assertGt(price, 0, "price must be > 0");

        // New rule: 0–20x multiple charges 10% tax. Use 0.5x (50U) to verify it still charges 10%.
        uint256 usdtTargetGross = 50e18;
        uint256 tokenAmount = (usdtTargetGross * 1e18) / price;
        require(tokenAmount > 0, "tokenAmount=0");

        // Give Alice enough NAIO to sell (sent from controller; transfer tax does not apply)
        vm.prank(address(controller));
        naio.transfer(alice, tokenAmount);

        uint256 usdtGross = (tokenAmount * price) / 1e18;
        assertLt(usdtGross, 2000e18, "should stay under 20x tier");

        uint256 opsBefore = usdt.balanceOf(marketF);
        uint256 aliceUsdtBefore = usdt.balanceOf(alice);

        // Sell: transfer NAIO to controller triggers onNAIOReceived
        vm.prank(alice);
        naio.transfer(address(controller), tokenAmount);

        uint256 poolTax = (usdtGross * 700) / 10000; // 7%
        uint256 opsTax = (usdtGross * 300) / 10000; // 3%
        uint256 expectedReceived = usdtGross - poolTax - opsTax;

        assertEq(usdt.balanceOf(marketF) - opsBefore, opsTax, "opsTax should be paid to opsPool(F)");
        assertEq(usdt.balanceOf(alice) - aliceUsdtBefore, expectedReceived, "seller USDT received mismatch");
        assertEq(controller.totalSoldUsdt(alice), usdtGross, "totalSoldUsdt should record gross");
    }

    function test_business_tax_20_percent_when_multiple_ge_20x() public {
        uint256 price = controller.getPrice();
        assertGt(price, 0, "price must be > 0");

        // principal=100U; target 25x=2500U to ensure >=20x tier is hit
        uint256 usdtTargetGross = 2500e18;
        uint256 tokenAmount = (usdtTargetGross * 1e18) / price;
        require(tokenAmount > 0, "tokenAmount=0");

        vm.prank(address(controller));
        naio.transfer(alice, tokenAmount);

        uint256 usdtGross = (tokenAmount * price) / 1e18;
        assertGe(usdtGross, 2000e18, "must reach 20x principal to trigger 20% tier");

        uint256 opsBefore = usdt.balanceOf(marketF);
        uint256 aliceUsdtBefore = usdt.balanceOf(alice);

        vm.prank(alice);
        naio.transfer(address(controller), tokenAmount);

        uint256 poolTax = (usdtGross * 1400) / 10000; // 14%
        uint256 opsTax = (usdtGross * 600) / 10000; // 6%
        uint256 expectedReceived = usdtGross - poolTax - opsTax;

        assertEq(usdt.balanceOf(marketF) - opsBefore, opsTax, "opsTax should be paid to opsPool(F)");
        assertEq(usdt.balanceOf(alice) - aliceUsdtBefore, expectedReceived, "seller USDT received mismatch");
        assertEq(controller.totalSoldUsdt(alice), usdtGross, "totalSoldUsdt should record gross");
    }
}

