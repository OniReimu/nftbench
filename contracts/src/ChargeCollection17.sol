// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Base64} from "@openzeppelin/contracts/utils/Base64.sol";

// Dynamic charge trait and collection-specific maintenance operations.
contract ChargeCollection17 is ERC721 {
    error TraitDoesNotExist(bytes32 traitKey);
    event TraitUpdated(bytes32 indexed traitKey, uint256 tokenId, bytes32 traitValue);
    event TraitUpdatedRange(bytes32 indexed traitKey, uint256 fromTokenId, uint256 toTokenId);
    event TraitUpdatedRangeUniformValue(bytes32 indexed traitKey, uint256 fromTokenId, uint256 toTokenId, bytes32 traitValue);
    event TraitUpdatedList(bytes32 indexed traitKey, uint256[] tokenIds);
    event TraitUpdatedListUniformValue(bytes32 indexed traitKey, uint256[] tokenIds, bytes32 traitValue);
    event TraitMetadataURIUpdated();

    bytes32 public constant CHARGE_KEY = keccak256("charge");
    address public immutable issuer;
    IERC20 public immutable paymentToken;
    uint256 public constant rechargeCost = 5_000_000;
    bool public rechargeEnabled = true;
    mapping(uint256 => uint256) private _charges;

    constructor(address paymentToken_) ERC721("Collection", "NFT") {
        issuer = msg.sender;
        paymentToken = IERC20(paymentToken_);
    }

    function mint(address to, uint256 tokenId) external {
        require(msg.sender == issuer, "only issuer");
        _mint(to, tokenId);
        _charges[tokenId] = 1;
        emit TraitUpdated(CHARGE_KEY, tokenId, bytes32(uint256(1)));
    }

    function getTraitValue(uint256 tokenId, bytes32 traitKey) public view returns (bytes32) {
        _requireOwned(tokenId);
        if (traitKey != CHARGE_KEY) revert TraitDoesNotExist(traitKey);
        return bytes32(_charges[tokenId]);
    }

    function getTraitValues(uint256 tokenId, bytes32[] calldata traitKeys) external view returns (bytes32[] memory values) {
        _requireOwned(tokenId);
        values = new bytes32[](traitKeys.length);
        for (uint256 i; i < traitKeys.length; ++i) values[i] = getTraitValue(tokenId, traitKeys[i]);
    }

    function getTraitMetadataURI() external pure returns (string memory) {
        return string.concat("data:application/json;base64,", Base64.encode(bytes(
            '{"traits":{"charge":{"displayName":"Charge","dataType":{"type":"decimal","signed":false,"decimals":0},"tokenOwnerCanUpdateValue":false,"validateOnSale":"none"}}}'
        )));
    }

    function setTrait(uint256 tokenId, bytes32 traitKey, bytes32 newValue) external {
        require(msg.sender == issuer, "only issuer");
        _requireOwned(tokenId);
        if (traitKey != CHARGE_KEY) revert TraitDoesNotExist(traitKey);
        _charges[tokenId] = uint256(newValue);
        emit TraitUpdated(traitKey, tokenId, newValue);
    }

    function consumeCharge(uint256 tokenId) external {
        require(msg.sender == ownerOf(tokenId), "only owner");
        require(_charges[tokenId] > 0, "no charge");
        --_charges[tokenId];
        emit TraitUpdated(CHARGE_KEY, tokenId, bytes32(_charges[tokenId]));
    }

    function setRechargeEnabled(bool enabled) external {
        require(msg.sender == issuer, "only issuer");
        rechargeEnabled = enabled;
    }

    function recharge(uint256 tokenId) external {
        _requireOwned(tokenId);
        require(rechargeEnabled, "recharge unavailable");
        require(paymentToken.transferFrom(msg.sender, issuer, rechargeCost), "payment failed");
        ++_charges[tokenId];
        emit TraitUpdated(CHARGE_KEY, tokenId, bytes32(_charges[tokenId]));
    }

    function supportsInterface(bytes4 interfaceId) public view override returns (bool) {
        return interfaceId == 0xaf332f3e || super.supportsInterface(interfaceId);
    }
}
