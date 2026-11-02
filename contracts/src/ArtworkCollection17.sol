// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {IERC4906} from "@openzeppelin/contracts/interfaces/IERC4906.sol";
import {IERC165} from "@openzeppelin/contracts/utils/introspection/IERC165.sol";

contract ArtworkCollection17 is ERC721, IERC4906 {
    address public immutable issuer;
    mapping(uint256 => string) private _uris;
    mapping(uint256 => bytes32) public artworkCommitment;

    constructor() ERC721("Collection", "NFT") { issuer = msg.sender; }

    function mint(address to, uint256 tokenId) external {
        require(msg.sender == issuer, "only issuer");
        _mint(to, tokenId);
    }

    function setMetadata(uint256 tokenId, string calldata uri, bytes32 commitment) external {
        require(msg.sender == issuer, "only issuer");
        _requireOwned(tokenId);
        _uris[tokenId] = uri;
        artworkCommitment[tokenId] = commitment;
        emit MetadataUpdate(tokenId);
    }

    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        _requireOwned(tokenId);
        return _uris[tokenId];
    }

    function supportsInterface(bytes4 interfaceId) public view override(ERC721, IERC165) returns (bool) {
        return interfaceId == 0x49064906 || super.supportsInterface(interfaceId);
    }
}
