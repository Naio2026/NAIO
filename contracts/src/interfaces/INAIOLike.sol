
pragma solidity ^0.8.24;

interface INAIOLike {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 value) external returns (bool);
    function transferFrom(address from, address to, uint256 value) external returns (bool);

    function inviter(address account) external view returns (address);
    function totalSupply() external view returns (uint256);
    function burnAddress() external view returns (address);

    function burn(uint256 value) external returns (bool);
    function burnFrom(address from, uint256 value) external;
}

