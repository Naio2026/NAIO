
pragma solidity ^0.8.24;

import {INAIOControllerLike} from "./interfaces/INAIOControllerLike.sol";

interface IPair {
    function getReserves() external view returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast);
    function token0() external view returns (address);
    function token1() external view returns (address);
}

library EnumerableSet {
    struct Set {
        bytes32[] _values;
        mapping(bytes32 => uint256) _indexes;
    }

    function _add(Set storage set, bytes32 value) private returns (bool) {
        if (!_contains(set, value)) {
            set._values.push(value);
            set._indexes[value] = set._values.length;
            return true;
        } else {
            return false;
        }
    }

    function _remove(Set storage set, bytes32 value) private returns (bool) {
        uint256 valueIndex = set._indexes[value];
        if (valueIndex != 0) {
            uint256 toDeleteIndex = valueIndex - 1;
            uint256 lastIndex = set._values.length - 1;
            if (lastIndex != toDeleteIndex) {
                bytes32 lastvalue = set._values[lastIndex];
                set._values[toDeleteIndex] = lastvalue;
                set._indexes[lastvalue] = valueIndex;
            }
            set._values.pop();
            delete set._indexes[value];
            return true;
        } else {
            return false;
        }
    }

    function _contains(Set storage set, bytes32 value) private view returns (bool) {
        return set._indexes[value] != 0;
    }

    function _length(Set storage set) private view returns (uint256) {
        return set._values.length;
    }

    function _at(Set storage set, uint256 index) private view returns (bytes32) {
        return set._values[index];
    }

    function _values(Set storage set) private view returns (bytes32[] memory) {
        return set._values;
    }

    struct AddressSet {
        Set _inner;
    }

    function add(AddressSet storage set, address value) internal returns (bool) {
        return _add(set._inner, bytes32(uint256(uint160(value))));
    }

    function remove(AddressSet storage set, address value) internal returns (bool) {
        return _remove(set._inner, bytes32(uint256(uint160(value))));
    }

    function contains(AddressSet storage set, address value) internal view returns (bool) {
        return _contains(set._inner, bytes32(uint256(uint160(value))));
    }

    function length(AddressSet storage set) internal view returns (uint256) {
        return _length(set._inner);
    }

    function at(AddressSet storage set, uint256 index) internal view returns (address) {
        return address(uint160(uint256(_at(set._inner, index))));
    }

    function values(AddressSet storage set) internal view returns (address[] memory) {
        bytes32[] memory store = _values(set._inner);
        address[] memory result;
        assembly {
            result := store
        }
        return result;
    }
}

