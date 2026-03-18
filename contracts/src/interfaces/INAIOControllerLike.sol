
pragma solidity ^0.8.24;

interface INAIOControllerLike {
    function recordSaleAndGetBusinessTax(
        address seller,
        uint256 soldUsdtAmount
    ) external returns (uint16 businessTaxBps, uint16 poolTaxBps, uint16 opsTaxBps);

    function usdt() external view returns (address);

    function onNAIOReceived(address from, uint256 amount) external returns (bool);

    function bindReferral(address user, address inviter) external returns (bool);
}

