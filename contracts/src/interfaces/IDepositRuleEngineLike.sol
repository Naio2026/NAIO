
pragma solidity ^0.8.24;

interface IDepositRuleEngineLike {
    function authorizeAndApplyDeposit(
        address user,
        uint256 usdtAmount,
        bytes32 txHash,
        uint256 principalBefore,
        uint256 witnessDeadline,
        bytes[] calldata signatures
    ) external returns (uint8 reason);

    function notifyUsdtInflow(uint256 amount) external;

    function notifyUsdtOutflow(uint256 amount) external;

    function notifyReservedUsdtIncrease(uint256 amount) external;

    function notifyReservedUsdtDecrease(uint256 amount) external;

    function rulePoolUsdt() external view returns (uint256);

    function replaceWitnessSigner(address oldSigner, address newSigner) external;

}