contract NAIOToken {
    using EnumerableSet for EnumerableSet.AddressSet;
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;
    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    address public owner;
    address public immutable burnAddress;
    address public controller;

    address public pair;

    address public ops;
    address public poolReceiver;

    uint16 public sellBurnBps;
    uint16 public sellPoolBps;
    uint16 public sellOpsBps;

    bool public buyDisabled;

    address public immutable transferTaxReceiver;
    uint16 public constant TRANSFER_TAX_BPS = 500;

    mapping(address => address) public inviter;
    mapping(address => EnumerableSet.AddressSet) private inviterChildList;
    uint256 public constant MIN_BIND_AMOUNT = 1e15;
    uint8 public constant MAX_DEPTH = 50;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event PairSet(address indexed pair);
    event FeeReceiversSet(address indexed ops, address indexed poolReceiver);
    event SellFeeBpsSet(uint16 burnBps, uint16 poolBps, uint16 opsBps);
    event BuyDisabledSet(bool disabled);
    event ControllerSet(address indexed controller);
    event Minted(address indexed to, uint256 amount);
    event InviterBound(address indexed user, address indexed inviter);

    modifier onlyOwner() {
        require(msg.sender == owner, "NOT_OWNER");
        _;
    }

    constructor(
        string memory _name,
        string memory _symbol,
        uint256 _initialSupply,
        address _burnAddress,
        address _transferTaxReceiver
    ) {
        require(_burnAddress != address(0), "ZERO_BURN");
        owner = msg.sender;
        name = _name;
        symbol = _symbol;
        burnAddress = _burnAddress;
        transferTaxReceiver = _transferTaxReceiver;

        _mint(msg.sender, _initialSupply);
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

    function setPair(address _pair) external onlyOwner {
        require(pair == address(0), "PAIR_ALREADY_SET");
        require(_pair != address(0), "ZERO_PAIR");
        pair = _pair;
        emit PairSet(_pair);
    }

    function setFeeReceivers(address _ops, address _poolReceiver) external onlyOwner {
        require(_ops != address(0) && _poolReceiver != address(0), "ZERO_ADDR");
        ops = _ops;
        poolReceiver = _poolReceiver;
        emit FeeReceiversSet(_ops, _poolReceiver);
    }

    function setSellFeeBps(uint16 burnBps, uint16 poolBps, uint16 opsBps) external onlyOwner {
        uint256 total = uint256(burnBps) + uint256(poolBps) + uint256(opsBps);
        require(total <= 2000, "FEE_TOO_HIGH");
        sellBurnBps = burnBps;
        sellPoolBps = poolBps;
        sellOpsBps = opsBps;
        emit SellFeeBpsSet(burnBps, poolBps, opsBps);
    }

    function setBuyDisabled(bool disabled) external onlyOwner {
        buyDisabled = disabled;
        emit BuyDisabledSet(disabled);
    }

    function setController(address _controller) external onlyOwner {
        require(_controller != address(0), "ZERO_CONTROLLER");
        controller = _controller;
        emit ControllerSet(_controller);
    }

    function mint(address to, uint256 amount) external {
        require(msg.sender == controller, "ONLY_CONTROLLER");
        require(to != address(0), "MINT_TO_ZERO");
        _mint(to, amount);
        emit Minted(to, amount);
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transfer(address to, uint256 value) external returns (bool) {
        _transfer(msg.sender, to, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 current = allowance[from][msg.sender];
        require(current >= value, "ALLOWANCE");
        unchecked {
            allowance[from][msg.sender] = current - value;
        }
        _transfer(from, to, value);
        return true;
    }

    function burn(uint256 value) external returns (bool) {
        _transferToBurn(msg.sender, value);
        return true;
    }

    function burnFrom(address from, uint256 value) external {
        require(msg.sender == controller, "ONLY_CONTROLLER");
        _transferToBurn(from, value);
    }

    function _transferToBurn(address from, uint256 value) internal {
        require(burnAddress != address(0), "ZERO_BURN");
        uint256 bal = balanceOf[from];
        require(bal >= value, "BALANCE");
        unchecked {
            balanceOf[from] = bal - value;
        }
        balanceOf[burnAddress] += value;
        emit Transfer(from, burnAddress, value);
    }

    function isCanBindInviter(address user, address inviterAddr) public view returns (bool) {
        if (inviter[user] != address(0) || user == inviterAddr) {
            return false;
        }
        address current = inviterAddr;
        uint8 depth = 0;
        while (current != address(0) && depth < MAX_DEPTH) {
            if (current == user) {
                return false;
            }
            current = inviter[current];
            depth++;
        }
        return true;
    }

    function getInviterChildList(address account) public view returns (address[] memory) {
        return inviterChildList[account].values();
    }

    function getInviterChildCount(address account) public view returns (uint256) {
        return inviterChildList[account].length();
    }

    function _mint(address to, uint256 value) internal {
        require(to != address(0), "MINT_TO_ZERO");
        totalSupply += value;
        balanceOf[to] += value;
        emit Transfer(address(0), to, value);
    }

    function _burn(address from, uint256 value) internal {
        require(from != address(0), "BURN_FROM_ZERO");
        uint256 bal = balanceOf[from];
        require(bal >= value, "BALANCE");
        unchecked {
            balanceOf[from] = bal - value;
        }
        totalSupply -= value;
        emit Transfer(from, address(0), value);
    }

    function _estimateUsdtFromPair(uint256 tokenAmount) internal view returns (uint256 usdtAmount) {
        if (pair == address(0) || controller == address(0)) {
            return 0;
        }

        try IPair(pair).getReserves() returns (uint112 reserve0, uint112 reserve1, uint32) {
            address token0 = IPair(pair).token0();

            uint256 reserveNAIO;
            uint256 reserveUSDT;

            if (token0 == address(this)) {
                reserveNAIO = reserve0;
                reserveUSDT = reserve1;
            } else {
                reserveNAIO = reserve1;
                reserveUSDT = reserve0;
            }

            if (reserveNAIO == 0 || reserveUSDT == 0) {
                return 0;
            }

            usdtAmount = (tokenAmount * reserveUSDT) / (reserveNAIO + tokenAmount);
        } catch {
            return 0;
        }
    }

    function _tryBindReferralViaController(address user, address inviterAddr, uint256 value) internal {
        if (controller == address(0) || value < MIN_BIND_AMOUNT) return;
        if (user == address(0) || inviterAddr == address(0) || user == inviterAddr) return;
        if (user == controller || user.code.length > 0 || inviterAddr.code.length > 0) return;

        bool bound = false;
        try INAIOControllerLike(controller).bindReferral(user, inviterAddr) returns (bool ok) {
            bound = ok;
        } catch {}

        if (bound && inviter[user] == address(0) && isCanBindInviter(user, inviterAddr)) {
            inviter[user] = inviterAddr;
            inviterChildList[inviterAddr].add(user);
            emit InviterBound(user, inviterAddr);
        }
    }

    function _transfer(address from, address to, uint256 value) internal {
        require(to != address(0), "TO_ZERO");
        uint256 bal = balanceOf[from];
        require(bal >= value, "BALANCE");

        if (buyDisabled && from == pair && pair != address(0)) {
            revert("BUY_DISABLED");
        }

        if (controller != address(0) && to == controller && value > 0) {
            unchecked {
                balanceOf[from] = bal - value;
            }
            balanceOf[to] += value;
            emit Transfer(from, to, value);

            try INAIOControllerLike(controller).onNAIOReceived(from, value) returns (bool success) {
                require(success, "SELL_FAILED");
            } catch {
                revert("SELL_FAILED");
            }
            return;
        }

        if (TRANSFER_TAX_BPS > 0 && transferTaxReceiver != address(0)) {
            bool fromIsContract = from.code.length > 0;
            bool toIsContract = to.code.length > 0;
            if (!fromIsContract && !toIsContract) {
                uint256 taxAmount = (value * uint256(TRANSFER_TAX_BPS)) / 10000;
                uint256 sendAmount = value - taxAmount;

                _tryBindReferralViaController(to, from, value);

                unchecked {
                    balanceOf[from] = bal - value;
                }

                if (taxAmount > 0) {
                    balanceOf[transferTaxReceiver] += taxAmount;
                    emit Transfer(from, transferTaxReceiver, taxAmount);
                }

                balanceOf[to] += sendAmount;
                emit Transfer(from, to, sendAmount);
                return;
            }
        }

        _tryBindReferralViaController(to, from, value);

        unchecked {
            balanceOf[from] = bal - value;
        }
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }
}

