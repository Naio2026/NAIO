
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import {KeeperCouncil} from "../src/KeeperCouncil.sol";

contract DeployKeeperCouncil is Script {
    function run() external {
        address[5] memory members = [
            vm.envAddress("KEEPER_COUNCIL_MEMBER_1"),
            vm.envAddress("KEEPER_COUNCIL_MEMBER_2"),
            vm.envAddress("KEEPER_COUNCIL_MEMBER_3"),
            vm.envAddress("KEEPER_COUNCIL_MEMBER_4"),
            vm.envAddress("KEEPER_COUNCIL_MEMBER_5")
        ];

        string memory controllerStr = vm.envOr("CONTROLLER_ADDRESS", string(""));
        address controller = address(0);
        if (bytes(controllerStr).length > 0) {
            controller = vm.parseAddress(controllerStr);
        }

        vm.startBroadcast();
        KeeperCouncil council = new KeeperCouncil(controller, members);
        vm.stopBroadcast();

        console.log("==========================================");
        console.log("KeeperCouncil deployed");
        console.log("==========================================");
        console.log("KeeperCouncil:", address(council));
        console.log("Initial controller:", controller);
    }
}
