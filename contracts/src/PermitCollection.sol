// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

contract PermitCollection is ERC721, EIP712 {
    address public immutable issuer;
    bytes32 public constant PERMIT_TYPEHASH =
        keccak256("Permit(address spender,uint256 tokenId,uint256 nonce,uint256 deadline)");

    mapping(uint256 => uint256) private _nonces;

    constructor() ERC721("Collection", "NFT") EIP712("Collection", "1") {
        issuer = msg.sender;
    }

    function mint(address to, uint256 tokenId) external {
        require(msg.sender == issuer, "only issuer");
        _mint(to, tokenId);
    }

    function nonces(uint256 tokenId) external view returns (uint256) {
        _requireOwned(tokenId);
        return _nonces[tokenId];
    }

    function DOMAIN_SEPARATOR() external view returns (bytes32) {
        return _domainSeparatorV4();
    }

    function bumpNonce(uint256 tokenId) external {
        address owner = _requireOwned(tokenId);
        require(msg.sender == issuer || msg.sender == owner, "not authorized");
        _nonces[tokenId]++;
    }

    function permit(
        address spender,
        uint256 tokenId,
        uint256 deadline,
        bytes memory sig
    ) external {
        require(block.timestamp <= deadline, "permit expired");
        address owner = _requireOwned(tokenId);
        bytes32 structHash = keccak256(
            abi.encode(
                PERMIT_TYPEHASH,
                spender,
                tokenId,
                _nonces[tokenId]++,
                deadline
            )
        );
        bytes32 hash = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(hash, sig);
        require(signer == owner, "invalid permit signature");
        _approve(spender, tokenId, address(0), true);
    }

    function _update(address to, uint256 tokenId, address auth) internal virtual override returns (address from) {
        from = super._update(to, tokenId, auth);
        if (from != address(0)) {
            _nonces[tokenId]++;
        }
    }

    function supportsInterface(bytes4 interfaceId) public view virtual override returns (bool) {
        return interfaceId == 0x5604e225 || super.supportsInterface(interfaceId);
    }
}
