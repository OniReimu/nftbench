// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/interfaces/IERC4906.sol";
import "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import "@openzeppelin/contracts/utils/Strings.sol";
import "@openzeppelin/contracts/utils/introspection/IERC165.sol";

/// @notice Minimal ERC-5192 soulbound interface used by marketplaces and wallets.
interface IERC5192Genesis {
    event Locked(uint256 tokenId);
    event Unlocked(uint256 tokenId);

    function locked(uint256 tokenId) external view returns (bool);
}

/**
 * @title OdeiAgentPassportGenesis
 * @notice Production collection for ODEI Agent Passport public mint.
 * @dev Minting is permissionless, sequential, one passport per wallet, and soulbound.
 *      The contract builds tokenURI from the final tokenId to avoid race-condition metadata.
 */
contract OdeiAgentPassportGenesis is ERC721, IERC5192Genesis, IERC4906, Ownable {
    using Strings for uint256;

    error EmptyBaseURI();
    error InvalidOwnerLabel();
    error PassportAlreadyMinted();
    error SoulboundTransferDisabled();

    event ContractURIUpdated();
    event MetadataBaseURIUpdated(string nextMetadataBaseURI);
    event PassportMinted(address indexed owner, uint256 indexed tokenId, string ownerLabel);

    bytes4 private constant ERC4906_INTERFACE_ID = 0x49064906;
    bytes16 private constant HEX_SYMBOLS = "0123456789ABCDEF";

    uint256 public nextTokenId = 1;
    string public metadataBaseURI;

    string private _collectionMetadataURI;

    mapping(uint256 => string) private _ownerLabels;
    mapping(address => uint256) public tokenOfOwner;

    constructor(
        address initialOwner,
        string memory initialMetadataBaseURI,
        string memory initialContractURI
    ) ERC721("ODEI Agent Passport", "ODEIPASS") Ownable(initialOwner) {
        if (bytes(initialMetadataBaseURI).length == 0) revert EmptyBaseURI();

        metadataBaseURI = initialMetadataBaseURI;
        _collectionMetadataURI = initialContractURI;
    }

    function mintPassport(string calldata ownerLabel) external returns (uint256) {
        if (tokenOfOwner[msg.sender] != 0) revert PassportAlreadyMinted();
        _validateOwnerLabel(ownerLabel);

        uint256 tokenId = nextTokenId;
        nextTokenId = tokenId + 1;
        tokenOfOwner[msg.sender] = tokenId;
        _ownerLabels[tokenId] = ownerLabel;

        _safeMint(msg.sender, tokenId);
        emit Locked(tokenId);
        emit PassportMinted(msg.sender, tokenId, ownerLabel);

        return tokenId;
    }

    function ownerLabelOf(uint256 tokenId) external view returns (string memory) {
        ownerOf(tokenId);
        return _ownerLabels[tokenId];
    }

    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        address owner = ownerOf(tokenId);
        string memory id = tokenId.toString();
        return string.concat(
            metadataBaseURI,
            "owner=owner_bound",
            "&wallet=verified_wallet",
            "&agent=profile_bound",
            "&proof=preview",
            "&token_id=",
            id,
            "&owner_label=",
            _urlEncode(_ownerLabels[tokenId]),
            "&wallet_label=",
            _urlEncode(_shortAddress(owner)),
            "&token_label=%23",
            id,
            "&chain_label=Base",
            "&agent_label=Soulbound",
            "&proof_label=Genesis+proof",
            "&status_label=Genesis+Minted",
            "&animated_logo=1"
        );
    }

    /// @notice OpenSea-compatible collection metadata endpoint.
    function contractURI() external view returns (string memory) {
        return _collectionMetadataURI;
    }

    function setOwnerLabel(uint256 tokenId, string calldata nextOwnerLabel) external {
        address owner = ownerOf(tokenId);
        if (msg.sender != owner && msg.sender != this.owner()) revert OwnableUnauthorizedAccount(msg.sender);
        _validateOwnerLabel(nextOwnerLabel);

        _ownerLabels[tokenId] = nextOwnerLabel;
        emit MetadataUpdate(tokenId);
    }

    function setContractURI(string calldata nextContractURI) external onlyOwner {
        _collectionMetadataURI = nextContractURI;
        emit ContractURIUpdated();
    }

    function setMetadataBaseURI(string calldata nextMetadataBaseURI) external onlyOwner {
        if (bytes(nextMetadataBaseURI).length == 0) revert EmptyBaseURI();
        metadataBaseURI = nextMetadataBaseURI;
        emit MetadataBaseURIUpdated(nextMetadataBaseURI);
        if (nextTokenId > 1) {
            emit BatchMetadataUpdate(1, nextTokenId - 1);
        }
    }

    function locked(uint256 tokenId) external view override returns (bool) {
        ownerOf(tokenId);
        return true;
    }

    function supportsInterface(bytes4 interfaceId) public view override(ERC721, IERC165) returns (bool) {
        return
            interfaceId == type(IERC5192Genesis).interfaceId ||
            interfaceId == ERC4906_INTERFACE_ID ||
            super.supportsInterface(interfaceId);
    }

    function _update(address to, uint256 tokenId, address auth) internal override returns (address) {
        address from = _ownerOf(tokenId);
        if (from != address(0) && to != address(0)) {
            revert SoulboundTransferDisabled();
        }
        return super._update(to, tokenId, auth);
    }

    function _validateOwnerLabel(string calldata ownerLabel) private pure {
        bytes calldata value = bytes(ownerLabel);
        if (value.length == 0 || value.length > 64) revert InvalidOwnerLabel();
    }

    function _shortAddress(address account) private pure returns (string memory) {
        bytes memory full = bytes(Strings.toHexString(uint160(account), 20));
        bytes memory shortValue = new bytes(13);
        for (uint256 index = 0; index < 6; index += 1) shortValue[index] = full[index];
        shortValue[6] = ".";
        shortValue[7] = ".";
        shortValue[8] = ".";
        for (uint256 index = 0; index < 4; index += 1) shortValue[9 + index] = full[38 + index];
        return string(shortValue);
    }

    function _urlEncode(string memory value) private pure returns (string memory) {
        bytes memory raw = bytes(value);
        bytes memory encoded = new bytes(raw.length * 3);
        uint256 length = 0;

        for (uint256 index = 0; index < raw.length; index += 1) {
            uint8 char = uint8(raw[index]);
            if (
                (char >= 0x30 && char <= 0x39) ||
                (char >= 0x41 && char <= 0x5A) ||
                (char >= 0x61 && char <= 0x7A) ||
                char == 0x2D ||
                char == 0x2E ||
                char == 0x5F ||
                char == 0x7E
            ) {
                encoded[length] = raw[index];
                length += 1;
            } else if (char == 0x20) {
                encoded[length] = "+";
                length += 1;
            } else {
                encoded[length] = "%";
                encoded[length + 1] = HEX_SYMBOLS[char >> 4];
                encoded[length + 2] = HEX_SYMBOLS[char & 0x0f];
                length += 3;
            }
        }

        bytes memory result = new bytes(length);
        for (uint256 index = 0; index < length; index += 1) {
            result[index] = encoded[index];
        }
        return string(result);
    }
}
