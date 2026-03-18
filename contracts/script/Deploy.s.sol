
pragma solidity ^0.8.24;

import "forge-std/Script.sol";

import {NAIOToken} from "../src/NAIOToken.sol";
import {NAIOController} from "../src/NAIOController.sol";
import {InitialPoolSeeder} from "../src/InitialPoolSeeder.sol";
import {NodeSeatPool} from "../src/pools/NodeSeatPool.sol";
import {DepositWitnessRuleEngine} from "../src/DepositWitnessRuleEngine.sol";

contract Deploy is Script {
    address constant BURN_ADDR = 0x000000000000000000000000000000000000dEaD;

    uint256 constant INITIAL_SUPPLY = 100_000_000e18;
    uint16 constant MAX_SEATS = 1000;
    uint256 constant REFERRAL_BOOTSTRAP_NAIO = 1_000e18;
    uint256 constant INITIAL_POOL_TARGET_USDT = 500_000e18;

    function _parseHexUint256(string memory s) internal pure returns (uint256 v) {
        bytes memory b = bytes(s);
        require(b.length > 0, "EMPTY_HEX");

        uint256 i = 0;
        if (b.length >= 2 && b[0] == "0" && (b[1] == "x" || b[1] == "X")) {
            i = 2;
        }
        require(b.length - i <= 64, "HEX_TOO_LONG");

        for (; i < b.length; i++) {
            uint8 c = uint8(b[i]);
            uint8 nibble;
            if (c >= 48 && c <= 57) {
                nibble = c - 48; 
            } else if (c >= 65 && c <= 70) {
                nibble = c - 55; 
            } else if (c >= 97 && c <= 102) {
                nibble = c - 87; 
            } else {
                revert("BAD_HEX_CHAR");
            }
            v = (v << 4) | uint256(nibble);
        }
    }

    function _isWhitespace(bytes1 c) internal pure returns (bool) {
        return c == 0x20 || c == 0x09 || c == 0x0a || c == 0x0d;
    }

    function _trim(string memory s) internal pure returns (string memory) {
        bytes memory b = bytes(s);
        if (b.length == 0) return s;
        uint256 start = 0;
        uint256 end = b.length;
        while (start < end && _isWhitespace(b[start])) start++;
        while (end > start && _isWhitespace(b[end - 1])) end--;
        if (start == 0 && end == b.length) return s;
        bytes memory out = new bytes(end - start);
        for (uint256 i = 0; i < out.length; i++) out[i] = b[start + i];
        return string(out);
    }

    function _readNodeOwners() internal view returns (address[] memory owners) {
        
        string memory path = string.concat(vm.projectRoot(), "/nodes_list.txt");
        string memory content = vm.readFile(path);
        bytes memory b = bytes(content);

        owners = new address[](MAX_SEATS);
        uint256 count = 0;
        uint256 lineStart = 0;

        for (uint256 i = 0; i <= b.length; i++) {
            bool atEnd = (i == b.length);
            if (!atEnd && b[i] != 0x0a) continue; 

            uint256 lineEnd = i;
            if (lineEnd > lineStart && b[lineEnd - 1] == 0x0d) {
                lineEnd -= 1; 
            }
            if (lineEnd > lineStart) {
                bytes memory lineBytes = new bytes(lineEnd - lineStart);
                for (uint256 j = 0; j < lineBytes.length; j++) {
                    lineBytes[j] = b[lineStart + j];
                }
                string memory line = _trim(string(lineBytes));
                if (bytes(line).length > 0) {
                    require(count < MAX_SEATS, "NODES_LIST_TOO_LONG");
                    owners[count] = vm.parseAddress(line);
                    require(owners[count] != address(0), "NODES_LIST_ZERO_ADDR");
                    count++;
                }
            }
            lineStart = i + 1;
        }

        require(count == MAX_SEATS, "NODES_LIST_NOT_1000");
        for (uint256 a = 0; a < MAX_SEATS; a++) {
            for (uint256 c = a + 1; c < MAX_SEATS; c++) {
                require(owners[a] != owners[c], "NODES_LIST_DUP");
            }
        }
    }

    function run() external {
        address usdtToken = vm.envAddress("USDT_ADDRESS");
        address TRANSFER_TAX_RECEIVER_C = vm.envAddress("TRANSFER_TAX_RECEIVER_C");
        address ECO_A = vm.envAddress("ECO_A");
        address INDEPENDENT_B = vm.envAddress("INDEPENDENT_B");
        address MARKET_E = vm.envAddress("MARKET_E");
        address MARKET_F = vm.envAddress("MARKET_F");
        address REFERRAL_BOOTSTRAP_ADDRESS = vm.envAddress("REFERRAL_BOOTSTRAP_ADDRESS");
        address KEEPER_GOVERNOR_ADDRESS = vm.envAddress("KEEPER_GOVERNOR_ADDRESS");
        address WITNESS_SIGNER_1 = vm.envAddress("WITNESS_SIGNER_1");
        address WITNESS_SIGNER_2 = vm.envAddress("WITNESS_SIGNER_2");
        address WITNESS_SIGNER_3 = vm.envAddress("WITNESS_SIGNER_3");
        uint16 WITNESS_THRESHOLD = 3;

        uint256 keeperPk = _parseHexUint256(vm.envString("KEEPER_PRIVATE_KEY"));
        address keeperAddr = vm.addr(keeperPk);

        uint256 validatorPk = _parseHexUint256(vm.envString("VALIDATOR_PRIVATE_KEY"));
        address validatorAddr = vm.addr(validatorPk);

        require(TRANSFER_TAX_RECEIVER_C != address(0), "TRANSFER_TAX_RECEIVER_C not set");
        require(ECO_A != address(0), "ECO_A not set");
        require(INDEPENDENT_B != address(0), "INDEPENDENT_B not set");
        require(MARKET_E != address(0), "MARKET_E not set");
        require(MARKET_F != address(0), "MARKET_F not set");
        require(REFERRAL_BOOTSTRAP_ADDRESS != address(0), "REFERRAL_BOOTSTRAP_ADDRESS not set");
        require(KEEPER_GOVERNOR_ADDRESS != address(0), "KEEPER_GOVERNOR_ADDRESS not set");
        require(WITNESS_SIGNER_1 != address(0), "WITNESS_SIGNER_1 not set");
        require(WITNESS_SIGNER_2 != address(0), "WITNESS_SIGNER_2 not set");
        require(WITNESS_SIGNER_3 != address(0), "WITNESS_SIGNER_3 not set");
        require(keeperAddr != address(0), "KEEPER_PRIVATE_KEY not set");
        require(validatorAddr != address(0), "VALIDATOR_PRIVATE_KEY not set");
        require(usdtToken != address(0), "USDT_ADDRESS not set");

        vm.startBroadcast();

        console.log("==========================================");
        console.log("Deploy NAIO contracts to BSC Mainnet");
        console.log("==========================================");
        console.log("USDT_TOKEN:", usdtToken);

        console.log("\n1. Deploy NAIOToken...");
        NAIOToken aio = new NAIOToken("NAIO", "NAIO", INITIAL_SUPPLY, BURN_ADDR, TRANSFER_TAX_RECEIVER_C);
        console.log("   NAIOToken:", address(aio));

        console.log("\n2. Deploy NAIOController...");
        NAIOController controller = new NAIOController(usdtToken, address(aio));
        console.log("   NAIOController:", address(controller));

        console.log("\n2.1 Deploy DepositWitnessRuleEngine...");
        address[] memory witnessSigners = new address[](3);
        witnessSigners[0] = WITNESS_SIGNER_1;
        witnessSigners[1] = WITNESS_SIGNER_2;
        witnessSigners[2] = WITNESS_SIGNER_3;
        DepositWitnessRuleEngine ruleEngine = new DepositWitnessRuleEngine(
            address(controller),
            witnessSigners,
            WITNESS_THRESHOLD,
            0
        );
        console.log("   DepositWitnessRuleEngine:", address(ruleEngine));

        console.log("\n2.2 Deploy InitialPoolSeeder...");
        InitialPoolSeeder seeder = new InitialPoolSeeder(address(controller), INITIAL_POOL_TARGET_USDT);
        console.log("   InitialPoolSeeder:", address(seeder));

        console.log("\n3. Bootstrap referral binding with fixed distributor...");
        aio.transfer(REFERRAL_BOOTSTRAP_ADDRESS, REFERRAL_BOOTSTRAP_NAIO);
        aio.transfer(address(controller), INITIAL_SUPPLY - REFERRAL_BOOTSTRAP_NAIO);
        console.log("   OK: transferred 1000 NAIO to bootstrap address");
        console.log("   OK: transferred 99999000 NAIO to Controller");

        console.log("\n4. Deploy NodeSeatPool...");
        NodeSeatPool nodePool = new NodeSeatPool(usdtToken, address(aio));
        nodePool.setController(address(controller));
        console.log("   NodeSeatPool:", address(nodePool));

        console.log("\n5. Init 1000 node seats...");
        address[] memory owners = _readNodeOwners();
        for (uint256 batch = 0; batch < 10; batch++) {
            uint256 start = batch * 100 + 1;
            address[] memory batchOwners = new address[](100);
            for (uint256 i = 0; i < 100; i++) {
                batchOwners[i] = owners[start - 1 + i];
            }
            nodePool.setInitialOwners(uint16(start), batchOwners);
        }
        nodePool.seal();
        console.log("   OK: 1000 seats initialized and sealed");

        console.log("\n6. Configure Controller...");
        controller.setPools(address(nodePool), MARKET_E, MARKET_F);
        controller.setRewardReceivers(ECO_A, INDEPENDENT_B);
        controller.setKeeper(keeperAddr);
        controller.setKeeperGovernor(KEEPER_GOVERNOR_ADDRESS, true);
        controller.setReferralRewardExcluded(REFERRAL_BOOTSTRAP_ADDRESS);
        controller.setPoolSeeder(address(seeder));
        controller.setDepositRuleEngine(address(ruleEngine));
        controller.setValidatorGuardian(validatorAddr);
        console.log("   OK: Controller configured (incl. validatorGuardian)");

        console.log("\n7. Set NAIOToken controller...");
        aio.setController(address(controller));
        console.log("   OK: token controller set");

        console.log("\n8. Transfer ownerships to burn address...");
        controller.transferOwnership(BURN_ADDR);
        aio.transferOwnership(BURN_ADDR);
        nodePool.transferOwnership(BURN_ADDR);
        seeder.transferOwnership(BURN_ADDR);
        console.log("   OK: controller/token/pools ownership moved to", BURN_ADDR);
        console.log("   OK: ruleEngine has no owner mutators (immutable committee mode)");

        vm.stopBroadcast();

        console.log("\n==========================================");
        console.log("Done.");
        console.log("==========================================");
        console.log("NAIOToken:", address(aio));
        console.log("NAIOController:", address(controller));
        console.log("DepositWitnessRuleEngine:", address(ruleEngine));
        console.log("InitialPoolSeeder:", address(seeder));
        console.log("NodeSeatPool:", address(nodePool));
        console.log("\nIMPORTANT: record addresses and fill CONTROLLER_ADDRESS / NODE_SEAT_POOL_ADDRESS in .env");
        console.log("NOTE: controller/token/pools ownership burned; ruleEngine has no owner upgrade path");
    }
}

