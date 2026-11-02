// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";

contract Collection16 is ERC721 {
    address public immutable minter;

    constructor(address minter_) ERC721("Collection", "NFT") {
        minter = minter_;
    }

    function mint(address to, uint256 tokenId) external {
        require(msg.sender == minter, "only minter");
        _mint(to, tokenId);
    }

    function exists(uint256 tokenId) external view returns (bool) {
        return _ownerOf(tokenId) != address(0);
    }
}
