// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import {IERC5192} from "./IERC5192.sol";

interface ITransferable is IERC5192 {

    struct UpdateLock {
        uint256 tokenId;
        bool transferable;
    }

    function lock(uint256 tokenId) external;
    function unlock(uint256 tokenId) external;
    function lockBatch(uint256[] calldata tokenIds) external;
    function unlockBatch(uint256[] calldata tokenIds) external;
    function updateLockBatch(UpdateLock[] calldata tokenIds) external;
}
