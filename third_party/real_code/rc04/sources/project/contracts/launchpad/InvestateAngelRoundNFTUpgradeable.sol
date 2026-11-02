// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {ERC721Upgradeable} from "@openzeppelin/contracts-upgradeable/token/ERC721/ERC721Upgradeable.sol";
import {ERC721EnumerableUpgradeable} from "@openzeppelin/contracts-upgradeable/token/ERC721/extensions/ERC721EnumerableUpgradeable.sol";
import {ERC721URIStorageUpgradeable} from "@openzeppelin/contracts-upgradeable/token/ERC721/extensions/ERC721URIStorageUpgradeable.sol";
import {ERC2981Upgradeable} from "@openzeppelin/contracts-upgradeable/token/common/ERC2981Upgradeable.sol";
import {Ownable2StepUpgradeable} from "@openzeppelin/contracts-upgradeable/access/Ownable2StepUpgradeable.sol";
import {AccessControlUpgradeable} from "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import {PausableUpgradeable} from "@openzeppelin/contracts-upgradeable/utils/PausableUpgradeable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {Strings} from "@openzeppelin/contracts/utils/Strings.sol";
import {IERC5192} from "../soulbound/interfaces/IERC5192.sol";

/**
 * @title InvestateAngelRoundNFTUpgradeable
 * @notice UUPS Upgradeable Soulbound NFT representing an allocation in the Angel Round.
 * @dev Only the Launchpad Controller can mint these NFTs. Inherits ERC-5192 soulbound logic.
 */
contract InvestateAngelRoundNFTUpgradeable is
    Initializable,
    ERC721Upgradeable,
    ERC721EnumerableUpgradeable,
    ERC721URIStorageUpgradeable,
    ERC2981Upgradeable,
    Ownable2StepUpgradeable,
    AccessControlUpgradeable,
    PausableUpgradeable,
    UUPSUpgradeable,
    IERC5192
{
    using Strings for uint256;

    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");

    string private _baseTokenURI;
    string private _contractURI;
    uint256 public maxSupply;
    uint256 private _nextTokenId;

    /// @notice Mapping from tokenId => locked status (ERC-5192)
    mapping(uint256 => bool) private _locked;

    /// @notice Per-token custom URI overrides
    mapping(uint256 => string) private _tokenURIsCustom;

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    /**
     * @notice Initializes the Angel Round NFT Tier UUPS Proxy.
     */
    function initialize(
        string memory name_,
        string memory symbol_,
        string memory baseURI_,
        string memory contractURI_,
        uint256 maxSupply_,
        address royaltyReceiver_,
        uint96 royaltyBps_,
        address initialOwner_,
        address controller_
    ) external initializer {
        __ERC721_init(name_, symbol_);
        __ERC721Enumerable_init();
        __ERC721URIStorage_init();
        __ERC2981_init();
        __Ownable_init(initialOwner_);
        __AccessControl_init();
        __Pausable_init();

        _baseTokenURI = baseURI_;
        _contractURI = contractURI_;
        maxSupply = maxSupply_;
        _nextTokenId = 1;

        if (royaltyReceiver_ != address(0) && royaltyBps_ > 0) {
            _setDefaultRoyalty(royaltyReceiver_, royaltyBps_);
        }

        _grantRole(DEFAULT_ADMIN_ROLE, initialOwner_);
        if (controller_ != address(0)) {
            _grantRole(MINTER_ROLE, controller_);
        }
    }

    function _authorizeUpgrade(address newImplementation) internal override onlyOwner {}

    // ─── ERC-5192 Soulbound Logic ─────────────────────────────────────────────

    function locked(uint256 tokenId) external view override returns (bool) {
        _requireOwned(tokenId);
        return _locked[tokenId];
    }

    function setTokenLocked(uint256 tokenId, bool isLocked) external onlyOwner {
        _requireOwned(tokenId);
        _locked[tokenId] = isLocked;
        if (isLocked) {
            emit Locked(tokenId);
        } else {
            emit Unlocked(tokenId);
        }
    }

    // ─── Minting Logic ────────────────────────────────────────────────────────

    function mint(address to) public returns (uint256) {
        if (owner() != _msgSender() && !hasRole(MINTER_ROLE, _msgSender())) {
            revert("Unauthorized: not owner or minter");
        }
        if (maxSupply > 0) {
            require(_nextTokenId <= maxSupply, "Max supply reached");
        }

        uint256 tokenId = _nextTokenId;
        _nextTokenId++;

        _safeMint(to, tokenId);
        _locked[tokenId] = true;
        emit Locked(tokenId);

        return tokenId;
    }

    function batchMint(address[] calldata recipients) external returns (uint256[] memory) {
        if (owner() != _msgSender() && !hasRole(MINTER_ROLE, _msgSender())) {
            revert("Unauthorized: not owner or minter");
        }

        uint256 count = recipients.length;
        if (maxSupply > 0) {
            require(_nextTokenId + count - 1 <= maxSupply, "Exceeds max supply");
        }

        uint256[] memory tokenIds = new uint256[](count);
        for (uint256 i = 0; i < count; i++) {
            uint256 tokenId = _nextTokenId;
            _nextTokenId++;

            _safeMint(recipients[i], tokenId);
            _locked[tokenId] = true;
            emit Locked(tokenId);

            tokenIds[i] = tokenId;
        }

        return tokenIds;
    }

    // ─── Admin Configuration ──────────────────────────────────────────────────

    function setBaseURI(string calldata newBaseURI) external onlyOwner {
        _baseTokenURI = newBaseURI;
    }

    function setContractURI(string calldata newContractURI) external onlyOwner {
        _contractURI = newContractURI;
    }

    function contractURI() external view returns (string memory) {
        return _contractURI;
    }

    function tokenURI(uint256 tokenId)
        public
        view
        override(ERC721Upgradeable, ERC721URIStorageUpgradeable)
        returns (string memory)
    {
        _requireOwned(tokenId);

        string memory custom = _tokenURIsCustom[tokenId];
        if (bytes(custom).length > 0) {
            return custom;
        }

        string memory base = _baseTokenURI;
        return bytes(base).length > 0 ? string.concat(base, tokenId.toString(), ".json") : "";
    }

    function setDefaultRoyalty(address receiver, uint96 feeNumerator) external onlyOwner {
        _setDefaultRoyalty(receiver, feeNumerator);
    }

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    // ─── Internal Overrides ───────────────────────────────────────────────────

    function _update(address to, uint256 tokenId, address auth)
        internal
        override(ERC721Upgradeable, ERC721EnumerableUpgradeable)
        whenNotPaused
        returns (address)
    {
        address from = _ownerOf(tokenId);

        if (from != address(0) && to != address(0)) {
            require(!_locked[tokenId], "Token is locked (soulbound)");
        }

        return super._update(to, tokenId, auth);
    }

    function _increaseBalance(address account, uint128 value)
        internal
        override(ERC721Upgradeable, ERC721EnumerableUpgradeable)
    {
        super._increaseBalance(account, value);
    }

    function supportsInterface(bytes4 interfaceId)
        public
        view
        override(
            ERC721Upgradeable,
            ERC721EnumerableUpgradeable,
            ERC721URIStorageUpgradeable,
            ERC2981Upgradeable,
            AccessControlUpgradeable
        )
        returns (bool)
    {
        return interfaceId == type(IERC5192).interfaceId || super.supportsInterface(interfaceId);
    }
}
