
pragma solidity ^0.8.24;

contract MockFaucetERC20 {
    string public name;
    string public symbol;
    uint8 public immutable decimals;
    uint256 public totalSupply;

    address public owner;
    uint256 public faucetAmount;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    event FaucetMinted(address indexed user, uint256 bnbPaid, uint256 tokenMinted);
    event FaucetAmountUpdated(uint256 newAmount);
    event OwnerUpdated(address indexed newOwner);
    event WithdrawBNB(address indexed to, uint256 amount);

    modifier onlyOwner() {
        require(msg.sender == owner, "NOT_OWNER");
        _;
    }

    constructor(string memory _name, string memory _symbol, uint8 _decimals) {
        name = _name;
        symbol = _symbol;
        decimals = _decimals;
        owner = msg.sender;

        faucetAmount = 10_000 * (10 ** uint256(_decimals));
        emit OwnerUpdated(msg.sender);
        emit FaucetAmountUpdated(faucetAmount);
    }

    receive() external payable {
        require(msg.value > 0, "ZERO_BNB");
        _mint(msg.sender, faucetAmount);
        emit FaucetMinted(msg.sender, msg.value, faucetAmount);
    }

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }

    function setFaucetAmount(uint256 newAmount) external onlyOwner {
        faucetAmount = newAmount;
        emit FaucetAmountUpdated(newAmount);
    }

    function setOwner(address newOwner) external onlyOwner {
        require(newOwner != address(0), "ZERO_OWNER");
        owner = newOwner;
        emit OwnerUpdated(newOwner);
    }

    function withdrawBNB(address payable to, uint256 amount) external onlyOwner {
        require(to != address(0), "TO_ZERO");
        require(address(this).balance >= amount, "INSUFFICIENT_BNB");
        to.transfer(amount);
        emit WithdrawBNB(to, amount);
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

    function _mint(address to, uint256 amount) internal {
        require(to != address(0), "MINT_TO_ZERO");
        require(amount > 0, "MINT_ZERO");
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function _transfer(address from, address to, uint256 value) internal {
        require(to != address(0), "TO_ZERO");
        uint256 bal = balanceOf[from];
        require(bal >= value, "BALANCE");
        unchecked {
            balanceOf[from] = bal - value;
        }
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }
}

