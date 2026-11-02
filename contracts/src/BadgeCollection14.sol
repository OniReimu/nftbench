// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {IERC5192} from "./interfaces/IERC5192.sol";

contract BadgeCollection14 is ERC721, IERC5192 {
    error TraitDoesNotExist(bytes32 traitKey);
    event TraitUpdated(bytes32 indexed traitKey, uint256 tokenId, bytes32 traitValue);
    event TraitUpdatedRange(bytes32 indexed traitKey, uint256 fromTokenId, uint256 toTokenId);
    event TraitUpdatedRangeUniformValue(
        bytes32 indexed traitKey, uint256 fromTokenId, uint256 toTokenId, bytes32 traitValue
    );
    event TraitUpdatedList(bytes32 indexed traitKey, uint256[] tokenIds);
    event TraitUpdatedListUniformValue(bytes32 indexed traitKey, uint256[] tokenIds, bytes32 traitValue);
    event TraitMetadataURIUpdated();

    address public immutable issuer;
    bytes32 public constant BOUND_KEY = keccak256("bound");
    bytes32 public constant XP_KEY = keccak256("xp");
    mapping(uint256 => bool) public permanent;
    mapping(uint256 => bool) private _bound;
    mapping(uint256 => uint256) private _xp;

    constructor() ERC721("Badge Collection", "BADGE") {
        issuer = msg.sender;
    }

    function mint(address to, uint256 id, bool bound, bool permanent_) external {
        require(msg.sender == issuer, "only issuer");
        require(!permanent_ || bound, "bound required");
        _mint(to, id);
        _bound[id] = bound;
        permanent[id] = permanent_;
        _xp[id] = 50;
        if (bound) emit Locked(id);
        else emit Unlocked(id);
        emit TraitUpdated(BOUND_KEY, id, bytes32(uint256(bound ? 1 : 0)));
        emit TraitUpdated(XP_KEY, id, bytes32(uint256(50)));
    }

    function locked(uint256 id) public view returns (bool) {
        _requireOwned(id);
        return _bound[id];
    }

    function getTraitValue(uint256 id, bytes32 key) public view returns (bytes32) {
        _requireOwned(id);
        if (key == BOUND_KEY) return bytes32(uint256(_bound[id] ? 1 : 0));
        if (key == XP_KEY) return bytes32(_xp[id]);
        revert TraitDoesNotExist(key);
    }

    function getTraitValues(uint256 id, bytes32[] calldata keys) external view returns (bytes32[] memory values) {
        values = new bytes32[](keys.length);
        for (uint256 i; i < keys.length; ++i) {
            values[i] = getTraitValue(id, keys[i]);
        }
        _requireOwned(id);
    }

    function getTraitMetadataURI() external pure returns (string memory) {
        return 'data:application/json,{"traits":{"bound":{"displayName":"Bound","dataType":{"type":"decimal","signed":false,"decimals":0},"tokenOwnerCanUpdateValue":true},"xp":{"displayName":"XP","dataType":{"type":"decimal","signed":false,"decimals":0}}}}';
    }

    function setTrait(uint256 id, bytes32 key, bytes32 value) external {
        require(msg.sender == _requireOwned(id), "only owner");
        if (key != BOUND_KEY && key != XP_KEY) revert TraitDoesNotExist(key);
        require(key == BOUND_KEY, "trait is fixed");
        require(!permanent[id], "binding is fixed");
        require(uint256(value) <= 1, "bound value");
        bool next = uint256(value) == 1;
        if (_bound[id] && !next) {
            _xp[id] = 0;
            emit TraitUpdated(XP_KEY, id, bytes32(0));
        }
        _bound[id] = next;
        emit TraitUpdated(key, id, value);
        if (next) emit Locked(id);
        else emit Unlocked(id);
    }

    function supportsInterface(bytes4 interfaceId) public view override returns (bool) {
        return
            interfaceId == 0xaf332f3e || interfaceId == type(IERC5192).interfaceId
                || super.supportsInterface(interfaceId);
    }

    function _update(address to, uint256 id, address auth) internal override returns (address from) {
        from = super._update(to, id, auth);
        if (from != address(0)) require(!_bound[id], "token is locked");
    }
}
