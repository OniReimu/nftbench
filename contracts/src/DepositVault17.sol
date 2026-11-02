// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC1155Holder} from "@openzeppelin/contracts/token/ERC1155/utils/ERC1155Holder.sol";

contract DepositVault17 is ERC1155Holder {
    address public immutable manager;
    mapping(address => bool) public depositsEnabled;
    mapping(address => mapping(address => mapping(uint256 => uint256))) public creditOf;

    constructor(address manager_) { manager = manager_; }

    function setDepositsEnabled(address collection, bool enabled) external {
        require(msg.sender == manager, "only manager");
        depositsEnabled[collection] = enabled;
    }

    function onERC1155Received(address, address from, uint256 id, uint256 value, bytes memory)
        public override returns (bytes4)
    {
        require(depositsEnabled[msg.sender], "deposits unavailable");
        creditOf[msg.sender][from][id] += value;
        return this.onERC1155Received.selector;
    }

    function onERC1155BatchReceived(address, address from, uint256[] memory ids, uint256[] memory values, bytes memory)
        public override returns (bytes4)
    {
        require(depositsEnabled[msg.sender], "deposits unavailable");
        for (uint256 i; i < ids.length; ++i) creditOf[msg.sender][from][ids[i]] += values[i];
        return this.onERC1155BatchReceived.selector;
    }
}
