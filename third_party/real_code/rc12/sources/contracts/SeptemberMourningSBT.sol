// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {ERC721URIStorage} from "@openzeppelin/contracts/token/ERC721/extensions/ERC721URIStorage.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @notice Minimal EIP-5192-style soulbound NFT for the September Mourning campaign.
contract SeptemberMourningSBT is ERC721URIStorage, Ownable {
    uint256 private _nextTokenId = 1;
    string public defaultTokenURI;

    event Locked(uint256 tokenId);

    error SoulboundToken();
    error TokenDoesNotExist(uint256 tokenId);

    constructor(address initialOwner, string memory tokenURI_)
        ERC721("Welcome to the Reaperverse", "SMSBT")
        Ownable(initialOwner)
    {
        defaultTokenURI = tokenURI_;
    }

    function claim() external returns (uint256 tokenId) {
        tokenId = _mintToken(msg.sender, defaultTokenURI);
    }

    function ownerMint(address to, string calldata uri) external onlyOwner returns (uint256 tokenId) {
        tokenId = _mintToken(to, uri);
    }

    function _mintToken(address to, string memory uri) private returns (uint256 tokenId) {
        tokenId = _nextTokenId++;

        _safeMint(to, tokenId);
        _setTokenURI(tokenId, uri);

        emit Locked(tokenId);
    }

    function locked(uint256 tokenId) external view returns (bool) {
        if (_ownerOf(tokenId) == address(0)) {
            revert TokenDoesNotExist(tokenId);
        }

        return true;
    }

    function _update(address to, uint256 tokenId, address auth)
        internal
        override
        returns (address)
    {
        address from = _ownerOf(tokenId);

        if (from != address(0)) {
            revert SoulboundToken();
        }

        return super._update(to, tokenId, auth);
    }

    function supportsInterface(bytes4 interfaceId)
        public
        view
        override(ERC721URIStorage)
        returns (bool)
    {
        return interfaceId == 0xb45a3c0e || super.supportsInterface(interfaceId);
    }
}
