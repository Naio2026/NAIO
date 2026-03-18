
pragma solidity ^0.8.24;

import {IMinimalERC20} from "../interfaces/IMinimalERC20.sol";

contract NodeSeatPool {
    address public owner;
    address public immutable usdt;
    address public immutable naio;
    address public controller;

    uint16 public constant MAX_SEATS = 1000;
    uint256 public constant ACC = 1e18;

    string public name = "NAIO Node Seat";
    string public symbol = "NAIOSEAT";
    uint8 public constant decimals = 0;
    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    bool public isSealed;
    uint16 public seatCount;

    uint256 public accUsdtPerSeat;
    uint256 public accNaioPerSeat;

    mapping(address => uint256) public seatDebtUsdt;
    mapping(address => uint256) public seatDebtNaio;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event ControllerSet(address indexed controller);
    event Sealed();
    event SeatInitialized(uint16 indexed seatId, address indexed owner);
    event SeatTransferred(uint16 indexed seatId, address indexed from, address indexed to);
    event SeatForceTransferred(address indexed operator, address indexed from, address indexed to);
    event DividendNotifiedUsdt(uint256 amount, uint256 newAccUsdtPerSeat);
    event DividendNotifiedNaio(uint256 amount, uint256 newAccNaioPerSeat);
    event Claimed(uint16 indexed seatId, address indexed owner, uint256 usdtAmount, uint256 naioAmount);

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    modifier onlyOwner() {
        require(msg.sender == owner, "NOT_OWNER");
        _;
    }

    modifier onlyController() {
        require(msg.sender == controller, "NOT_CONTROLLER");
        _;
    }

    modifier onlySealed() {
        require(isSealed, "NOT_SEALED");
        _;
    }

    constructor(address _usdt, address _naio) {
        require(_usdt != address(0) && _naio != address(0), "ZERO_ADDR");
        owner = msg.sender;
        usdt = _usdt;
        naio = _naio;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "ZERO_OWNER");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    function renounceOwnership() external onlyOwner {
        emit OwnershipTransferred(owner, address(0));
        owner = address(0);
    }

    function setController(address _controller) external onlyOwner {
        require(_controller != address(0), "ZERO_CONTROLLER");
        require(controller == address(0), "CONTROLLER_ALREADY_SET");
        controller = _controller;
        emit ControllerSet(_controller);
    }

    function setInitialOwners(uint16 startSeatId, address[] calldata owners_) external onlyOwner {
        require(!isSealed, "SEALED");
        require(owners_.length > 0, "EMPTY");
        require(startSeatId >= 1, "BAD_START");
        require(startSeatId == uint16(seatCount + 1), "BAD_START");

        uint256 endSeatId = uint256(startSeatId) + owners_.length - 1;
        require(endSeatId <= MAX_SEATS, "OUT_OF_RANGE");

        for (uint256 i = 0; i < owners_.length; i++) {
            uint16 seatId = uint16(uint256(startSeatId) + i);
            address o = owners_[i];
            require(o != address(0), "ZERO_OWNER_ADDR");
            require(balanceOf[o] == 0, "OWNER_ALREADY_HAS_SEAT");

            _mintSeat(o);
            seatDebtUsdt[o] = accUsdtPerSeat;
            seatDebtNaio[o] = accNaioPerSeat;

            seatCount += 1;
            emit SeatInitialized(seatId, o);
        }
    }

    function seal() external onlyOwner {
        require(!isSealed, "SEALED");
        require(seatCount == MAX_SEATS, "NOT_FULL");
        isSealed = true;
        emit Sealed();
    }

    function seatOf(address owner_) public view returns (uint16) {
        return balanceOf[owner_] > 0 ? 1 : 0;
    }

    function pendingUsdt(address owner_) public view returns (uint256) {
        if (balanceOf[owner_] == 0) return 0;
        uint256 acc = accUsdtPerSeat;
        uint256 debt = seatDebtUsdt[owner_];
        if (acc <= debt) return 0;
        return ((acc - debt) * balanceOf[owner_]) / ACC;
    }

    function pendingNaio(address owner_) public view returns (uint256) {
        if (balanceOf[owner_] == 0) return 0;
        uint256 acc = accNaioPerSeat;
        uint256 debt = seatDebtNaio[owner_];
        if (acc <= debt) return 0;
        return ((acc - debt) * balanceOf[owner_]) / ACC;
    }

    function claimSeat(uint16 seatId) external onlySealed {
        seatId;
        require(balanceOf[msg.sender] > 0, "NO_SEAT");
        _claimTo(msg.sender);
    }

    function claimMy() external onlySealed {
        require(balanceOf[msg.sender] > 0, "NO_SEAT");
        _claimTo(msg.sender);
    }

    function claimFor(address user) external onlySealed {
        require(user != address(0), "ZERO_USER");
        require(balanceOf[user] > 0, "NO_SEAT");
        _claimTo(user);
    }

    function transferSeat(uint16 seatId, address to) external onlySealed {
        seatId;
        _transferSeat(msg.sender, to, 1);
    }

    function forceTransferSeat(address from, address to) external onlyOwner onlySealed {
        _forceTransferSeat(from, to, 1);
        emit SeatForceTransferred(msg.sender, from, to);
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transfer(address to, uint256 value) external returns (bool) {
        _transferSeat(msg.sender, to, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 current = allowance[from][msg.sender];
        require(current >= value, "ALLOWANCE");
        unchecked {
            allowance[from][msg.sender] = current - value;
        }
        _transferSeat(from, to, value);
        return true;
    }

    function notifyUsdt(uint256 amount) external onlySealed onlyController {
        require(amount > 0, "AMOUNT_0");
        require(IMinimalERC20(usdt).balanceOf(address(this)) >= amount, "BALANCE_LT_AMOUNT");
        accUsdtPerSeat += (amount * ACC) / MAX_SEATS;
        emit DividendNotifiedUsdt(amount, accUsdtPerSeat);
    }

    function notifyNaio(uint256 amount) external onlySealed onlyController {
        require(amount > 0, "AMOUNT_0");
        require(IMinimalERC20(naio).balanceOf(address(this)) >= amount, "BALANCE_LT_AMOUNT");
        accNaioPerSeat += (amount * ACC) / MAX_SEATS;
        emit DividendNotifiedNaio(amount, accNaioPerSeat);
    }

    function _claimTo(address to) internal {
        uint256 usdtAmt = pendingUsdt(to);
        uint256 naioAmt = pendingNaio(to);

        seatDebtUsdt[to] = accUsdtPerSeat;
        seatDebtNaio[to] = accNaioPerSeat;

        if (usdtAmt > 0) {
            require(IMinimalERC20(usdt).transfer(to, usdtAmt), "USDT_TF_FAIL");
        }
        if (naioAmt > 0) {
            require(IMinimalERC20(naio).transfer(to, naioAmt), "NAIO_TF_FAIL");
        }

        emit Claimed(1, to, usdtAmt, naioAmt);
    }

    function _mintSeat(address to) internal {
        require(to != address(0), "ZERO_TO");
        require(balanceOf[to] == 0, "TO_ALREADY_HAS_SEAT");
        balanceOf[to] = 1;
        totalSupply += 1;
        emit Transfer(address(0), to, 1);
    }

    function _transferSeat(address from, address to, uint256 value) internal {
        require(isSealed, "NOT_SEALED");
        require(to != address(0), "ZERO_TO");
        require(to != from, "SAME_TO");
        require(value == 1, "INVALID_AMOUNT");
        require(balanceOf[from] >= 1, "NO_SEAT");
        require(balanceOf[to] == 0, "TO_ALREADY_HAS_SEAT");

        _claimTo(from);

        balanceOf[from] = 0;
        balanceOf[to] = 1;

        seatDebtUsdt[to] = accUsdtPerSeat;
        seatDebtNaio[to] = accNaioPerSeat;
        seatDebtUsdt[from] = accUsdtPerSeat;
        seatDebtNaio[from] = accNaioPerSeat;

        emit Transfer(from, to, 1);
        emit SeatTransferred(1, from, to);
    }

    function _forceTransferSeat(address from, address to, uint256 value) internal {
        require(isSealed, "NOT_SEALED");
        require(to != address(0), "ZERO_TO");
        require(to != from, "SAME_TO");
        require(value == 1, "INVALID_AMOUNT");
        require(balanceOf[from] >= 1, "NO_SEAT");
        require(balanceOf[to] == 0, "TO_ALREADY_HAS_SEAT");

        _claimTo(from);

        balanceOf[from] = 0;
        balanceOf[to] = 1;

        seatDebtUsdt[to] = accUsdtPerSeat;
        seatDebtNaio[to] = accNaioPerSeat;
        seatDebtUsdt[from] = accUsdtPerSeat;
        seatDebtNaio[from] = accNaioPerSeat;

        emit Transfer(from, to, 1);
        emit SeatTransferred(1, from, to);
    }
}

