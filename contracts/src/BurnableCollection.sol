// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {CollectionBase} from "./CollectionBase.sol";

contract BurnableCollection is CollectionBase {
    function burn(uint256 tokenId) external {
        require(msg.sender == _requireOwned(tokenId), "only owner");
        _burn(tokenId);
    }
}
