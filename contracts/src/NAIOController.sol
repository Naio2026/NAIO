
pragma solidity ^0.8.24;

import {IMinimalERC20} from "./interfaces/IMinimalERC20.sol";
import {INAIOLike} from "./interfaces/INAIOLike.sol";
import {INodeSeatPoolLike} from "./interfaces/INodeSeatPoolLike.sol";
import {IDepositRuleEngineLike} from "./interfaces/IDepositRuleEngineLike.sol";

contract NAIOController {
    address public owner;
    address public immutable usdt;
    address public immutable naio;

    address public nodePool;
    address public marketPool;
    address public opsPool;

    address public ecoPool;
    address public independentPool;

    uint16 public depositNodeBps = 1000;
    uint16 public depositMarketBps = 500;
    uint16 public depositOpsBps = 500;
    uint16 public depositToLiquidityBps = 8000;

    mapping(address => uint256) public pendingUsdt;
    mapping(address => uint256) public pendingNaio;
    mapping(address => uint256) public pendingStaticNaio;
    uint256 public reservedUsdt;
    uint256 public reservedNaio;

    uint256 public referralPoolNaio;

    uint64 public systemStartTs;
    uint64 public lpOpenTs;
    uint32 public lastPokeEpoch;

    mapping(uint32 => uint256) public newUserRewardNaioByDay;
    mapping(uint32 => uint256) public newUserTotalPowerByDay;
    mapping(uint32 => mapping(address => uint256)) public newUserEligiblePower;
    mapping(uint32 => mapping(address => bool)) public newUserClaimed;
    uint32 internal constant MAX_NEWUSER_CLAIM_SCAN_DAYS = 300;
    mapping(address => uint32) private newUserClaimCursorDayPlus1;
    mapping(address => uint32) private newUserClaimCursorEpoch;

    uint32 public withdrawBurnEpoch;
    mapping(uint32 => uint256) public withdrawBurnUsedByEpoch;
    struct WithdrawRequest {
        address user;
        uint256 amount;
    }
    WithdrawRequest[] public withdrawQueue;
    uint256 public withdrawQueueHead;
    mapping(address => uint256) public withdrawQueuedAmount;

    mapping(address => uint256) public totalClaimedEarningsUsdt;

    struct DeflationSnapshot {
        uint32 epoch;
        uint64 timestamp;
        uint256 rateBps;
        uint256 priceBefore;
        uint256 poolTokenAmount;
        uint256 deflationAmount;
        uint256 burnAmount;
        uint256 ecoAmount;
        uint256 newUserAmount;
        uint256 nodeAmount;
        uint256 independentAmount;
        uint256 referralAmount;
        uint256 staticAmount;
        uint256 withdrawBurnConsumed;
    }
    struct DeflationSettleCache {
        uint256 priceBefore;
        uint256 burnAmount;
        uint256 ecoAmount;
        uint256 newUserAmount;
        uint256 nodeAmount;
        uint256 independentAmount;
        uint256 referralAmount;
        uint256 staticAmount;
        uint256 withdrawBurnConsumed;
    }
    mapping(uint32 => DeflationSnapshot) public deflationSnapshots;
    uint32 public lastDeflationSnapshotEpoch;

    struct UserInfo {
        uint256 principalUsdt;
        uint256 power;
        address referrer;
        uint16 directCount;
        uint64 lastClaimTs;
        uint64 firstDepositTs;
        uint256 rewardDebt;

        uint32 lastDepositEpoch;
        uint32 powerSnapEpoch;
        uint256 powerSnapAtDayStart;

        uint256 withdrawnUsdt;
        uint256 lockedUsdt;
    }
    mapping(address => UserInfo) public users;

    uint256 public totalPower;
    uint256 public accRewardPerPower;

    uint64 public lastPokeTs;
    uint32 public constant MAX_POKE_CATCHUP_EPOCHS = 30;
    uint256 public constant EPOCH_SECONDS = 1 days;
    uint32 public constant EPOCHS_PER_MONTH_FOR_UNLOCK = 30;

    mapping(address => bool) public keepers;
    bool public allowSeatDepositors;
    address public keeper;
    mapping(address => bool) public keeperGovernors;
    address public validatorGuardian;
    bool public keeperAccountingPaused;
    address public depositRuleEngine;

    address public referralRewardExcluded;
    address public poolSeeder;
    uint256 public seededPoolUsdt;
    uint256 public constant INITIAL_POOL_TARGET_USDT = 500_000e18;
    function _isAuthorizedDepositor(address caller) internal view returns (bool) {
        if (keepers[caller]) return true;
        if (!allowSeatDepositors) return false;
        if (nodePool == address(0)) return false;
        return INodeSeatPoolLike(nodePool).seatOf(caller) != 0;
    }

    mapping(bytes32 => bool) public processedTransfers;
    uint256 public trackedUsdtOutflow;
    uint256 public keeperObservedUsdtInflow;
    uint256 public keeperAvailableUsdtInflow;

    mapping(address => uint256) public totalSoldUsdt;

    uint256 public constant OP_CLAIM_NEWUSER = 1e14;
    uint256 public constant OP_POKE = 3e14;
    uint256 public constant OP_GENESIS_PARTNER = 4e14;
    uint256 public constant OP_CLAIM_STATIC = 5e14;
    uint256 public constant OP_CLAIM_DYNAMIC = 6e14;
    uint256 public constant OP_CLAIM_NODE = 7e14;
    uint256 public constant OP_WITHDRAW_LP = 888e12;
    uint256 public constant OP_CLAIM_ALL = 9e14;

    uint16 public constant DEFLATION_BURN_BPS = 4000;
    uint16 public constant DEFLATION_ECO_BPS = 500;
    uint16 public constant DEFLATION_NEWUSER_BPS = 100;
    uint16 public constant DEFLATION_NODE_BPS = 400;
    uint16 public constant DEFLATION_INDEPENDENT_BPS = 450;
    uint16 public constant DEFLATION_REFERRAL_BPS = 1050;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event PoolsSet(address indexed nodePool, address indexed marketPool, address indexed opsPool);
    event RewardReceiversSet(address indexed ecoPool, address indexed independentPool);
    event Deposit(address indexed user, uint256 usdtAmount, address indexed referrer, uint256 powerAdded);
    event Poke(uint256 indexed timestamp, uint256 rateBps);
    event DepositBpsSet(uint16 nodeBps, uint16 marketBps, uint16 opsBps, uint16 liquidityBps);
    event KeeperSet(address indexed keeper);
    event KeeperStatusSet(address indexed keeper, bool enabled);
    event KeeperGovernorSet(address indexed governor, bool enabled);
    event ValidatorGuardianSet(address indexed previousGuardian, address indexed newGuardian);
    event KeeperAccountingPausedSet(bool paused, address indexed operator);
    event KeeperAccountingVetoed(address indexed validator);
    event DepositRuleEngineSet(address indexed previousEngine, address indexed newEngine);
    event ReferralRewardExcludedSet(address indexed account);
    event PoolSeederSet(address indexed seeder);
    event ReferralBound(address indexed user, address indexed inviter);
    event DepositFromTransfer(address indexed user, uint256 usdtAmount, bytes32 indexed txHash);
    event DepositRefunded(address indexed user, uint256 usdtAmount, bytes32 indexed txHash, uint8 reason);
    event InitialFundingDeposited(address indexed user, uint256 usdtAmount);
    event PoolSeeded(address indexed operator, uint256 usdtAmount, uint256 seededTotal, uint256 seededTarget);
    event StaticRewardClaimed(address indexed user, uint256 amount);
    event DynamicRewardClaimed(address indexed user, uint256 amount);
    event DeflationExecutedDetailed(
        uint32 indexed epoch,
        uint256 indexed timestamp,
        uint256 rateBps,
        uint256 priceBefore,
        uint256 poolTokenAmount,
        uint256 deflationAmount,
        uint256 burnAmount,
        uint256 ecoAmount,
        uint256 newUserAmount,
        uint256 nodeAmount,
        uint256 independentAmount,
        uint256 referralAmount,
        uint256 staticAmount,
        uint256 withdrawBurnConsumed
    );
    event LPWithdrawn(address indexed user, uint256 lpAmount, uint256 usdtReturned, uint256 tokenBurned);

    error ZeroRuleEngine();
    error RuleEngineImmutable();
    error RuleEngineNotSet();
    error PoolsNotSet();
    error UsdtBalanceLtAmount();
    error UsdtRefundFailed();
    error BurnAddrNotSet();
    error NaioBurnTransferFailed();
    error UsdtTransferFailed();
    error OpsTransferFailed();
    error ZeroAddress();
    error NotOwner();
    error NotKeeperGovernor();
    error ZeroOwner();
    error BpsNot100();
    error ZeroGovernor();
    error NotValidator();
    error OnlyNaioToken();
    error NotStarted();
    error NotReady();
    error AlreadyPokedToday();
    error NoDeflation();
    error NodePoolNotSet();
    error NaioNodeTransferFailed();
    error EcoPoolNotSet();
    error IndepPoolNotSet();
    error NotKeeper();
    error ZeroUser();
    error AlreadyProcessed();
    error NoFreshInflow();
    error ZeroKeeper();
    error ZeroGuardian();
    error MinMinDeposit();
    error NotPoolSeeder();
    error ZeroAmount();
    error SeedExceedsTarget();
    error UsdtNodeTransferFailed();
    error MarketPoolNotSet();
    error OpsPoolNotSet();
    error NoReward();
    error ReservedNaioLt();
    error NaioTransferFailed();
    error NotSelf();
    error NoNaio();
    error NoUsdt();
    error ReservedUsdtLt();
    error DayNotOver();
    error AlreadyClaimed();
    error NotEligible();
    error NoTotalEligible();
    error NotNode();
    error NoDeposit();
    error NoPrincipal();
    error NoWithdrawable();
    error AlreadyQueued();
    error PokeCatchupRequired();
    error ZeroBurn();
    error BurnQuotaExceeded();
    error PriceZero();
    error BurnZero();
    error InsufficientPoolNaio();
    error ZeroSeller();
    error InsufficientPoolUsdt();
    event WithdrawQueued(address indexed user, uint256 amount);
    event WithdrawProcessed(
        address indexed user,
        uint256 usdtReturned,
        uint256 naioBurned,
        uint256 dailyBurnUsed,
        uint256 dailyBurnRemaining
    );

    constructor(address _usdt, address _naio) {
        if (_usdt == address(0) || _naio == address(0)) revert ZeroAddress();
        owner = msg.sender;
        usdt = _usdt;
        naio = _naio;
        keeperObservedUsdtInflow = IMinimalERC20(_usdt).balanceOf(address(this));
        emit OwnershipTransferred(address(0), msg.sender);
    }

    function _onlyOwner() private view {
        if (msg.sender != owner) revert NotOwner();
    }

    function _onlyKeeperGovernor() private view {
        if (!keeperGovernors[msg.sender]) revert NotKeeperGovernor();
    }

    function transferOwnership(address newOwner) external {
        _onlyOwner();
        if (newOwner == address(0)) revert ZeroOwner();
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    function renounceOwnership() external {
        _onlyOwner();
        emit OwnershipTransferred(owner, address(0));
        owner = address(0);
    }

    function setPools(address _nodePool, address _marketPool, address _opsPool) external {
        _onlyOwner();
        if (_nodePool == address(0) || _marketPool == address(0) || _opsPool == address(0)) revert ZeroAddress();
        nodePool = _nodePool;
        marketPool = _marketPool;
        opsPool = _opsPool;
        emit PoolsSet(_nodePool, _marketPool, _opsPool);
    }

    function setRewardReceivers(address _ecoPool, address _independentPool) external {
        _onlyOwner();
        if (_ecoPool == address(0) || _independentPool == address(0)) revert ZeroAddress();
        ecoPool = _ecoPool;
        independentPool = _independentPool;
        emit RewardReceiversSet(_ecoPool, _independentPool);
    }

    function setDepositBps(uint16 nodeBps, uint16 marketBps, uint16 opsBps, uint16 liquidityBps) external {
        _onlyOwner();
        if (uint256(nodeBps) + uint256(marketBps) + uint256(opsBps) + uint256(liquidityBps) != 10000) {
            revert BpsNot100();
        }
        depositNodeBps = nodeBps;
        depositMarketBps = marketBps;
        depositOpsBps = opsBps;
        depositToLiquidityBps = liquidityBps;
        emit DepositBpsSet(nodeBps, marketBps, opsBps, liquidityBps);
    }

    function setKeeper(address _keeper) external {
        _onlyOwner();
        _setKeeper(_keeper);
    }

    function setKeeperStatus(address _keeper, bool enabled) external {
        _onlyOwner();
        _setKeeperStatus(_keeper, enabled);
    }

    function setKeeperGovernor(address governor, bool enabled) external {
        _onlyOwner();
        if (governor == address(0)) revert ZeroGovernor();
        keeperGovernors[governor] = enabled;
        emit KeeperGovernorSet(governor, enabled);
    }

    function setKeeperByGovernor(address _keeper) external {
        _onlyKeeperGovernor();
        _setKeeper(_keeper);
    }

    function setKeeperStatusByGovernor(address _keeper, bool enabled) external {
        _onlyKeeperGovernor();
        _setKeeperStatus(_keeper, enabled);
    }

    function setValidatorGuardian(address guardian) external {
        _onlyOwner();
        _setValidatorGuardian(guardian);
    }

    function setValidatorGuardianByGovernor(address guardian) external {
        _onlyKeeperGovernor();
        _setValidatorGuardian(guardian);
    }

    function setKeeperAccountingPaused(bool paused) external {
        _onlyOwner();
        _setKeeperAccountingPaused(paused);
    }

    function setKeeperAccountingPausedByGovernor(bool paused) external {
        _onlyKeeperGovernor();
        _setKeeperAccountingPaused(paused);
    }

    function replaceWitnessSignerByGovernor(address oldSigner, address newSigner) external {
        _onlyKeeperGovernor();
        if (depositRuleEngine == address(0)) revert RuleEngineNotSet();
        IDepositRuleEngineLike(depositRuleEngine).replaceWitnessSigner(oldSigner, newSigner);
    }

    function setDepositRuleEngine(address engine) external {
        _onlyOwner();
        if (engine == address(0)) revert ZeroRuleEngine();
        if (depositRuleEngine != address(0)) revert RuleEngineImmutable();
        address prev = depositRuleEngine;
        depositRuleEngine = engine;
        emit DepositRuleEngineSet(prev, engine);
    }

    function validatorVetoPause() external {
        if (msg.sender != validatorGuardian) revert NotValidator();
        if (!keeperAccountingPaused) {
            _setKeeperAccountingPaused(true);
        }
        emit KeeperAccountingVetoed(msg.sender);
    }

    function setAllowSeatDepositors(bool enabled) external {
        _onlyOwner();
        allowSeatDepositors = enabled;
    }

    function setReferralRewardExcluded(address account) external {
        _onlyOwner();
        referralRewardExcluded = account;
        emit ReferralRewardExcludedSet(account);
    }

    function setPoolSeeder(address seeder) external {
        _onlyOwner();
        if (seeder == address(0)) revert ZeroAddress();
        poolSeeder = seeder;
        emit PoolSeederSet(seeder);
    }

    function bindReferral(address user, address inviter) external returns (bool) {
        if (msg.sender != naio) revert OnlyNaioToken();
        if (user == address(0) || inviter == address(0) || user == inviter) return false;

        UserInfo storage u = users[user];
        if (u.referrer != address(0)) return false;

        address current = inviter;
        for (uint256 depth = 0; depth < 50 && current != address(0); depth++) {
            if (current == user) return false;
            current = users[current].referrer;
        }

        u.referrer = inviter;
        users[inviter].directCount += 1;
        emit ReferralBound(user, inviter);
        return true;
    }

    function poke() public {
        if (systemStartTs == 0) revert NotStarted();

        uint32 currentEpoch = _currentEpoch(block.timestamp);
        if (currentEpoch == 0) revert NotReady();
        uint32 sentinel = type(uint32).max;
        if (lastPokeEpoch != sentinel) {
            if (lastPokeEpoch >= currentEpoch - 1) revert AlreadyPokedToday();
        }

        uint32 startEpoch = (lastPokeEpoch == sentinel) ? 0 : (lastPokeEpoch + 1);
        uint32 targetEpoch = currentEpoch - 1;
        if (lastPokeEpoch == sentinel) {
            if (targetEpoch >= MAX_POKE_CATCHUP_EPOCHS) {
                targetEpoch = MAX_POKE_CATCHUP_EPOCHS - 1;
            }
        } else {
            uint32 maxTarget = lastPokeEpoch + MAX_POKE_CATCHUP_EPOCHS;
            if (targetEpoch > maxTarget) targetEpoch = maxTarget;
        }

        uint32 settled = 0;
        uint32 epoch = startEpoch;
        uint32 lastSettledEpoch = startEpoch;

        while (epoch <= targetEpoch) {
            uint256 rateBps = _getDailyReleaseBpsAtEpoch(epoch);
            uint256 poolTokenAmount = _getPoolTokenAmount();
            if (poolTokenAmount == 0 || rateBps == 0) {
                if (settled == 0) revert NoDeflation();
                break;
            }

            uint256 deflationAmount = (poolTokenAmount * rateBps) / 10000;
            if (deflationAmount == 0) {
                if (settled == 0) revert NoDeflation();
                break;
            }

            _settleDeflationEpoch(epoch, rateBps, poolTokenAmount, deflationAmount);
            settled += 1;
            lastSettledEpoch = epoch;
            unchecked {
                epoch += 1;
            }
        }

        if (settled == 0) revert NoDeflation();
        lastPokeEpoch = lastSettledEpoch;
        lastPokeTs = uint64(block.timestamp);
        emit Poke(block.timestamp, _getDailyReleaseBpsAtEpoch(lastSettledEpoch));
    }

    function _settleDeflationEpoch(uint32 epoch, uint256 rateBps, uint256 poolTokenAmount, uint256 deflationAmount) internal {
        _rolloverNewUserReward(epoch);

        DeflationSettleCache memory c = _buildDeflationSettleCache(deflationAmount);
        c.withdrawBurnConsumed = _executeBurnForEpoch(epoch, c.burnAmount);
        _applyDeflationAllocations(epoch, c);
        _recordDeflation(epoch, rateBps, poolTokenAmount, deflationAmount, c);
    }

    function _rolloverNewUserReward(uint32 epoch) internal {
        if (epoch == 0) return;
        uint32 y = epoch - 1;
        uint256 yReward = newUserRewardNaioByDay[y];
        if (yReward > 0 && newUserTotalPowerByDay[y] == 0) {
            newUserRewardNaioByDay[epoch] += yReward;
            newUserRewardNaioByDay[y] = 0;
        }
    }

    function _buildDeflationSettleCache(uint256 deflationAmount) internal view returns (DeflationSettleCache memory c) {
        c.priceBefore = _getPrice();
        c.burnAmount = (deflationAmount * DEFLATION_BURN_BPS) / 10000;
        c.ecoAmount = (deflationAmount * DEFLATION_ECO_BPS) / 10000;
        c.newUserAmount = (deflationAmount * DEFLATION_NEWUSER_BPS) / 10000;
        c.nodeAmount = (deflationAmount * DEFLATION_NODE_BPS) / 10000;
        c.independentAmount = (deflationAmount * DEFLATION_INDEPENDENT_BPS) / 10000;
        c.referralAmount = (deflationAmount * DEFLATION_REFERRAL_BPS) / 10000;
        c.staticAmount = deflationAmount - c.burnAmount - c.ecoAmount - c.newUserAmount - c.nodeAmount
            - c.independentAmount - c.referralAmount;
    }

    function _executeBurnForEpoch(uint32 epoch, uint256 burnAmount) internal returns (uint256 withdrawBurnConsumed) {
        uint256 burnToExecute = burnAmount;
        withdrawBurnConsumed = withdrawBurnUsedByEpoch[epoch];
        if (withdrawBurnConsumed > 0) {
            if (withdrawBurnConsumed >= burnAmount) {
                withdrawBurnConsumed = burnAmount;
                burnToExecute = 0;
            } else {
                burnToExecute = burnAmount - withdrawBurnConsumed;
            }
        }

        if (burnToExecute > 0) {
            address burnAddr = INAIOLike(naio).burnAddress();
            if (burnAddr == address(0)) revert BurnAddrNotSet();
            if (!INAIOLike(naio).transfer(burnAddr, burnToExecute)) revert NaioBurnTransferFailed();
        }
    }

    function _applyDeflationAllocations(uint32 epoch, DeflationSettleCache memory c) internal {
        if (c.nodeAmount > 0) {
            if (nodePool == address(0)) revert NodePoolNotSet();
            if (!INAIOLike(naio).transfer(nodePool, c.nodeAmount)) revert NaioNodeTransferFailed();
            INodeSeatPoolLike(nodePool).notifyNaio(c.nodeAmount);
        }

        if (c.ecoAmount > 0) {
            if (ecoPool == address(0)) revert EcoPoolNotSet();
            pendingNaio[ecoPool] += c.ecoAmount;
            reservedNaio += c.ecoAmount;
        }

        if (c.independentAmount > 0) {
            if (independentPool == address(0)) revert IndepPoolNotSet();
            pendingNaio[independentPool] += c.independentAmount;
            reservedNaio += c.independentAmount;
        }

        if (c.newUserAmount > 0) {
            newUserRewardNaioByDay[epoch] += c.newUserAmount;
            reservedNaio += c.newUserAmount;
        }

        if (c.referralAmount > 0) {
            referralPoolNaio += c.referralAmount;
            reservedNaio += c.referralAmount;
        }

        if (c.staticAmount > 0) {
            reservedNaio += c.staticAmount;
            if (totalPower > 0) {
                accRewardPerPower += (c.staticAmount * 1e18) / totalPower;
            }
        }
    }

    function _recordDeflation(
        uint32 epoch,
        uint256 rateBps,
        uint256 poolTokenAmount,
        uint256 deflationAmount,
        DeflationSettleCache memory c
    ) internal {
        deflationSnapshots[epoch] = DeflationSnapshot({
            epoch: epoch,
            timestamp: uint64(block.timestamp),
            rateBps: rateBps,
            priceBefore: c.priceBefore,
            poolTokenAmount: poolTokenAmount,
            deflationAmount: deflationAmount,
            burnAmount: c.burnAmount,
            ecoAmount: c.ecoAmount,
            newUserAmount: c.newUserAmount,
            nodeAmount: c.nodeAmount,
            independentAmount: c.independentAmount,
            referralAmount: c.referralAmount,
            staticAmount: c.staticAmount,
            withdrawBurnConsumed: c.withdrawBurnConsumed
        });
        lastDeflationSnapshotEpoch = epoch;

        emit DeflationExecutedDetailed(
            epoch,
            block.timestamp,
            rateBps,
            c.priceBefore,
            poolTokenAmount,
            deflationAmount,
            c.burnAmount,
            c.ecoAmount,
            c.newUserAmount,
            c.nodeAmount,
            c.independentAmount,
            c.referralAmount,
            c.staticAmount,
            c.withdrawBurnConsumed
        );
    }

    function _getPoolUsdt() internal view returns (uint256) {
        uint256 bal = IMinimalERC20(usdt).balanceOf(address(this));
        if (bal <= reservedUsdt) return 0;
        return bal - reservedUsdt;
    }

    function _trackUsdtOutflow(uint256 amount) internal {
        if (amount == 0) return;
        trackedUsdtOutflow += amount;
        if (depositRuleEngine != address(0)) {
            IDepositRuleEngineLike(depositRuleEngine).notifyUsdtOutflow(amount);
        }
    }

    function _notifyRuleEngineUsdtInflow(uint256 amount) internal {
        if (amount == 0 || depositRuleEngine == address(0)) return;
        IDepositRuleEngineLike(depositRuleEngine).notifyUsdtInflow(amount);
    }

    function _notifyRuleEngineReservedIncrease(uint256 amount) internal {
        if (amount == 0 || depositRuleEngine == address(0)) return;
        IDepositRuleEngineLike(depositRuleEngine).notifyReservedUsdtIncrease(amount);
    }

    function _notifyRuleEngineReservedDecrease(uint256 amount) internal {
        if (amount == 0 || depositRuleEngine == address(0)) return;
        IDepositRuleEngineLike(depositRuleEngine).notifyReservedUsdtDecrease(amount);
    }

    function _refreshKeeperUsdtInflow() internal {
        uint256 bal = IMinimalERC20(usdt).balanceOf(address(this));
        uint256 cumulativeInflow = bal + trackedUsdtOutflow;
        if (cumulativeInflow > keeperObservedUsdtInflow) {
            keeperAvailableUsdtInflow += (cumulativeInflow - keeperObservedUsdtInflow);
        }
        keeperObservedUsdtInflow = cumulativeInflow;
    }

    function _getPoolTokenAmount() internal view returns (uint256) {
        uint256 bal = INAIOLike(naio).balanceOf(address(this));
        if (bal <= reservedNaio) return 0;
        return bal - reservedNaio;
    }

    function _getTotalTokenSupply() internal view returns (uint256) {
        uint256 supply = INAIOLike(naio).totalSupply();
        address burnAddr = INAIOLike(naio).burnAddress();
        if (burnAddr == address(0)) return supply;
        uint256 burned = INAIOLike(naio).balanceOf(burnAddr);
        if (burned >= supply) return 0;
        return supply - burned;
    }

    function getPrice() external view returns (uint256) {
        return _getPrice();
    }

    function _getPrice() internal view returns (uint256) {
        if (depositRuleEngine == address(0)) return 0;
        uint256 poolUsdt = IDepositRuleEngineLike(depositRuleEngine).rulePoolUsdt();
        uint256 totalSupply = _getTotalTokenSupply();

        if (totalSupply == 0) {
            return 0;
        }

        return (poolUsdt * 1e18) / totalSupply;
    }

    function depositFromTransferWitness(
        address user,
        uint256 usdtAmount,
        bytes32 txHash,
        uint256 witnessDeadline,
        bytes[] calldata signatures
    ) external {
        if (!_isAuthorizedDepositor(msg.sender)) revert NotKeeper();
        if (depositRuleEngine == address(0)) revert RuleEngineNotSet();
        if (user == address(0)) revert ZeroUser();
        if (processedTransfers[txHash]) revert AlreadyProcessed();

        uint8 reason = IDepositRuleEngineLike(depositRuleEngine).authorizeAndApplyDeposit(
            user,
            usdtAmount,
            txHash,
            users[user].principalUsdt,
            witnessDeadline,
            signatures
        );

        _refreshKeeperUsdtInflow();
        if (keeperAvailableUsdtInflow < usdtAmount) revert NoFreshInflow();
        keeperAvailableUsdtInflow -= usdtAmount;
        if (IMinimalERC20(usdt).balanceOf(address(this)) < usdtAmount) revert UsdtBalanceLtAmount();

        if (keeperAccountingPaused || reason != 0) {
            _refundWitnessDeposit(user, usdtAmount, txHash, keeperAccountingPaused ? 3 : reason);
            return;
        }

        if (nodePool == address(0) || marketPool == address(0) || opsPool == address(0)) revert PoolsNotSet();
        processedTransfers[txHash] = true;
        _depositInternal(user, usdtAmount, true);
        emit DepositFromTransfer(user, usdtAmount, txHash);
    }

    function _setKeeper(address _keeper) internal {
        if (_keeper == address(0)) revert ZeroKeeper();
        keeper = _keeper;
        keepers[_keeper] = true;
        emit KeeperSet(_keeper);
        emit KeeperStatusSet(_keeper, true);
    }

    function _setKeeperStatus(address _keeper, bool enabled) internal {
        if (_keeper == address(0)) revert ZeroKeeper();
        keepers[_keeper] = enabled;
        if (!enabled && keeper == _keeper) {
            keeper = address(0);
        }
        emit KeeperStatusSet(_keeper, enabled);
    }

    function _setValidatorGuardian(address guardian) internal {
        if (guardian == address(0)) revert ZeroGuardian();
        address prev = validatorGuardian;
        validatorGuardian = guardian;
        emit ValidatorGuardianSet(prev, guardian);
    }

    function _setKeeperAccountingPaused(bool paused) internal {
        keeperAccountingPaused = paused;
        emit KeeperAccountingPausedSet(paused, msg.sender);
    }

    function _refundWitnessDeposit(address user, uint256 usdtAmount, bytes32 txHash, uint8 reason) internal {
        processedTransfers[txHash] = true;
        _trackUsdtOutflow(usdtAmount);
        if (!IMinimalERC20(usdt).transfer(user, usdtAmount)) revert UsdtRefundFailed();
        emit DepositRefunded(user, usdtAmount, txHash, reason);
    }

    function depositInitialFunding(address user, uint256 usdtAmount) external {
        _onlyOwner();
        if (user == address(0)) revert ZeroUser();
        if (usdtAmount < 100e18) revert MinMinDeposit();
        if (IMinimalERC20(usdt).balanceOf(address(this)) < usdtAmount) revert UsdtBalanceLtAmount();

        _notifyRuleEngineUsdtInflow(usdtAmount);
        _depositInternal(user, usdtAmount, true);

        emit InitialFundingDeposited(user, usdtAmount);
    }

    function seedPoolUsdtFromSeeder(uint256 usdtAmount) external {
        if (msg.sender != poolSeeder) revert NotPoolSeeder();
        if (usdtAmount == 0) revert ZeroAmount();

        uint256 newTotal = seededPoolUsdt + usdtAmount;
        if (newTotal > INITIAL_POOL_TARGET_USDT) revert SeedExceedsTarget();
        if (!IMinimalERC20(usdt).transferFrom(msg.sender, address(this), usdtAmount)) revert UsdtTransferFailed();
        _notifyRuleEngineUsdtInflow(usdtAmount);

        seededPoolUsdt = newTotal;
        emit PoolSeeded(msg.sender, usdtAmount, newTotal, INITIAL_POOL_TARGET_USDT);
    }

    function _depositInternal(address user, uint256 usdtAmount, bool isTransferMode) internal {
        UserInfo storage u = users[user];
        bool hadDepositBefore = (u.firstDepositTs != 0);

        if (systemStartTs == 0) {
            systemStartTs = uint64(block.timestamp);
            lpOpenTs = uint64(block.timestamp);
            lastPokeEpoch = type(uint32).max;
        }

        uint32 today = _currentEpoch(block.timestamp);

        if (u.powerSnapEpoch < today) {
            u.powerSnapEpoch = today;
            u.powerSnapAtDayStart = u.power;
        }

        if (u.lastDepositEpoch != today) {
            u.lastDepositEpoch = today;
        }

        if (!isTransferMode) {
            if (!IMinimalERC20(usdt).transferFrom(user, address(this), usdtAmount)) revert UsdtTransferFailed();
        }

        if (u.power > 0) {
            _harvestStaticToPending(user);
        }

        uint256 nodeAmt = (usdtAmount * depositNodeBps) / 10000;
        uint256 marketAmt = (usdtAmount * depositMarketBps) / 10000;
        uint256 opsAmt = (usdtAmount * depositOpsBps) / 10000;
        uint256 liqAmt = usdtAmount - nodeAmt - marketAmt - opsAmt;

        if (nodeAmt > 0) {
            if (nodePool == address(0)) revert NodePoolNotSet();
            _trackUsdtOutflow(nodeAmt);
            if (!IMinimalERC20(usdt).transfer(nodePool, nodeAmt)) revert UsdtNodeTransferFailed();
            INodeSeatPoolLike(nodePool).notifyUsdt(nodeAmt);
        }
        if (marketAmt > 0) {
            if (marketPool == address(0)) revert MarketPoolNotSet();
            pendingUsdt[marketPool] += marketAmt;
            reservedUsdt += marketAmt;
            _notifyRuleEngineReservedIncrease(marketAmt);
        }
        if (opsAmt > 0) {
            if (opsPool == address(0)) revert OpsPoolNotSet();
            pendingUsdt[opsPool] += opsAmt;
            reservedUsdt += opsAmt;
            _notifyRuleEngineReservedIncrease(opsAmt);
        }

        u.principalUsdt += usdtAmount;
        u.lockedUsdt += liqAmt;
        if (u.firstDepositTs == 0) {
            u.firstDepositTs = uint64(block.timestamp);
        }
        uint256 multiplierBps = 10000;
        if (lpOpenTs > 0 && block.timestamp >= uint256(lpOpenTs)) {
            uint256 epoch = (block.timestamp - uint256(lpOpenTs)) / EPOCH_SECONDS;
            multiplierBps += epoch * 150;
        }
        uint256 powerAdded = (liqAmt * multiplierBps) / 10000;
        u.power += powerAdded;
        totalPower += powerAdded;

        if (hadDepositBefore && powerAdded > 0) {
            newUserEligiblePower[today][user] += powerAdded;
            newUserTotalPowerByDay[today] += powerAdded;
        }

        u.rewardDebt = (u.power * accRewardPerPower) / 1e18;
        u.lastClaimTs = uint64(block.timestamp);

        emit Deposit(user, usdtAmount, u.referrer, powerAdded);
    }

    function _harvestStaticToPending(address user) internal {
        UserInfo storage u = users[user];
        if (u.power == 0) {
            u.rewardDebt = 0;
            return;
        }

        uint256 accumulated = (u.power * accRewardPerPower) / 1e18;
        uint256 pending = accumulated - u.rewardDebt;
        if (pending > 0) {
            pendingStaticNaio[user] += pending;
        }
        u.rewardDebt = accumulated;
    }

    function _addClaimedEarningsUsdt(address user, uint256 usdtAmount) internal {
        if (usdtAmount == 0) return;
        totalClaimedEarningsUsdt[user] += usdtAmount;
    }

    function _addClaimedEarningsNaio(address user, uint256 naioAmount) internal {
        if (naioAmount == 0) return;
        uint256 price = _getPrice();
        if (price == 0) return;
        uint256 usdtEq = (naioAmount * price) / 1e18;
        totalClaimedEarningsUsdt[user] += usdtEq;
    }

    function _claimStatic(address user) internal {
        UserInfo storage u = users[user];

        if (u.power > 0) {
            _harvestStaticToPending(user);
        }

        uint256 amount = pendingStaticNaio[user];
        if (amount == 0) revert NoReward();
        pendingStaticNaio[user] = 0;

        _allocateReferralRewards(user, amount);

        if (reservedNaio < amount) revert ReservedNaioLt();
        reservedNaio -= amount;
        if (!INAIOLike(naio).transfer(user, amount)) revert NaioTransferFailed();
        _addClaimedEarningsNaio(user, amount);
        emit StaticRewardClaimed(user, amount);
    }

    function claimStatic() external {
        _claimStatic(msg.sender);
    }

    function claimStaticFor(address user) external {
        if (msg.sender != user && msg.sender != address(this)) revert NotSelf();
        _claimStatic(user);
    }

    function _claimDynamic(address user) internal {
        uint256 amount = pendingNaio[user];
        if (amount == 0) revert NoReward();
        pendingNaio[user] = 0;
        if (reservedNaio < amount) revert ReservedNaioLt();
        reservedNaio -= amount;
        if (!INAIOLike(naio).transfer(user, amount)) revert NaioTransferFailed();
        _addClaimedEarningsNaio(user, amount);
        emit DynamicRewardClaimed(user, amount);
    }

    function claimDynamic() external {
        _claimDynamic(msg.sender);
    }

    function claimDynamicFor(address user) external {
        if (msg.sender != user && msg.sender != address(this)) revert NotSelf();
        _claimDynamic(user);
    }

    function claimNaio() external {
        uint256 amount = pendingNaio[msg.sender];
        if (amount == 0) revert NoNaio();
        pendingNaio[msg.sender] = 0;
        if (reservedNaio < amount) revert ReservedNaioLt();
        reservedNaio -= amount;
        if (!INAIOLike(naio).transfer(msg.sender, amount)) revert NaioTransferFailed();
        _addClaimedEarningsNaio(msg.sender, amount);
    }

    function claimAll() public {
        address user = msg.sender;

        _tryCallNoRevert(address(this), abi.encodeWithSelector(this.claimStaticFor.selector, user));

        _tryCallNoRevert(address(this), abi.encodeWithSelector(this.claimDynamicFor.selector, user));

        if (nodePool != address(0)) {
            _tryCallNoRevert(nodePool, abi.encodeWithSelector(INodeSeatPoolLike.claimFor.selector, user));
        }

        _tryCallNoRevert(address(this), abi.encodeWithSelector(this.claimUsdtFor.selector, user));

        _tryCallNoRevert(address(this), abi.encodeWithSelector(this.claimAccumulatedNewUserReward.selector));
    }

    function _tryCallNoRevert(address target, bytes memory data) internal {
        (bool ok,) = target.call(data);
        ok;
    }

    function _claimUsdt(address user) internal {
        uint256 amount = pendingUsdt[user];
        if (amount == 0) revert NoUsdt();
        pendingUsdt[user] = 0;
        if (reservedUsdt < amount) revert ReservedUsdtLt();
        reservedUsdt -= amount;
        _notifyRuleEngineReservedDecrease(amount);
        _trackUsdtOutflow(amount);
        if (!IMinimalERC20(usdt).transfer(user, amount)) revert UsdtTransferFailed();
        _addClaimedEarningsUsdt(user, amount);
    }

    function claimUsdt() external {
        _claimUsdt(msg.sender);
    }

    function claimUsdtFor(address user) external {
        if (msg.sender != user && msg.sender != address(this)) revert NotSelf();
        _claimUsdt(user);
    }

    function _claimAccumulatedNewUserReward(address user) internal {
        uint32 today = _currentEpoch(block.timestamp);
        if (today == 0) revert NotReady();

        uint32 day;
        if (newUserClaimCursorEpoch[user] != today) {
            newUserClaimCursorEpoch[user] = today;
            day = today - 1;
        } else {
            uint32 cursorPlus1 = newUserClaimCursorDayPlus1[user];
            day = cursorPlus1 == 0 ? (today - 1) : (cursorPlus1 - 1);
            if (day >= today) {
                day = today - 1;
            }
        }

        uint256 totalReward = 0;
        uint32 scanned = 0;
        bool exhausted = false;
        while (true) {
            if (!newUserClaimed[day][user]) {
                uint256 eligible = newUserEligiblePower[day][user];
                if (eligible > 0) {
                    uint256 totalEligible = newUserTotalPowerByDay[day];
                    if (totalEligible == 0) revert NoTotalEligible();

                    uint256 pool = newUserRewardNaioByDay[day];
                    if (pool > 0) {
                        uint256 reward = (pool * eligible) / totalEligible;
                        newUserClaimed[day][user] = true;
                        totalReward += reward;
                    }
                }
            }

            if (day == 0) {
                newUserClaimCursorDayPlus1[user] = 1; 
                exhausted = true;
                break;
            } else {
                newUserClaimCursorDayPlus1[user] = day;
            }

            scanned += 1;
            if (scanned >= MAX_NEWUSER_CLAIM_SCAN_DAYS) break;
            day -= 1;
        }

        if (totalReward == 0) revert NoReward();

        if (reservedNaio < totalReward) revert ReservedNaioLt();
        reservedNaio -= totalReward;
        if (!INAIOLike(naio).transfer(user, totalReward)) revert NaioTransferFailed();
        _addClaimedEarningsNaio(user, totalReward);
    }

    function claimAccumulatedNewUserReward() external {
        _claimAccumulatedNewUserReward(msg.sender);
    }

    function processWithdrawQueue(uint256 maxSteps) external {
        _processWithdrawQueue(maxSteps);
    }

    receive() external payable {
        uint256 opType = msg.value;
        address user = msg.sender;

        if (opType == OP_CLAIM_STATIC) {
            _claimStatic(user);
        } else if (opType == OP_CLAIM_DYNAMIC) {
            _claimDynamic(user);
        } else if (opType == OP_CLAIM_NEWUSER) {
            _claimAccumulatedNewUserReward(user);
        } else if (opType == OP_GENESIS_PARTNER) {
            _claimUsdt(user);
        } else if (opType == OP_POKE) {
            poke();
        } else if (opType == OP_CLAIM_NODE) {
            if (nodePool == address(0)) revert NodePoolNotSet();
            if (INodeSeatPoolLike(nodePool).seatOf(user) == 0) revert NotNode();
            INodeSeatPoolLike(nodePool).claimFor(user);
        } else if (opType == OP_WITHDRAW_LP) {
            _withdrawLP(user);
        } else if (opType == OP_CLAIM_ALL) {
            claimAll();
        } else {
            payable(user).transfer(msg.value);
            return;
        }

        payable(user).transfer(msg.value);
    }

    function _currentEpoch(uint256 ts) internal view returns (uint32) {
        if (systemStartTs == 0) return 0;
        if (ts <= uint256(systemStartTs)) return 0;
        return uint32((ts - uint256(systemStartTs)) / EPOCH_SECONDS);
    }

    function _withdrawBurnEpochFor(uint32 today) internal view returns (uint32 burnEpoch) {
        if (systemStartTs == 0) return 0;
        return today;
    }

    function _getDailyReleaseBpsAtEpoch(uint32 epoch) internal pure returns (uint256) {
        uint256 monthIndex = uint256(epoch) / uint256(EPOCHS_PER_MONTH_FOR_UNLOCK);
        uint256 steps = monthIndex >= 10 ? 10 : monthIndex;
        return 200 + (steps * 10);
    }

    function getCurrentEpoch() external view returns (uint32) {
        return _currentEpoch(block.timestamp);
    }

    function _computeWithdrawBurnQuota(uint32 epoch) internal view returns (uint256 quota) {
        if (systemStartTs == 0) return 0;
        uint256 poolTokenAmount = _getPoolTokenAmount() + withdrawBurnUsedByEpoch[epoch];
        uint256 rateBps = _getDailyReleaseBpsAtEpoch(epoch);
        uint256 c = rateBps * uint256(DEFLATION_BURN_BPS);
        if (poolTokenAmount == 0 || c == 0) return 0;
        return (poolTokenAmount * c) / (100_000_000 + c);
    }

    function withdrawBurnQuotaToken() external view returns (uint256) {
        return _computeWithdrawBurnQuota(_currentEpoch(block.timestamp));
    }

    function withdrawBurnUsedToken() external view returns (uint256) {
        return withdrawBurnUsedByEpoch[_currentEpoch(block.timestamp)];
    }

    function _rateBpsForGen(uint8 gen) internal pure returns (uint16) {
        if (gen == 1) return 600;
        if (gen == 2) return 500;
        if (gen == 3) return 400;
        if (gen == 4) return 300;
        if (gen == 5) return 200;
        if (gen >= 6 && gen <= 10) return 100;
        if (gen >= 11 && gen <= 20) return 50;
        return 0;
    }

    function _requiredDirectForGen(uint8 gen) internal pure returns (uint16) {
        if (gen >= 1 && gen <= 5) return uint16(gen);
        if (gen >= 6 && gen <= 10) return 6;
        if (gen >= 11 && gen <= 20) return 12;
        return type(uint16).max;
    }

    function _allocateReferralRewards(address downline, uint256 downlineStaticReward) internal {
        if (downlineStaticReward == 0 || referralPoolNaio == 0) return;

        address current = users[downline].referrer;
        if (current == address(0)) return;

        for (uint8 gen = 1; gen <= 20 && current != address(0) && referralPoolNaio > 0; gen++) {
            uint16 rateBps = _rateBpsForGen(gen);
            if (rateBps == 0) break;

            UserInfo storage up = users[current];
            if (
                current != referralRewardExcluded &&
                up.principalUsdt >= 100e18 &&
                up.directCount >= _requiredDirectForGen(gen)
            ) {
                uint256 amt = (downlineStaticReward * uint256(rateBps)) / 10000;
                if (amt > 0) {
                    if (amt > referralPoolNaio) {
                        amt = referralPoolNaio;
                    }
                    referralPoolNaio -= amt;
                    pendingNaio[current] += amt;
                }
            }

            current = up.referrer;
        }
    }

    function _getWithdrawableAmount(address user) internal view returns (uint256 amount) {
        UserInfo storage u = users[user];
        if (u.firstDepositTs == 0) revert NoDeposit();
        if (u.lockedUsdt == 0) revert NoPrincipal();
        if (totalClaimedEarningsUsdt[user] >= u.principalUsdt * 2) return 0;

        uint256 epochsSinceFirst = (block.timestamp - uint256(u.firstDepositTs)) / EPOCH_SECONDS;
        uint256 oneMonth = uint256(EPOCHS_PER_MONTH_FOR_UNLOCK);
        uint256 unlockBps;
        if (epochsSinceFirst >= 2 * oneMonth) {
            unlockBps = 8000;  
        } else if (epochsSinceFirst >= oneMonth) {
            unlockBps = 6000;  
        } else {
            unlockBps = 4000;  
        }

        uint256 unlocked = (u.principalUsdt * unlockBps) / 10000;
        if (unlocked > u.lockedUsdt) unlocked = u.lockedUsdt;

        if (unlocked <= u.withdrawnUsdt) revert NoWithdrawable();
        amount = unlocked - u.withdrawnUsdt;
    }

    function _queueWithdraw(address user) internal {
        uint256 amount = _getWithdrawableAmount(user);
        if (amount == 0) revert NoWithdrawable();
        if (withdrawQueuedAmount[user] != 0) revert AlreadyQueued();
        withdrawQueue.push(WithdrawRequest({user: user, amount: amount}));
        withdrawQueuedAmount[user] = amount;
        emit WithdrawQueued(user, amount);
    }

    function _consumeWithdrawBurnQuota(uint256 burnAmount) internal returns (uint256 remainingAfter) {
        if (burnAmount == 0) revert ZeroBurn();
        uint32 today = _currentEpoch(block.timestamp);
        uint32 burnEpoch = _withdrawBurnEpochFor(today);

        uint256 quota = _computeWithdrawBurnQuota(burnEpoch);
        uint256 used = withdrawBurnUsedByEpoch[burnEpoch];
        if (used + burnAmount > quota) revert BurnQuotaExceeded();

        withdrawBurnUsedByEpoch[burnEpoch] = used + burnAmount;
        withdrawBurnEpoch = burnEpoch;
        remainingAfter = quota - withdrawBurnUsedByEpoch[burnEpoch];
    }

    function _executeWithdraw(address user, uint256 requestedUsdt) internal returns (uint256 paidUsdt, uint256 burnedNaio, uint256 burnRemaining) {
        UserInfo storage u = users[user];
        if (requestedUsdt == 0) return (0, 0, 0);

        if (u.power > 0) {
            _harvestStaticToPending(user);
        }

        uint256 withdrawnBefore = u.withdrawnUsdt;
        if (u.lockedUsdt <= withdrawnBefore) return (0, 0, 0);
        uint256 remainingLockedBefore = u.lockedUsdt - withdrawnBefore;

        uint256 price = _getPrice();
        if (price == 0) revert PriceZero();
        burnedNaio = (requestedUsdt * 1e18 + price - 1) / price;
        if (burnedNaio == 0) revert BurnZero();
        if (_getPoolTokenAmount() < burnedNaio) revert InsufficientPoolNaio();
        burnRemaining = _consumeWithdrawBurnQuota(burnedNaio);

        address burnAddr = INAIOLike(naio).burnAddress();
        if (burnAddr == address(0)) revert BurnAddrNotSet();
        if (!INAIOLike(naio).transfer(burnAddr, burnedNaio)) revert NaioBurnTransferFailed();

        _trackUsdtOutflow(requestedUsdt);
        if (!IMinimalERC20(usdt).transfer(user, requestedUsdt)) revert UsdtTransferFailed();

        u.withdrawnUsdt = withdrawnBefore + requestedUsdt;

        if (u.power > 0) {
            uint256 powerReduction = (u.power * requestedUsdt) / remainingLockedBefore;
            if (powerReduction > u.power) powerReduction = u.power;
            u.power -= powerReduction;
            if (powerReduction > totalPower) {
                totalPower = 0;
            } else {
                totalPower -= powerReduction;
            }
            u.rewardDebt = (u.power * accRewardPerPower) / 1e18;
        }

        emit LPWithdrawn(user, 0, requestedUsdt, burnedNaio);
        return (requestedUsdt, burnedNaio, burnRemaining);
    }

    function _processWithdrawQueue(uint256 maxSteps) internal {
        if (maxSteps == 0) return;

        uint32 today = _currentEpoch(block.timestamp);
        uint32 burnEpoch = _withdrawBurnEpochFor(today);
        uint256 quota = _computeWithdrawBurnQuota(burnEpoch);
        uint256 used = withdrawBurnUsedByEpoch[burnEpoch];
        uint256 burnRemainingBefore = quota > used ? (quota - used) : 0;

        uint256 steps = 0;
        while (steps < maxSteps && withdrawQueueHead < withdrawQueue.length) {
            address user = withdrawQueue[withdrawQueueHead].user;
            uint256 remaining = withdrawQueuedAmount[user];
            if (remaining == 0) {
                withdrawQueueHead += 1;
                steps += 1;
                continue;
            }

            burnRemainingBefore = quota > withdrawBurnUsedByEpoch[burnEpoch] ? (quota - withdrawBurnUsedByEpoch[burnEpoch]) : 0;
            if (burnRemainingBefore == 0) break;

            uint256 price = _getPrice();
            if (price == 0) break;

            uint256 maxUsdtByBurn = (burnRemainingBefore * price) / 1e18;
            if (maxUsdtByBurn == 0) break;

            uint256 pay = remaining;
            if (pay > maxUsdtByBurn) pay = maxUsdtByBurn;
            uint256 poolUsdt = _getPoolUsdt();
            if (pay > poolUsdt) pay = poolUsdt;
            if (pay == 0) break;

            (uint256 paidUsdt, uint256 burnedNaio, uint256 burnRemainingAfter) = _executeWithdraw(user, pay);
            if (paidUsdt == 0) {
                withdrawQueuedAmount[user] = 0;
                withdrawQueueHead += 1;
                steps += 1;
                continue;
            }

            remaining -= paidUsdt;
            withdrawQueuedAmount[user] = remaining;
            emit WithdrawProcessed(user, paidUsdt, burnedNaio, this.withdrawBurnUsedToken(), burnRemainingAfter);

            if (remaining == 0) {
                withdrawQueueHead += 1;
            }

            steps += 1;
        }
    }

    function recordSaleAndGetBusinessTax(
        address seller,
        uint256 soldUsdtAmount
    ) external returns (uint16 businessTaxBps, uint16 poolTaxBps, uint16 opsTaxBps) {
        if (msg.sender != naio) revert OnlyNaioToken();
        return _recordSaleAndGetBusinessTaxInternal(seller, soldUsdtAmount);
    }

    function _recordSaleAndGetBusinessTaxInternal(
        address seller,
        uint256 soldUsdtAmount
    ) internal returns (uint16 businessTaxBps, uint16 poolTaxBps, uint16 opsTaxBps) {
        if (seller == address(0)) revert ZeroSeller();
        if (soldUsdtAmount == 0) revert ZeroAmount();

        totalSoldUsdt[seller] += soldUsdtAmount;

        UserInfo storage u = users[seller];
        uint256 principal = u.principalUsdt;

        if (principal == 0) {
            return (1000, 700, 300);
        }

        uint256 multiple = (totalSoldUsdt[seller] * 1e18) / principal;

        if (multiple > 20e18) {
            return (2000, 1400, 600);
        } else {
            return (1000, 700, 300);
        }
    }

    function _withdrawLP(address user) internal {
        if (withdrawQueuedAmount[user] != 0) {
            _processWithdrawQueue(20);
            return;
        }

        uint256 amount = _getWithdrawableAmount(user);

        if (withdrawQueueHead >= withdrawQueue.length) {
            uint32 today = _currentEpoch(block.timestamp);
            uint32 burnEpoch = _withdrawBurnEpochFor(today);
            uint256 quota = _computeWithdrawBurnQuota(burnEpoch);
            uint256 used = withdrawBurnUsedByEpoch[burnEpoch];
            uint256 burnRemainingBefore = quota > used ? (quota - used) : 0;
            if (burnRemainingBefore > 0) {
                uint256 price = _getPrice();
                if (price == 0) revert PriceZero();

                uint256 maxUsdtByBurn = (burnRemainingBefore * price) / 1e18;
                uint256 pay = amount;
                if (pay > maxUsdtByBurn) pay = maxUsdtByBurn;

                uint256 poolUsdt = _getPoolUsdt();
                if (pay > poolUsdt) pay = poolUsdt;

                if (pay > 0) {
                    (uint256 paidUsdt, uint256 burnedNaio, uint256 burnRemainingAfter) = _executeWithdraw(user, pay);
                    if (paidUsdt > 0) {
                        emit WithdrawProcessed(user, paidUsdt, burnedNaio, this.withdrawBurnUsedToken(), burnRemainingAfter);
                        if (paidUsdt == amount) {
                            return;
                        }
                    }
                }
            }
        }

        if (withdrawQueuedAmount[user] == 0) {
            _queueWithdraw(user);
        }
        _processWithdrawQueue(20);
    }

    function onNAIOReceived(address from, uint256 amount) external returns (bool) {
        if (msg.sender != naio) revert OnlyNaioToken();
        if (from == address(0)) revert ZeroSeller();
        if (amount == 0) revert ZeroAmount();

        _sellNAIOInternal(from, amount);

        return true;
    }

    function _sellNAIOInternal(address seller, uint256 tokenAmount) internal returns (uint256 usdtReceived) {
        uint256 price = _getPrice();
        if (price == 0) revert PriceZero();
        uint256 usdtGross = (tokenAmount * price) / 1e18;

        uint256 burnAmt = (tokenAmount * 9500) / 10000;

        (uint16 businessTaxBps, uint16 poolTaxBps, uint16 opsTaxBps) = _recordSaleAndGetBusinessTaxInternal(seller, usdtGross);

        uint256 poolTaxUsdt = 0;
        uint256 opsTaxUsdt = 0;
        if (businessTaxBps > 0 && usdtGross > 0) {
            poolTaxUsdt = (usdtGross * uint256(poolTaxBps)) / 10000;
            opsTaxUsdt = (usdtGross * uint256(opsTaxBps)) / 10000;
        }

        usdtReceived = usdtGross - poolTaxUsdt - opsTaxUsdt;

        uint256 poolUsdt = _getPoolUsdt();
        if (poolUsdt < usdtReceived + opsTaxUsdt) revert InsufficientPoolUsdt();

        if (burnAmt > 0) {
            address burnAddr = INAIOLike(naio).burnAddress();
            if (burnAddr == address(0)) revert BurnAddrNotSet();
            if (!INAIOLike(naio).transfer(burnAddr, burnAmt)) revert NaioBurnTransferFailed();
        }

        if (opsTaxUsdt > 0) {
            if (opsPool == address(0)) revert OpsPoolNotSet();
            _trackUsdtOutflow(opsTaxUsdt);
            if (!IMinimalERC20(usdt).transfer(opsPool, opsTaxUsdt)) revert OpsTransferFailed();
        }

        if (usdtReceived > 0) {
            _trackUsdtOutflow(usdtReceived);
            if (!IMinimalERC20(usdt).transfer(seller, usdtReceived)) revert UsdtTransferFailed();
        }

        emit LPWithdrawn(seller, tokenAmount, usdtReceived, burnAmt);
        return usdtReceived;
    }

    function withdrawLP() external {
        _withdrawLP(msg.sender);
    }
}

