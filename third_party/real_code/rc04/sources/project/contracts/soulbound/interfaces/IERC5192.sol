// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

/**
 * @title IERC5192 — Minimal Soulbound Token Interface
 * @notice EIP-5192: Minimal Soulbound NFTs
 * @dev Standard interface for soulbound (non-transferable) ERC-721 tokens.
 *      Adds a locked() status function and Locked/Unlocked events.
 *      Interface ID: 0xb45a3c0e
 *
 * Reference: https://eips.ethereum.org/EIPS/eip-5192
 * Status: Final
 * Base: ERC-721
 * Use cases: Memberships, POAPs, event badges, identity tokens
 */
interface IERC5192 {
    /// @notice Emitted when the locking status is changed to locked.
    /// @dev If a token is minted and the status is locked, this event should be
    ///      emitted. Emitted when the locking status is changed to locked.
    /// @param tokenId The identifier for a token.
    event Locked(uint256 tokenId);

    /// @notice Emitted when the locking status is changed to unlocked.
    /// @dev If a token is minted and the status is not locked, this event
    ///      should be emitted. Emitted when the locking status is changed to unlocked.
    /// @param tokenId The identifier for a token.
    event Unlocked(uint256 tokenId);

    /// @notice Returns the locking status of an Soulbound Token.
    /// @dev SBTs assigned to zero address are considered invalid, and queries
    ///      about them do throw.
    /// @param tokenId The identifier for an SBT.
    function locked(uint256 tokenId) external view returns (bool);
}
