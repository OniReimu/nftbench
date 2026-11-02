// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC1155} from "@openzeppelin/contracts/token/ERC1155/ERC1155.sol";
import {ERC2981} from "@openzeppelin/contracts/token/common/ERC2981.sol";

contract BundleCollection14 is ERC1155, ERC2981 {
    address public immutable issuer;

    constructor() ERC1155("") {
        issuer = msg.sender;
    }

    function mint(address to, uint256 id, uint256 amount) external {
        require(msg.sender == issuer, "only issuer");
        _mint(to, id, amount, "");
    }

    function setRoyalty(uint256 id, address receiver, uint96 bps) external {
        require(msg.sender == issuer, "only issuer");
        _setTokenRoyalty(id, receiver, bps);
    }

    function supportsInterface(bytes4 interfaceId) public view override(ERC1155, ERC2981) returns (bool) {
        return super.supportsInterface(interfaceId);
    }
}
