// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {AccountManager} from "./AccountManager.sol";
import {Transferable} from "./Transferable.sol";
import {IRunnerLeagueCollection} from "./interfaces/IRunnerLeagueCollection.sol";
import {BasicRoyalties} from "@limitbreak/creator-token-contracts/contracts/programmable-royalties/BasicRoyalties.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IERC2981} from "@openzeppelin/contracts/interfaces/IERC2981.sol";
import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {ERC2981} from "@openzeppelin/contracts/token/common/ERC2981.sol";
import {IERC165} from "@openzeppelin/contracts/utils/introspection/IERC165.sol";

// @author: miinded.com
abstract contract RunnerLeagueCollection is IRunnerLeagueCollection, ERC721, Transferable, BasicRoyalties {

    uint16 constant public MINT_MANAGER = 2;
    string public baseTokenURI;

    error BadParamsLength();
    error NotTransferable();
    error InvalidFrom(address,address);

    constructor(string memory _name, string memory _symbol, string memory _uri, address _royaltyReceiver, uint96 _royaltyFeeNumerator)
    Ownable(_msgSender())
    ERC721(_name, _symbol)
    BasicRoyalties(_royaltyReceiver, _royaltyFeeNumerator){
        baseTokenURI = _uri;
    }

    function execute(Action calldata _action) public virtual isRoleStrict(MINT_MANAGER) {
        if (_action.identifiers.length != _action.balances.length || _action.identifiers.length != _action.transferable.length) {
            revert BadParamsLength();
        }

        for (uint256 i = 0; i < _action.identifiers.length; i++) {
            address from = _update(_action.to, _action.identifiers[i], address(0));
            if(from != _action.from){
                revert InvalidFrom(_action.from, from);
            }
            if (_action.transferable[i]) {
                unlock(_action.identifiers[i]);
            } else {
                lock(_action.identifiers[i]);
            }
        }
    }


    function supportsInterface(bytes4 interfaceId) public view virtual override(ERC721, ERC2981, IERC165) returns (bool) {
        return interfaceId == type(IERC2981).interfaceId || super.supportsInterface(interfaceId);
    }

    function _baseURI() internal view virtual override returns (string memory) {
        return baseTokenURI;
    }

    function setBaseUri(string memory baseURI) public virtual isRoleStrict(GLOBAL_MANAGER) {
        baseTokenURI = baseURI;
    }

    function setDefaultRoyalty(address receiver, uint96 feeNumerator) public virtual isRoleStrict(GLOBAL_MANAGER) {
        _setDefaultRoyalty(receiver, feeNumerator);
    }

    function transferFrom(address from, address to, uint256 tokenId) public override(ERC721, IERC721) {
        if (locked(tokenId)) {
            revert NotTransferable();
        }
        super.transferFrom(from, to, tokenId);
    }

    function safeTransferFrom(address from, address to, uint256 tokenId, bytes memory data) public override(ERC721, IERC721) {
        if (locked(tokenId)) {
            revert NotTransferable();
        }
        super.safeTransferFrom(from, to, tokenId, data);
    }
}
