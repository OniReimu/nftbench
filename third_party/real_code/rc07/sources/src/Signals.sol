// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC721URIStorage, ERC721} from "@openzeppelin/contracts/token/ERC721/extensions/ERC721URIStorage.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {IERC5192} from "./interfaces/IERC5192.sol";

/**
 * @title KatchSignals
 * @author Katch Team
 * @notice Soulbound NFT contract for minting user signals and recording likes on-chain
 * @dev ERC-721 + ERC-5192 soulbound contract with role-based access control and pause functionality.
 *      NFTs are non-transferable — users cannot transfer, sell, or trade signals.
 *
 * Roles:
 * - OWNER_ROLE: Cold wallet (Ledger/Safe) - manages admin, pause, ownership transfer
 * - ADMIN_ROLE: Hot wallet (backend) - mint, burn, like, setTokenURI
 *
 * Key features:
 * - Soulbound (ERC-5192): all tokens are permanently locked
 * - Only admin can mint signals to users
 * - On-chain like tracking (one like per user per signal)
 * - Two-step ownership transfer for safety
 * - Pausable: owner can pause admin operations
 */
contract KatchSignals is ERC721URIStorage, AccessControl, Pausable, IERC5192 {
    /*//////////////////////////////////////////////////////////////
                                 ROLES
    //////////////////////////////////////////////////////////////*/

    /// @notice Role identifier for the contract owner (cold wallet)
    bytes32 public constant OWNER_ROLE = keccak256("OWNER_ROLE");
    /// @notice Role identifier for the admin (hot wallet)
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");

    /*//////////////////////////////////////////////////////////////
                                 ERRORS
    //////////////////////////////////////////////////////////////*/

    /// @notice Thrown when setting address to zero
    error InvalidAddress();

    /// @notice Thrown when attempting a disabled transfer
    error TransfersDisabled();

    /// @notice Thrown when attempting a disabled approval
    error ApprovalsDisabled();

    /// @notice Thrown when caller is not the pending owner
    error NotPendingOwner();

    /// @notice Thrown when attempting to use direct AccessControl role management
    error RoleManagementDisabled();

    /// @notice Thrown when array lengths do not match
    error ArrayLengthMismatch();

    /// @notice Thrown when new admin is the same as current admin
    error AdminNotChanged();

    /*//////////////////////////////////////////////////////////////
                                 EVENTS
    //////////////////////////////////////////////////////////////*/

    /// @notice Emitted when a new signal is minted
    event SignalMinted(uint256 indexed signalId, address indexed to, string tokenURI);

    /// @notice Emitted when a signal is burned
    event SignalBurned(uint256 indexed signalId, address indexed owner);

    /// @notice Emitted when a signal is liked
    event SignalLiked(uint256 indexed signalId, address indexed liker);

    /// @notice Emitted when a signal is unliked
    event SignalUnliked(uint256 indexed signalId, address indexed liker);

    /// @notice Emitted when a signal's URI is updated
    event SignalURIUpdated(uint256 indexed signalId, string newURI);

    /// @notice Emitted when admin role is changed
    event AdminChanged(address indexed oldAdmin, address indexed newAdmin);

    /// @notice Emitted when ownership transfer is initiated
    event OwnershipTransferStarted(address indexed currentOwner, address indexed pendingOwner);

    /// @notice Emitted when ownership transfer is completed
    event OwnershipTransferred(address indexed oldOwner, address indexed newOwner);

    /// @notice Emitted when ownership transfer is cancelled
    event OwnershipTransferCancelled(address indexed currentOwner, address indexed cancelledPendingOwner);

    /*//////////////////////////////////////////////////////////////
                                 STORAGE
    //////////////////////////////////////////////////////////////*/

    /// @notice The current owner address
    address private _owner;

    /// @notice The pending owner for two-step ownership transfer
    address public pendingOwner;

    /// @notice The current admin address
    address public admin;

    /// @notice Auto-increment counter for signal IDs (starts at 1)
    uint256 private _nextSignalId;

    /// @notice Tracks whether a user has liked a specific signal
    mapping(uint256 => mapping(address => bool)) private _likes;

    /// @notice Tracks the number of likes per signal
    mapping(uint256 => uint256) private _likesCount;

    /*//////////////////////////////////////////////////////////////
                              CONSTRUCTOR
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Deploys the KatchSignals contract
     * @param owner_ The owner address (cold wallet)
     * @param admin_ The admin address (hot wallet)
     */
    constructor(
        address owner_,
        address admin_
    ) ERC721("[katch] signals", "SIGNAL") {
        if (owner_ == address(0)) revert InvalidAddress();
        if (admin_ == address(0)) revert InvalidAddress();

        _grantRole(OWNER_ROLE, owner_);
        _grantRole(ADMIN_ROLE, admin_);

        _setRoleAdmin(ADMIN_ROLE, OWNER_ROLE);
        _setRoleAdmin(OWNER_ROLE, OWNER_ROLE);

        _owner = owner_;
        admin = admin_;
        _nextSignalId = 1;
    }

    /*//////////////////////////////////////////////////////////////
                         DISABLED ERC721 FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /// @notice Disabled. Signals are soulbound and cannot be transferred.
    function transferFrom(address, address, uint256) public pure override(ERC721, IERC721) {
        revert TransfersDisabled();
    }

    /// @notice Disabled. Signals are soulbound and cannot be transferred.
    function safeTransferFrom(address, address, uint256, bytes memory) public pure override(ERC721, IERC721) {
        revert TransfersDisabled();
    }

    /// @notice Disabled. Approvals are not needed for soulbound tokens.
    function approve(address, uint256) public pure override(ERC721, IERC721) {
        revert ApprovalsDisabled();
    }

    /// @notice Disabled. Approvals are not needed for soulbound tokens.
    function setApprovalForAll(address, bool) public pure override(ERC721, IERC721) {
        revert ApprovalsDisabled();
    }

    /*//////////////////////////////////////////////////////////////
                           ADMIN FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Mints a soulbound signal NFT to a user
     * @param to The recipient address
     * @param uri The token URI (metadata URL)
     */
    function mint(address to, string calldata uri) external onlyRole(ADMIN_ROLE) whenNotPaused {
        uint256 signalId;
        unchecked { signalId = _nextSignalId++; } // overflow unrealistic for NFT counter
        _mint(to, signalId);
        _setTokenURI(signalId, uri);

        emit SignalMinted(signalId, to, uri);
        emit Locked(signalId);
    }

    /**
     * @notice Burns a signal NFT (content moderation)
     * @param signalId The ID of the signal to burn
     */
    function burn(uint256 signalId) external onlyRole(ADMIN_ROLE) whenNotPaused {
        address signalOwner = _requireOwned(signalId);
        _burn(signalId);
        delete _likesCount[signalId];

        emit SignalBurned(signalId, signalOwner);
    }

    /**
     * @notice Toggles a like on a signal (like/unlike)
     * @dev First call = like, second call = unlike, third = like, etc.
     * @param signalId The ID of the signal to toggle like on
     * @param liker The address of the user toggling the like
     */
    function like(uint256 signalId, address liker) external onlyRole(ADMIN_ROLE) whenNotPaused {
        _requireOwned(signalId);
        if (liker == address(0)) revert InvalidAddress();

        if (_likes[signalId][liker]) {
            _likes[signalId][liker] = false;
            unchecked { _likesCount[signalId]--; }

            emit SignalUnliked(signalId, liker);
        } else {
            _likes[signalId][liker] = true;
            unchecked { _likesCount[signalId]++; }

            emit SignalLiked(signalId, liker);
        }
    }

    /**
     * @notice Updates the token URI of an existing signal
     * @param signalId The ID of the signal to update
     * @param newURI The new token URI
     */
    function setSignalURI(uint256 signalId, string calldata newURI) external onlyRole(ADMIN_ROLE) whenNotPaused {
        _requireOwned(signalId);
        _setTokenURI(signalId, newURI);

        emit SignalURIUpdated(signalId, newURI);
    }

    /**
     * @notice Batch updates token URIs for multiple signals
     * @param signalIds Array of signal IDs to update
     * @param newURIs Array of new token URIs (must match signalIds length)
     */
    function setSignalURIBatch(uint256[] calldata signalIds, string[] calldata newURIs) external onlyRole(ADMIN_ROLE) whenNotPaused {
        if (signalIds.length != newURIs.length) revert ArrayLengthMismatch();

        for (uint256 i; i < signalIds.length; ) {
            _requireOwned(signalIds[i]);
            _setTokenURI(signalIds[i], newURIs[i]);
            emit SignalURIUpdated(signalIds[i], newURIs[i]);
            unchecked { i++; }
        }
    }

    /*//////////////////////////////////////////////////////////////
                            OWNER FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /**
     * @notice Changes the admin address
     * @dev Works even when contract is paused.
     * @param newAdmin The new admin address
     */
    function setAdmin(address newAdmin) external onlyRole(OWNER_ROLE) {
        if (newAdmin == address(0)) revert InvalidAddress();

        address oldAdmin = admin;
        if (newAdmin == oldAdmin) revert AdminNotChanged();

        _revokeRole(ADMIN_ROLE, oldAdmin);
        _grantRole(ADMIN_ROLE, newAdmin);

        admin = newAdmin;

        emit AdminChanged(oldAdmin, newAdmin);
    }

    /// @notice Pauses admin operations (mint, burn, like, setSignalURI)
    function pause() external onlyRole(OWNER_ROLE) {
        _pause();
    }

    /// @notice Unpauses admin operations
    function unpause() external onlyRole(OWNER_ROLE) {
        _unpause();
    }

    /**
     * @notice Starts ownership transfer to a new address (step 1 of 2)
     * @param newOwner The address to transfer ownership to
     */
    function transferOwnership(address newOwner) external onlyRole(OWNER_ROLE) {
        if (newOwner == address(0)) revert InvalidAddress();

        pendingOwner = newOwner;

        emit OwnershipTransferStarted(msg.sender, newOwner);
    }

    /**
     * @notice Cancels a pending ownership transfer
     */
    function cancelTransferOwnership() external onlyRole(OWNER_ROLE) {
        address cancelled = pendingOwner;
        pendingOwner = address(0);

        emit OwnershipTransferCancelled(msg.sender, cancelled);
    }

    /**
     * @notice Completes ownership transfer (step 2 of 2)
     * @dev Must be called by the pending owner
     */
    function acceptOwnership() external {
        if (msg.sender != pendingOwner) revert NotPendingOwner();

        address oldOwner = _owner;

        _revokeRole(OWNER_ROLE, oldOwner);
        _grantRole(OWNER_ROLE, msg.sender);

        _owner = msg.sender;
        pendingOwner = address(0);

        emit OwnershipTransferred(oldOwner, msg.sender);
    }

    /*//////////////////////////////////////////////////////////////
                     DISABLED ACCESSCONTROL FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /// @notice Disabled. Use setAdmin() to change the admin role.
    function grantRole(bytes32, address) public pure override {
        revert RoleManagementDisabled();
    }

    /// @notice Disabled. Use setAdmin() to change the admin role.
    function revokeRole(bytes32, address) public pure override {
        revert RoleManagementDisabled();
    }

    /// @notice Disabled. Use setAdmin() or transferOwnership() instead.
    function renounceRole(bytes32, address) public pure override {
        revert RoleManagementDisabled();
    }

    /*//////////////////////////////////////////////////////////////
                           VIEW FUNCTIONS
    //////////////////////////////////////////////////////////////*/

    /// @notice Returns the number of likes for a signal
    function likesCount(uint256 signalId) external view returns (uint256) {
        _requireOwned(signalId);
        return _likesCount[signalId];
    }

    /// @notice Returns whether a user has liked a specific signal
    function hasLiked(uint256 signalId, address user) external view returns (bool) {
        _requireOwned(signalId);
        return _likes[signalId][user];
    }

    /// @notice Returns the current owner address
    function owner() external view returns (address) {
        return _owner;
    }

    /// @notice Checks if an address has the OWNER_ROLE
    function hasRoleOwner(address account) external view returns (bool) {
        return hasRole(OWNER_ROLE, account);
    }

    /// @notice Checks if an address has the ADMIN_ROLE
    function hasRoleAdmin(address account) external view returns (bool) {
        return hasRole(ADMIN_ROLE, account);
    }

    /*//////////////////////////////////////////////////////////////
                         ERC-5192 (SOULBOUND)
    //////////////////////////////////////////////////////////////*/

    /// @notice Returns the locking status of a token. Always true — all signals are soulbound.
    /// @param tokenId The token to check
    /// @return True (always locked)
    function locked(uint256 tokenId) external view returns (bool) {
        _requireOwned(tokenId);
        return true;
    }

    /*//////////////////////////////////////////////////////////////
                        INTERFACE SUPPORT
    //////////////////////////////////////////////////////////////*/

    /// @notice Returns true if the contract supports the given interface
    function supportsInterface(bytes4 interfaceId)
        public
        view
        override(ERC721URIStorage, AccessControl)
        returns (bool)
    {
        return interfaceId == type(IERC5192).interfaceId || super.supportsInterface(interfaceId);
    }
}
