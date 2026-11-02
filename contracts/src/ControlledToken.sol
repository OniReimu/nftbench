// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract ControlledToken is ERC20 {
    address public immutable issuer;
    bool private _paused;
    mapping(address => bool) private _blocked;

    constructor(uint256 supply) ERC20("USD Coin", "USDC") {
        issuer = msg.sender;
        _mint(msg.sender, supply);
    }

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    function paused() external view returns (bool) {
        return _paused;
    }

    function blocked(address account) external view returns (bool) {
        return _blocked[account];
    }

    function setPaused(bool value) external {
        require(msg.sender == issuer, "only issuer");
        _paused = value;
    }

    function setBlocked(address account, bool value) external {
        require(msg.sender == issuer, "only issuer");
        _blocked[account] = value;
    }

    function _update(address from, address to, uint256 value) internal override {
        require(!_paused, "token is paused");
        require(!_blocked[from] && !_blocked[to], "account is blocked");
        super._update(from, to, value);
    }
}
