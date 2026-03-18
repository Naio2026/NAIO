
pragma solidity ^0.8.24;

contract DepositWitnessRuleEngine {
    uint256 internal constant SECP256K1N_HALF =
        0x7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0;
    address public immutable controller;

    mapping(address => bool) public isWitnessSigner;
    uint16 public witnessSignerCount;
    uint16 public witnessThreshold;

    uint256 public rulePoolUsdt;

    mapping(bytes32 => bool) public usedTransferTxHash;

    bytes32 public constant WITNESS_TYPEHASH = keccak256(
        "NAIOWitness(uint256 chainId,address controller,address user,uint256 usdtAmount,bytes32 txHash,uint256 witnessDeadline)"
    );

    uint256 public constant MIN_DEPOSIT = 100e18;
    uint256 public constant MAX_DEPOSIT_BEFORE_1M = 1000e18;
    uint256 public constant OPEN_CAP_POOL_THRESHOLD = 1_000_000e18;

    event WitnessSignerSet(address indexed signer, bool enabled);
    event WitnessThresholdSet(uint16 threshold);
    event RulePoolSynced(uint256 value);
    event DepositAuthorized(
        bytes32 indexed txHash,
        address indexed user,
        uint256 usdtAmount,
        uint256 poolBefore,
        uint256 principalBefore,
        uint8 reason
    );

    modifier onlyController() {
        require(msg.sender == controller, "NOT_CONTROLLER");
        _;
    }

    constructor(
        address _controller,
        address[] memory signers,
        uint16 threshold,
        uint256 initialRulePoolUsdt
    ) {
        require(_controller != address(0), "ZERO_CONTROLLER");
        controller = _controller;

        for (uint256 i = 0; i < signers.length; i++) {
            address s = signers[i];
            require(s != address(0), "ZERO_SIGNER");
            require(!isWitnessSigner[s], "DUP_SIGNER");
            isWitnessSigner[s] = true;
            witnessSignerCount += 1;
            emit WitnessSignerSet(s, true);
        }
        require(threshold > 0 && threshold <= witnessSignerCount, "BAD_THRESHOLD");
        witnessThreshold = threshold;
        rulePoolUsdt = initialRulePoolUsdt;
        emit WitnessThresholdSet(threshold);
        emit RulePoolSynced(initialRulePoolUsdt);
    }

    function witnessDigest(
        address user,
        uint256 usdtAmount,
        bytes32 txHash,
        uint256 witnessDeadline
    ) external view returns (bytes32) {
        return _ethSignedHash(_structHash(user, usdtAmount, txHash, witnessDeadline));
    }

    function authorizeAndApplyDeposit(
        address user,
        uint256 usdtAmount,
        bytes32 txHash,
        uint256 principalBefore,
        uint256 witnessDeadline,
        bytes[] calldata signatures
    ) external onlyController returns (uint8 reason) {
        require(user != address(0), "ZERO_USER");
        require(txHash != bytes32(0), "ZERO_TX_HASH");
        require(!usedTransferTxHash[txHash], "TXHASH_USED");
        require(block.timestamp <= witnessDeadline, "WITNESS_EXPIRED");

        bytes32 digest = _ethSignedHash(_structHash(user, usdtAmount, txHash, witnessDeadline));
        _verifyWitnessSignatures(digest, signatures);

        uint256 poolBefore = rulePoolUsdt;
        uint256 principalAfter = principalBefore + usdtAmount;
        bool before1m = poolBefore < OPEN_CAP_POOL_THRESHOLD;

        if (principalAfter < MIN_DEPOSIT) {
            reason = 1;
        } else if (before1m && principalAfter > MAX_DEPOSIT_BEFORE_1M) {
            reason = 2;
        } else {
            reason = 0;
        }

        rulePoolUsdt = poolBefore + usdtAmount;
        usedTransferTxHash[txHash] = true;
        emit DepositAuthorized(txHash, user, usdtAmount, poolBefore, principalBefore, reason);
        return reason;
    }

    function notifyUsdtInflow(uint256 amount) external onlyController {
        if (amount == 0) return;
        rulePoolUsdt += amount;
    }

    function notifyUsdtOutflow(uint256 amount) external onlyController {
        if (amount == 0) return;
        if (amount >= rulePoolUsdt) {
            rulePoolUsdt = 0;
        } else {
            rulePoolUsdt -= amount;
        }
    }

    function notifyReservedUsdtIncrease(uint256 amount) external onlyController {
        if (amount == 0) return;
        if (amount >= rulePoolUsdt) {
            rulePoolUsdt = 0;
        } else {
            rulePoolUsdt -= amount;
        }
    }

    function notifyReservedUsdtDecrease(uint256 amount) external onlyController {
        if (amount == 0) return;
        rulePoolUsdt += amount;
    }

    function replaceWitnessSigner(address oldSigner, address newSigner) external onlyController {
        require(oldSigner != address(0) && newSigner != address(0), "ZERO_SIGNER");
        require(oldSigner != newSigner, "SAME_SIGNER");
        require(isWitnessSigner[oldSigner], "OLD_NOT_SIGNER");
        require(!isWitnessSigner[newSigner], "NEW_ALREADY_SIGNER");

        isWitnessSigner[oldSigner] = false;
        isWitnessSigner[newSigner] = true;

        emit WitnessSignerSet(oldSigner, false);
        emit WitnessSignerSet(newSigner, true);
    }

    function _structHash(
        address user,
        uint256 usdtAmount,
        bytes32 txHash,
        uint256 witnessDeadline
    ) internal view returns (bytes32) {
        return keccak256(
            abi.encode(
                WITNESS_TYPEHASH,
                block.chainid,
                controller,
                user,
                usdtAmount,
                txHash,
                witnessDeadline
            )
        );
    }

    function _ethSignedHash(bytes32 h) internal pure returns (bytes32) {
        return keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32", h));
    }

    function _verifyWitnessSignatures(bytes32 digest, bytes[] calldata signatures) internal view {
        require(signatures.length >= witnessThreshold, "SIGS_LT_THRESHOLD");
        address[] memory seen = new address[](signatures.length);
        uint256 valid = 0;

        for (uint256 i = 0; i < signatures.length; i++) {
            address signer = _recoverSigner(digest, signatures[i]);
            if (!isWitnessSigner[signer]) continue;

            bool duplicated = false;
            for (uint256 j = 0; j < valid; j++) {
                if (seen[j] == signer) {
                    duplicated = true;
                    break;
                }
            }
            if (duplicated) continue;

            seen[valid] = signer;
            valid += 1;
            if (valid >= witnessThreshold) return;
        }
        revert("INSUFFICIENT_WITNESS_SIGS");
    }

    function _recoverSigner(bytes32 digest, bytes calldata signature) internal pure returns (address) {
        if (signature.length != 65) return address(0);

        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := calldataload(signature.offset)
            s := calldataload(add(signature.offset, 32))
            v := byte(0, calldataload(add(signature.offset, 64)))
        }
        if (v < 27) v += 27;
        if (v != 27 && v != 28) return address(0);
        if (uint256(s) > SECP256K1N_HALF) return address(0);

        return ecrecover(digest, v, r, s);
    }
}

