// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "@openzeppelin/contracts/access/Ownable.sol";

abstract contract AccountManager is Ownable {

    error SenderNotManager(address, uint16);

    uint16 constant public GLOBAL_MANAGER = 98;
    uint16 constant public ADMIN_MANAGER = 99;

    mapping(address => uint16) public accounts;

    modifier isRole(uint16 _role){
        if (accounts[_msgSender()] < _role && _msgSender() != owner()) {
            revert SenderNotManager(_msgSender(), accounts[_msgSender()]);
        }
        _;
    }
    modifier isRoleStrict(uint16 _role){
        if (accounts[_msgSender()] != _role) {
            revert SenderNotManager(_msgSender(), accounts[_msgSender()]);
        }
        _;
    }

    function setAccountManager(address _account, uint16 _role) public isRole(ADMIN_MANAGER) {
        _setAccountManager(_account, _role);
    }

    function _setAccountManager(address _account, uint16 _role) internal {
        accounts[_account] = _role;
    }
}
