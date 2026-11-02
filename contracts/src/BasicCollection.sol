// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";

contract BasicCollection is ERC721 {
    address public immutable issuer;

    constructor() ERC721("Basic Collection", "BASIC") {
        issuer = msg.sender;
    }

    function mint(address to, uint256 tokenId) external {
        require(msg.sender == issuer, "only issuer");
        _mint(to, tokenId);
    }
}
