
pragma solidity ^0.8.24;

import {IMinimalERC20} from "./interfaces/IMinimalERC20.sol";

interface INAIOControllerSeeder {
    function seedPoolUsdtFromSeeder(uint256 usdtAmount) external;
    function usdt() external view returns (address);
}

contract InitialPoolSeeder {
    uint256 public constant OP_SEED_USDT = 10e14;

    address public owner;
    address public immutable controller;
    address public immutable usdt;
    uint256 public immutable targetUsdt;

    uint256 public totalSeededUsdt;
    uint256 public totalReceivedUsdt;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event SeededByOp(
        address indexed user,
        uint256 receivedAmount,
        uint256 seededAmount,
        uint256 refundAmount,
        uint256 totalSeededUsdt,
        uint256 totalReceivedUsdt
    );

    modifier onlyOwner() {
        require(msg.sender == owner, "NOT_OWNER");
        _;
    }

    constructor(address _controller, uint256 _targetUsdt) {
        require(_controller != address(0), "ZERO_CONTROLLER");
        require(_targetUsdt > 0, "ZERO_TARGET");
        owner = msg.sender;
        controller = _controller;
        usdt = INAIOControllerSeeder(_controller).usdt();
        targetUsdt = _targetUsdt;

        require(IMinimalERC20(usdt).approve(controller, type(uint256).max), "APPROVE_FAIL");
        emit OwnershipTransferred(address(0), msg.sender);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "ZERO_OWNER");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    receive() external payable {
        address user = msg.sender;
        uint256 op = msg.value;

        if (op == OP_SEED_USDT) {
            _seedFromBalance(user);
            (bool ok,) = payable(user).call{value: msg.value}("");
            require(ok, "BNB_REFUND_FAIL");
            return;
        }

        revert("INVALID_OP");
    }

    function seedByBalance() external returns (bool) {
        _seedFromBalance(msg.sender);
        return true;
    }

    function seedByPull() external returns (bool) {
        _seedFromBalance(msg.sender);
        return true;
    }

    function _seedFromBalance(address refundTo) internal {
        require(refundTo != address(0), "ZERO_USER");
        uint256 remaining = targetUsdt > totalSeededUsdt ? (targetUsdt - totalSeededUsdt) : 0;

        uint256 bal = IMinimalERC20(usdt).balanceOf(address(this));
        require(bal > 0, "NO_USDT_IN_SEEDER");
        totalReceivedUsdt += bal;

        if (remaining == 0) {
            require(IMinimalERC20(usdt).transfer(refundTo, bal), "REFUND_FAIL");
            emit SeededByOp(refundTo, bal, 0, bal, totalSeededUsdt, totalReceivedUsdt);
            return;
        }

        uint256 seedAmt = bal <= remaining ? bal : remaining;
        uint256 refundAmt = bal - seedAmt;

        INAIOControllerSeeder(controller).seedPoolUsdtFromSeeder(seedAmt);
        totalSeededUsdt += seedAmt;

        if (refundAmt > 0) {
            require(IMinimalERC20(usdt).transfer(refundTo, refundAmt), "REFUND_FAIL");
        }

        emit SeededByOp(refundTo, bal, seedAmt, refundAmt, totalSeededUsdt, totalReceivedUsdt);
    }
}

