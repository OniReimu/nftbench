// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {CollectionBase} from "./CollectionBase.sol";

contract CollectionV2 is CollectionBase {
    function setLocked(uint256 tokenId, bool value) public override {
        require(value, "lock is permanent");
        super.setLocked(tokenId, value);
    }

    function setUser(uint256 tokenId, address user, uint64 expires) public override {
        require(!locked(tokenId), "token is locked");
        super.setUser(tokenId, user, expires);
    }
}
