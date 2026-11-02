// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {CollectionModule15} from "./CollectionModule15.sol";

contract CollectionExtension15 is CollectionModule15 {
    function reassign(uint256 id, address to) public override {
        require(msg.sender == controller, "only controller");
        _transfer(ownerOf(id), to, id);
    }
}
