// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";

contract CollectionModule15 is ERC721 {
    address public controller;

    constructor() ERC721("Collection", "NFT") { controller = msg.sender; }

    function mint(address to, uint256 id) external {
        require(msg.sender == controller, "only controller");
        _mint(to, id);
    }

    function reassign(uint256, address) public virtual { revert("operation unavailable"); }
}
