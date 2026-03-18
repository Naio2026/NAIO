// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

import {NAIOToken} from "../src/NAIOToken.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {DepositWitnessRuleEngine} from "../src/DepositWitnessRuleEngine.sol";
import {NodeSeatPool} from "../src/pools/NodeSeatPool.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

/**
 * @notice Core flow regression (for local debugging)
 * Run: cd contracts && forge test -vvvv --match-test test_
 */
contract CoreFlowTest is Test {
    MockERC20 usdt;
    NAIOToken naio;
    NAIOController controller;
    DepositWitnessRuleEngine engine;
    NodeSeatPool nodePool;

    address taxReceiver = address(0xC0FFEE); // Transfer-tax receiver C (test-only)
    address ecoA = address(0xA0A0A0);
    address independentB = address(0xB0B0B0);
    address marketE = address(0xE0E0E0);
    address marketF = address(0xF0F0F0);

    address nodeOwner = vm.addr(101);
    address alice = vm.addr(201);
    address bob = vm.addr(202);
    address keeper = vm.addr(301);
    address bootstrap = vm.addr(401);
    uint256 w1Pk = 0xA11;
    uint256 w2Pk = 0xA12;
    uint256 w3Pk = 0xA13;

    function setUp() public {
        // Ensure timestamp is initialized so `poke()` can pass time checks
        vm.warp(1_700_000_000);

        usdt = new MockERC20("USDT", "USDT", 18);

        // Mint 1e8 NAIO to the deployer (this test contract), then transfer into Controller as initial pool
        naio = new NAIOToken("NAIO Token", "NAIO", 100_000_000e18, address(0xdead), taxReceiver);
        controller = new NAIOController(address(usdt), address(naio));

        // Important: transfer into the pool first, then setController (otherwise it may be treated as a sell and trigger callbacks)
        naio.transfer(address(controller), 100_000_000e18);

        nodePool = new NodeSeatPool(address(usdt), address(naio));
        nodePool.setController(address(controller));

        _init1000Seats();
        nodePool.seal();

        controller.setPools(address(nodePool), marketE, marketF);
        controller.setRewardReceivers(ecoA, independentB);
        controller.setKeeper(keeper);
        controller.setReferralRewardExcluded(bootstrap);
        address[] memory signers = new address[](3);
        signers[0] = vm.addr(w1Pk);
        signers[1] = vm.addr(w2Pk);
        signers[2] = vm.addr(w3Pk);
        engine = new DepositWitnessRuleEngine(address(controller), signers, 3, 0);
        controller.setDepositRuleEngine(address(engine));

        naio.setController(address(controller));

        // Fund test users with USDT and gas
        usdt.mint(alice, 1_000e18);
        usdt.mint(bob, 1_000e18);
        usdt.mint(bootstrap, 1_000e18);
        vm.deal(alice, 10 ether);
        vm.deal(bob, 10 ether);
        vm.deal(bootstrap, 10 ether);
        vm.deal(nodeOwner, 10 ether);
    }

    function _init1000Seats() internal {
        // seatId=1 belongs to nodeOwner; other seats are filled with placeholder addresses (no private keys)
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
        assertEq(nodePool.balanceOf(nodeOwner), 1);
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

    function test_deposit_poke_claim_static_and_node() public {
        // Alice deposit (keeper mode): transfer USDT to Controller first, then call depositFromTransferWitness to book it
        _depositByWitness(alice, 100e18, bytes32(uint256(1)));

        // poke
        vm.warp(block.timestamp + controller.EPOCH_SECONDS());
        controller.poke();

        // Alice claims static rewards (direct call; can also be triggered by transferring BNB)
        uint256 aliceNaioBefore = naio.balanceOf(alice);
        vm.prank(alice);
        controller.claimStatic();
        assertGt(naio.balanceOf(alice), aliceNaioBefore);

        // nodeOwner claims node dividends: transfer 0.0007 BNB to Controller (OP_CLAIM_NODE)
        uint256 nodeUsdtBefore = usdt.balanceOf(nodeOwner);
        uint256 nodeNaioBefore = naio.balanceOf(nodeOwner);
        vm.prank(nodeOwner);
        (bool ok,) = address(controller).call{value: 7e14}("");
        assertTrue(ok);
        assertGt(usdt.balanceOf(nodeOwner), nodeUsdtBefore);
        assertGt(naio.balanceOf(nodeOwner), nodeNaioBefore);
    }

    function test_referral_reward_allocated_on_static_claim() public {
        // Bob deposits first to become an active user (keeper mode)
        _depositByWitness(bob, 100e18, bytes32(uint256(2)));

        // Bob transfers 0.001 NAIO to Alice to bind referral relation
        vm.prank(address(controller));
        naio.transfer(bob, 1e15);
        vm.startPrank(bob);
        naio.transfer(alice, 1e15);
        vm.stopPrank();

        // Alice deposits; Controller reads inviter and writes referrer, and increments Bob's directCount
        _depositByWitness(alice, 100e18, bytes32(uint256(3)));
        // users(address) is a public mapping(struct) getter returning a tuple; you cannot access fields like .referrer directly.
        // UserInfo = (principalUsdt, power, referrer, directCount, lastClaimTs, firstDepositTs, rewardDebt, lastDepositEpoch, powerSnapEpoch, powerSnapAtDayStart, withdrawnUsdt, lockedUsdt)
        (, , address aliceReferrer, , , , , , , , ,) = controller.users(alice);
        assertEq(aliceReferrer, bob);

        (, , , uint16 bobDirectCount, , , , , , , ,) = controller.users(bob);
        assertEq(uint256(bobDirectCount), 1);

        // poke generates the referral pool
        vm.warp(block.timestamp + controller.EPOCH_SECONDS());
        controller.poke();

        // When Alice claims static rewards, part of the referral rewards are attributed to Bob
        vm.prank(alice);
        controller.claimStatic();

        // Bob claims dynamic/referral rewards (pendingNaio)
        uint256 bobNaioBefore = naio.balanceOf(bob);
        vm.prank(bob);
        controller.claimDynamic();
        assertGt(naio.balanceOf(bob), bobNaioBefore);
    }

    function test_excluded_referrer_gets_no_referral_reward() public {
        // bootstrap is the cold-start distribution address, using NAIO transfers to establish referrals
        vm.prank(address(controller));
        naio.transfer(bootstrap, 1e15);
        vm.prank(bootstrap);
        naio.transfer(alice, 1e15);

        // bootstrap and Alice both deposit to become active users
        _depositByWitness(bootstrap, 100e18, bytes32(uint256(4)));

        _depositByWitness(alice, 100e18, bytes32(uint256(5)));

        // Generate the referral pool and trigger distribution
        vm.warp(block.timestamp + controller.EPOCH_SECONDS());
        controller.poke();

        vm.prank(alice);
        controller.claimStatic();

        // Excluded address should receive no referral rewards: claimDynamic should revert NoReward
        vm.expectRevert(NAIOController.NoReward.selector);
        vm.prank(bootstrap);
        controller.claimDynamic();
    }
}

