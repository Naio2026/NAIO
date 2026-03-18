// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

import {NAIOToken} from "../src/NAIOToken.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {DepositWitnessRuleEngine} from "../src/DepositWitnessRuleEngine.sol";
import {NodeSeatPool} from "../src/pools/NodeSeatPool.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract BlackholeAccountingTest is Test {
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

    function _sig(uint256 pk, bytes32 digest) internal returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, v);
    }

    function test_burn_increases_on_poke_total_supply_unchanged() public {
        _depositByTransfer(alice, 100e18, bytes32(uint256(5001)));

        vm.warp(block.timestamp + controller.EPOCH_SECONDS());

        address burnAddr = naio.burnAddress();
        uint256 burnBefore = naio.balanceOf(burnAddr);
        uint256 supplyBefore = naio.totalSupply();

        controller.poke();

        uint256 burnAfter = naio.balanceOf(burnAddr);
        uint256 supplyAfter = naio.totalSupply();

        assertGt(burnAfter, burnBefore, "burn address balance should increase");
        assertEq(supplyAfter, supplyBefore, "totalSupply should not change");
    }

    function test_withdraw_does_not_burn_naio() public {
        _depositByTransfer(alice, 1000e18, bytes32(uint256(5002)));

        vm.warp(block.timestamp + controller.EPOCH_SECONDS());
        controller.poke();

        // Withdraw consumes NAIO from the pool via the burn quota (sent to the burn/blackhole); the user does not need to provide NAIO.
        address burnAddr = naio.burnAddress();
        uint256 burnBefore = naio.balanceOf(burnAddr);
        uint256 supplyBefore = naio.totalSupply();
        uint256 aliceNaioBefore = naio.balanceOf(alice);

        vm.prank(alice);
        controller.withdrawLP();

        uint256 burnAfter = naio.balanceOf(burnAddr);
        uint256 supplyAfter = naio.totalSupply();
        uint256 aliceNaioAfter = naio.balanceOf(alice);

        // Withdraw transfers pool NAIO into the burn address (total supply unchanged) and does not change the user's NAIO balance.
        assertGt(burnAfter, burnBefore, "burn address balance should increase on withdraw");
        assertEq(supplyAfter, supplyBefore, "totalSupply should not change");
        assertEq(aliceNaioAfter, aliceNaioBefore, "user NAIO balance should not change");
    }

    function test_burn_increases_on_sell_total_supply_unchanged() public {
        _depositByTransfer(alice, 1000e18, bytes32(uint256(5003)));

        // Give Alice some NAIO to sell
        vm.prank(address(controller));
        naio.transfer(alice, 1_000e18);

        address burnAddr = naio.burnAddress();
        uint256 burnBefore = naio.balanceOf(burnAddr);
        uint256 supplyBefore = naio.totalSupply();

        vm.prank(alice);
        naio.transfer(address(controller), 1_000e18);

        uint256 burnAfter = naio.balanceOf(burnAddr);
        uint256 supplyAfter = naio.totalSupply();

        assertGt(burnAfter, burnBefore, "burn address balance should increase");
        assertEq(supplyAfter, supplyBefore, "totalSupply should not change");
    }
}
