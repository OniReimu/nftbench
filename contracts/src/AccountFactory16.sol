// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract AccountFactory16 {
    event AccountCreated(address indexed account, uint256 chainId, address tokenContract, uint256 tokenId);

    function createAccount(address implementation, bytes32 salt, uint256 chainId, address tokenContract, uint256 tokenId)
        external returns (address account)
    {
        bytes memory code = abi.encodePacked(
            hex"3d60ad80600a3d3981f3",
            hex"363d3d373d3d3d363d73", implementation, hex"5af43d82803e903d91602b57fd5bf3",
            abi.encode(salt, chainId, tokenContract, tokenId)
        );
        assembly {
            account := create(0, add(code, 32), mload(code))
        }
        require(account != address(0), "account creation failed");
        emit AccountCreated(account, chainId, tokenContract, tokenId);
    }
}
