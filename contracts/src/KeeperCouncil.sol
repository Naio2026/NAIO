
pragma solidity ^0.8.24;

contract KeeperCouncil {
    enum ProposalKind {
        KeeperCall,
        ReplaceMember,
        SetController
    }

    struct Proposal {
        ProposalKind kind;
        bool executed;
        uint8 approvalCount;
        address proposer;
        uint64 createdAt;
        bytes callData;
        uint8 memberIndex;
        address newMember;
    }

    address public controller;
    address[5] public members;
    mapping(address => bool) public isMember;

    mapping(uint256 => Proposal) private _proposals;
    mapping(uint256 => mapping(address => bool)) public hasApproved;
    uint256 public nextProposalId;

    uint8 public constant KEEPER_OPS_THRESHOLD = 3;
    uint8 public constant MEMBER_OPS_THRESHOLD = 5;

    bytes4 private constant SEL_SET_KEEPER = bytes4(keccak256("setKeeperByGovernor(address)"));
    bytes4 private constant SEL_SET_KEEPER_STATUS = bytes4(keccak256("setKeeperStatusByGovernor(address,bool)"));
    bytes4 private constant SEL_SET_KEEPER_PAUSED = bytes4(keccak256("setKeeperAccountingPausedByGovernor(bool)"));
    bytes4 private constant SEL_SET_VALIDATOR = bytes4(keccak256("setValidatorGuardianByGovernor(address)"));
    bytes4 private constant SEL_REPLACE_WITNESS = bytes4(keccak256("replaceWitnessSignerByGovernor(address,address)"));

    event KeeperProposalCreated(uint256 indexed proposalId, address indexed proposer, bytes4 selector, bytes data);
    event MemberProposalCreated(uint256 indexed proposalId, address indexed proposer, uint8 memberIndex, address newMember);
    event ProposalApproved(uint256 indexed proposalId, address indexed approver, uint8 approvalCount);
    event ProposalExecuted(uint256 indexed proposalId);
    event MemberReplaced(uint8 indexed memberIndex, address indexed oldMember, address indexed newMember);
    event ControllerSet(address indexed previousController, address indexed newController);

    modifier onlyMember() {
        require(isMember[msg.sender], "NOT_COUNCIL_MEMBER");
        _;
    }

    constructor(address controller_, address[5] memory initialMembers) {
        controller = controller_;
        for (uint8 i = 0; i < 5; i++) {
            address m = initialMembers[i];
            require(m != address(0), "ZERO_MEMBER");
            require(!isMember[m], "DUP_MEMBER");
            members[i] = m;
            isMember[m] = true;
        }
    }

    function getProposal(uint256 proposalId)
        external
        view
        returns (
            ProposalKind kind,
            bool executed,
            uint8 approvalCount,
            address proposer,
            uint64 createdAt,
            bytes memory callData,
            uint8 memberIndex,
            address newMember
        )
    {
        Proposal storage p = _proposals[proposalId];
        return (p.kind, p.executed, p.approvalCount, p.proposer, p.createdAt, p.callData, p.memberIndex, p.newMember);
    }

    function createKeeperProposal(bytes calldata callData) external onlyMember returns (uint256 proposalId) {
        bytes4 sel = _selectorFromCalldata(callData);
        require(_isAllowedKeeperSelector(sel), "UNSUPPORTED_SELECTOR");

        proposalId = nextProposalId++;
        Proposal storage p = _proposals[proposalId];
        p.kind = ProposalKind.KeeperCall;
        p.proposer = msg.sender;
        p.createdAt = uint64(block.timestamp);
        p.callData = callData;

        emit KeeperProposalCreated(proposalId, msg.sender, sel, callData);
        _approve(proposalId, msg.sender);
    }

    function createMemberReplaceProposal(uint8 memberIndex, address newMember)
        external
        onlyMember
        returns (uint256 proposalId)
    {
        require(memberIndex < 5, "BAD_INDEX");
        require(newMember != address(0), "ZERO_MEMBER");
        require(!isMember[newMember], "ALREADY_MEMBER");

        proposalId = nextProposalId++;
        Proposal storage p = _proposals[proposalId];
        p.kind = ProposalKind.ReplaceMember;
        p.proposer = msg.sender;
        p.createdAt = uint64(block.timestamp);
        p.memberIndex = memberIndex;
        p.newMember = newMember;

        emit MemberProposalCreated(proposalId, msg.sender, memberIndex, newMember);
        _approve(proposalId, msg.sender);
    }

    function createSetControllerProposal(address newController) external onlyMember returns (uint256 proposalId) {
        require(newController != address(0), "ZERO_CONTROLLER");

        proposalId = nextProposalId++;
        Proposal storage p = _proposals[proposalId];
        p.kind = ProposalKind.SetController;
        p.proposer = msg.sender;
        p.createdAt = uint64(block.timestamp);
        p.newMember = newController;

        _approve(proposalId, msg.sender);
    }

    function approveProposal(uint256 proposalId) external onlyMember {
        _approve(proposalId, msg.sender);
    }

    function executeProposal(uint256 proposalId) external onlyMember {
        Proposal storage p = _proposals[proposalId];
        require(p.proposer != address(0), "NO_PROPOSAL");
        require(!p.executed, "ALREADY_EXECUTED");
        require(p.approvalCount >= _requiredThreshold(p), "INSUFFICIENT_APPROVALS");

        p.executed = true;

        if (p.kind == ProposalKind.KeeperCall) {
            require(controller != address(0), "CONTROLLER_NOT_SET");
            (bool ok, bytes memory ret) = controller.call(p.callData);
            if (!ok) _revertWithReason(ret);
        } else if (p.kind == ProposalKind.ReplaceMember) {
            address oldMember = members[p.memberIndex];
            require(oldMember != address(0), "EMPTY_SLOT");
            require(!isMember[p.newMember], "ALREADY_MEMBER");

            isMember[oldMember] = false;
            isMember[p.newMember] = true;
            members[p.memberIndex] = p.newMember;
            emit MemberReplaced(p.memberIndex, oldMember, p.newMember);
        } else {
            address prev = controller;
            controller = p.newMember;
            emit ControllerSet(prev, p.newMember);
        }

        emit ProposalExecuted(proposalId);
    }

    function _approve(uint256 proposalId, address approver) internal {
        Proposal storage p = _proposals[proposalId];
        require(p.proposer != address(0), "NO_PROPOSAL");
        require(!p.executed, "ALREADY_EXECUTED");
        require(!hasApproved[proposalId][approver], "ALREADY_APPROVED");

        hasApproved[proposalId][approver] = true;
        p.approvalCount += 1;
        emit ProposalApproved(proposalId, approver, p.approvalCount);
    }

    function _requiredThreshold(Proposal storage p) internal view returns (uint8) {
        if (p.kind == ProposalKind.ReplaceMember || p.kind == ProposalKind.SetController) return MEMBER_OPS_THRESHOLD;
        if (p.kind == ProposalKind.KeeperCall && _selectorFromBytes(p.callData) == SEL_REPLACE_WITNESS) {
            return MEMBER_OPS_THRESHOLD;
        }
        return KEEPER_OPS_THRESHOLD;
    }

    function _selectorFromCalldata(bytes calldata callData) internal pure returns (bytes4 sel) {
        require(callData.length >= 4, "BAD_CALLDATA");
        assembly {
            sel := calldataload(callData.offset)
        }
    }

    function _selectorFromBytes(bytes memory callData) internal pure returns (bytes4 sel) {
        require(callData.length >= 4, "BAD_CALLDATA");
        assembly {
            sel := mload(add(callData, 0x20))
        }
    }

    function _isAllowedKeeperSelector(bytes4 sel) internal pure returns (bool) {
        return sel == SEL_SET_KEEPER
            || sel == SEL_SET_KEEPER_STATUS
            || sel == SEL_SET_KEEPER_PAUSED
            || sel == SEL_SET_VALIDATOR
            || sel == SEL_REPLACE_WITNESS;
    }

    function _revertWithReason(bytes memory returnData) internal pure {
        if (returnData.length == 0) revert("CALL_FAILED");
        assembly {
            revert(add(returnData, 0x20), mload(returnData))
        }
    }
}
