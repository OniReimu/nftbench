// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {CollectionModule15} from "./CollectionModule15.sol";

contract CollectionProxy15 is CollectionModule15 {
    address public implementation;
    bool public finalized;

    constructor(address implementation_) { implementation = implementation_; }

    function upgrade(address value) external {
        require(msg.sender == controller && !finalized, "upgrade unavailable");
        require(value.code.length > 0, "code required");
        implementation = value;
    }

    function finalize() external {
        require(msg.sender == controller && !finalized, "finalization unavailable");
        finalized = true;
    }

    function transferFrom(address from, address to, uint256 id) public override {
        _dispatch(abi.encodeWithSignature("transferFrom(address,address,uint256)", from, to, id));
    }

    function reassign(uint256 id, address to) public override {
        _dispatch(abi.encodeWithSignature("reassign(uint256,address)", id, to));
    }

    function _dispatch(bytes memory data) private {
        address target = implementation;
        (bool success, bytes memory result) = target.delegatecall(data);
        if (!success) assembly { revert(add(result, 32), mload(result)) }
    }
}
