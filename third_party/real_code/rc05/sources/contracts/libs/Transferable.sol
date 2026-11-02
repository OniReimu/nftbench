// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {AccountManager} from "./AccountManager.sol";
import {ITransferable} from "./interfaces/ITransferable.sol";

// @author: miinded.com

abstract contract Transferable is ITransferable, AccountManager {

    uint16 constant public BACKEND_MANAGER = 1;

    mapping(uint256 => bool) public transferable;

    // ERC5192
    function lock(uint256 tokenId) public override isRole(BACKEND_MANAGER) {
        transferable[tokenId] = false;
        emit Locked(tokenId);
    }

    function unlock(uint256 tokenId) public override isRole(BACKEND_MANAGER) {
        transferable[tokenId] = true;
        emit Unlocked(tokenId);
    }

    function lockBatch(uint256[] calldata tokenIds) public override isRole(BACKEND_MANAGER) {
        for(uint256 i = 0; i < tokenIds.length; i++){
            lock(tokenIds[i]);
        }
    }
    function unlockBatch(uint256[] calldata tokenIds) public override isRole(BACKEND_MANAGER) {
        for(uint256 i = 0; i < tokenIds.length; i++){
            unlock(tokenIds[i]);
        }
    }
    function updateLockBatch(UpdateLock[] calldata _lockBatch) public override isRole(BACKEND_MANAGER) {
        for(uint256 i = 0; i < _lockBatch.length; i++){
            if(_lockBatch[i].transferable){
                unlock(_lockBatch[i].tokenId);
            }else{
                lock(_lockBatch[i].tokenId);
            }
        }
    }

    function locked(uint256 _id) public view returns (bool){
        return transferable[_id] == false;
    }

}
