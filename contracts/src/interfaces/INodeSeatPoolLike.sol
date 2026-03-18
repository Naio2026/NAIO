
pragma solidity ^0.8.24;

interface INodeSeatPoolLike {
    function notifyUsdt(uint256 amount) external;
    function notifyNaio(uint256 amount) external;
    function seatOf(address owner) external view returns (uint16);
    function claimMy() external;
    function claimFor(address user) external;
}

