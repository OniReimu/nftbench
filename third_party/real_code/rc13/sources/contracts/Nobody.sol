// SPDX-License-Identifier: MIT

/***
 *     __    __   ______   _______    ______   _______   __      __
 *    /  \  /  | /      \ /       \  /      \ /       \ /  \    /  |
 *    $$  \ $$ |/$$$$$$  |$$$$$$$  |/$$$$$$  |$$$$$$$  |$$  \  /$$/
 *    $$$  \$$ |$$ |  $$ |$$ |__$$ |$$ |  $$ |$$ |  $$ | $$  \/$$/
 *    $$$$  $$ |$$ |  $$ |$$    $$ |$$ |  $$ |$$ |  $$ |  $$  $$/
 *    $$ $$ $$ |$$ |  $$ |$$$$$$$  |$$ |  $$ |$$ |  $$ |   $$$$/
 *    $$ |$$$$ |$$ \__$$ |$$ |__$$ |$$ \__$$ |$$ |__$$ |    $$ |
 *    $$ | $$$ |$$    $$/ $$    $$/ $$    $$/ $$    $$/     $$ |
 *    $$/   $$/  $$$$$$/  $$$$$$$/   $$$$$$/  $$$$$$$/      $$/
 */

pragma solidity 0.8.16;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/token/common/ERC2981.sol";
import "@openzeppelin/contracts/token/ERC721/extensions/ERC721Enumerable.sol";

contract Nobody is ERC721Enumerable, ERC2981, Ownable {
    uint256 public constant MAX_SUPPLY = 10000;

    string public PROVENANCE;

    string public baseURI;
    string private _notRevealedURI;

    bool public isRevealed = false;

    error ExceedMaxSupply();

    constructor() ERC721("Nobody", "NOBODY") {}

    function setProvenance(string memory provenance) external onlyOwner {
        PROVENANCE = provenance;
    }

    function setBaseURI(string memory baseURI_) external onlyOwner {
        baseURI = baseURI_;
    }

    function setNotRevealedURI(string memory notRevealedURI_) external onlyOwner {
        _notRevealedURI = notRevealedURI_;
    }

    function setIsRevealed(bool _isRevealed) external onlyOwner {
        isRevealed = _isRevealed;
    }

    function setRoyaltyInfo(address receiver, uint96 feeNumerator) external onlyOwner {
        _setDefaultRoyalty(receiver, feeNumerator);
    }

    function airdrop(address[] calldata addresses, uint256 number) external onlyOwner {
        for (uint256 i = 0; i < addresses.length; i++) {
            uint256 ts = totalSupply();
            if (ts + number > MAX_SUPPLY) revert ExceedMaxSupply();
            for (uint256 j = 0; j < number; j++) {
                _safeMint(addresses[i], ts + j);
            }
        }
    }

    function supportsInterface(bytes4 interfaceId) public view override(ERC721Enumerable, ERC2981) returns (bool) {
        return super.supportsInterface(interfaceId);
    }

    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        _requireMinted(tokenId);

        if (!isRevealed) {
            return _notRevealedURI;
        }

        return bytes(baseURI).length > 0 ? string(abi.encodePacked(baseURI, Strings.toString(tokenId))) : "";
    }
}
