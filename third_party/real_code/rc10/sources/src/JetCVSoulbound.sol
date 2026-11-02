// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {ERC721URIStorage} from "@openzeppelin/contracts/token/ERC721/extensions/ERC721URIStorage.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

/// @dev ERC-5192 (Minimal Soulbound NFTs). InterfaceId: 0xb45a3c0e.
interface IERC5192 {
    /// @notice Emitted when the locking status is set to locked.
    event Locked(uint256 tokenId);
    /// @notice Emitted when the locking status is set to unlocked.
    event Unlocked(uint256 tokenId);
    /// @notice Returns the locking status of a token. Reverts if the token does not exist.
    function locked(uint256 tokenId) external view returns (bool);
}

/**
 * @title  JetCVSoulbound
 * @notice ERC-721 soulbound che notarizza i CV JetCV su Polygon.
 *
 *         - Un token per address (il wallet dell'utente), uno per CV.
 *         - Il seriale del CV (es. "AB12C-DE34F") è pubblico e unico on-chain.
 *         - Il tokenURI punta al manifest IPFS (`ipfs://<cid>`) che aggrega il
 *           documento CV (solo hash di PII) e le certificazioni accettate.
 *         - Ogni aggiornamento del manifest emette TokenURIUpdated + MetadataUpdate
 *           (ERC-4906): la storia degli eventi è la timeline di notarizzazione.
 *         - Soulbound (ERC-5192, sempre locked): transfer, approve e burn sono
 *           bloccati. Nessuna funzione di burn. Non-upgradeable by design.
 *         - "Recoverable SBT": l'unico movimento possibile è migrate(), eseguita
 *           dal minter con firma EIP-712 di ACCETTAZIONE del wallet di
 *           destinazione. Copre portabilità (wallet custodial -> personale) e
 *           recovery da chiave persa; il token resta invendibile (l'owner non
 *           può mai trasferire in autonomia).
 *
 *         Ruoli:
 *         - DEFAULT_ADMIN_ROLE: cold key offline; gestisce solo grant/revoke ruoli.
 *         - MINTER_ROLE: il chain service; può mint, setTokenURI e migrate.
 */
contract JetCVSoulbound is ERC721URIStorage, AccessControl, EIP712, IERC5192 {
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");

    /// @dev Id sequenziali a partire da 1 (0 = inesistente in _tokenBySerial).
    uint256 private _nextTokenId;

    /// @dev keccak256(seriale CV) → tokenId. Garantisce unicità del seriale on-chain.
    mapping(bytes32 serialHash => uint256 tokenId) private _tokenBySerial;

    /// @dev tokenId → seriale CV in chiaro.
    mapping(uint256 tokenId => string serial) private _serialByToken;

    /// @dev Typed data della firma di accettazione richiesta da migrate().
    bytes32 public constant MIGRATE_TYPEHASH =
        keccak256("Migrate(uint256 tokenId,address from,address to,uint256 nonce,uint256 deadline)");

    /// @dev Anti-replay per le firme di migrazione, per token.
    mapping(uint256 tokenId => uint256 nonce) public migrationNonce;

    /// @dev Sblocco puntuale del vincolo soulbound durante migrate() (0 = nessuna
    ///      migrazione in corso; gli id partono da 1).
    uint256 private _migrating;

    event CVMinted(uint256 indexed tokenId, address indexed to, string serial, string uri);
    event TokenURIUpdated(uint256 indexed tokenId, string uri);
    event Migrated(uint256 indexed tokenId, address indexed from, address indexed to);

    error Soulbound();
    error AlreadyHasToken(address owner);
    error SerialAlreadyMinted(string serial);
    error EmptySerial();
    error EmptyURI();
    error MigrationExpired(uint256 deadline);
    error InvalidMigrationTarget(address to);
    error InvalidMigrationSignature();

    constructor(address admin, address minter) ERC721("JetCV", "JCV") EIP712("JetCV", "1") {
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(MINTER_ROLE, minter);
    }

    // ------------------------------------------------------------------ mint

    /**
     * @notice Minta il JetCV di un utente sul suo wallet.
     * @param to     address del wallet utente (receive-only, un solo token).
     * @param serial seriale del CV in chiaro, unico on-chain.
     * @param uri    `ipfs://<manifestCid>` del manifest iniziale.
     */
    function mint(address to, string calldata serial, string calldata uri)
        external
        onlyRole(MINTER_ROLE)
        returns (uint256 tokenId)
    {
        if (bytes(serial).length == 0) revert EmptySerial();
        if (bytes(uri).length == 0) revert EmptyURI();
        if (balanceOf(to) != 0) revert AlreadyHasToken(to);
        bytes32 serialHash = keccak256(bytes(serial));
        if (_tokenBySerial[serialHash] != 0) revert SerialAlreadyMinted(serial);

        tokenId = ++_nextTokenId;
        _tokenBySerial[serialHash] = tokenId;
        _serialByToken[tokenId] = serial;
        // _mint (non _safeMint): i destinatari sono sempre EOA generati dal
        // chain service; il callback ERC721Receiver non serve e _safeMint
        // aprirebbe una superficie di reentrancy inutile.
        _mint(to, tokenId);
        _setTokenURI(tokenId, uri); // emette MetadataUpdate (ERC-4906)

        emit CVMinted(tokenId, to, serial, uri);
        emit Locked(tokenId);
    }

    /**
     * @notice Aggiorna il manifest di un token esistente (nuovo CID IPFS).
     */
    function setTokenURI(uint256 tokenId, string calldata uri) external onlyRole(MINTER_ROLE) {
        if (bytes(uri).length == 0) revert EmptyURI();
        _requireOwned(tokenId);
        _setTokenURI(tokenId, uri); // emette MetadataUpdate (ERC-4906)
        emit TokenURIUpdated(tokenId, uri);
    }

    // --------------------------------------------------------------- migrate

    /**
     * @notice Migrazione consensuale del token verso un nuovo wallet dello
     *         stesso titolare (portabilità custodial->personale o recovery da
     *         chiave persa). Unico movimento possibile dopo il mint.
     *
     *         Il consenso del titolare è verificato off-chain da JetCV (la
     *         mappa utente<->CV è off-chain per costruzione); on-chain la firma
     *         EIP-712 di `newOwner` prova che il titolare controlla la
     *         destinazione e accetta il token: il minter non può spostare
     *         token verso address arbitrari.
     *
     * @param tokenId          token da migrare.
     * @param newOwner         wallet di destinazione (deve firmare).
     * @param deadline         scadenza della firma (epoch seconds).
     * @param acceptSignature  firma EIP-712 di newOwner su
     *                         Migrate(tokenId, from, to, nonce, deadline).
     */
    function migrate(uint256 tokenId, address newOwner, uint256 deadline, bytes calldata acceptSignature)
        external
        onlyRole(MINTER_ROLE)
    {
        if (block.timestamp > deadline) revert MigrationExpired(deadline);
        address from = _requireOwned(tokenId);
        if (newOwner == address(0) || newOwner == from) revert InvalidMigrationTarget(newOwner);
        if (balanceOf(newOwner) != 0) revert AlreadyHasToken(newOwner);

        bytes32 digest = _hashTypedDataV4(
            keccak256(abi.encode(MIGRATE_TYPEHASH, tokenId, from, newOwner, migrationNonce[tokenId], deadline))
        );
        if (ECDSA.recover(digest, acceptSignature) != newOwner) revert InvalidMigrationSignature();

        migrationNonce[tokenId]++;
        _migrating = tokenId;
        _update(newOwner, tokenId, address(0));
        _migrating = 0;

        emit Migrated(tokenId, from, newOwner);
        emit Locked(tokenId); // resta soulbound sul nuovo owner (ERC-5192)
    }

    /// @notice Digest EIP-712 che il wallet di destinazione deve firmare per
    ///         accettare la migrazione (comodo per client e test).
    function migrationDigest(uint256 tokenId, address newOwner, uint256 deadline) external view returns (bytes32) {
        address from = _requireOwned(tokenId);
        return _hashTypedDataV4(
            keccak256(abi.encode(MIGRATE_TYPEHASH, tokenId, from, newOwner, migrationNonce[tokenId], deadline))
        );
    }

    // ----------------------------------------------------------------- views

    /// @notice tokenId per un seriale CV (0 se mai mintato). Usato per riconciliare.
    function tokenBySerial(string calldata serial) external view returns (uint256) {
        return _tokenBySerial[keccak256(bytes(serial))];
    }

    /// @notice Seriale CV in chiaro di un token. Reverta se il token non esiste.
    function serialOf(uint256 tokenId) external view returns (string memory) {
        _requireOwned(tokenId);
        return _serialByToken[tokenId];
    }

    /// @notice Numero totale di token mintati (gli id sono sequenziali, mai bruciati).
    function totalMinted() external view returns (uint256) {
        return _nextTokenId;
    }

    /// @inheritdoc IERC5192
    function locked(uint256 tokenId) external view returns (bool) {
        _requireOwned(tokenId);
        return true;
    }

    // ------------------------------------------------------------- soulbound

    /// @dev Blocca ogni movimento tranne il mint e la migrate() in corso:
    ///      transfer E burn revertano.
    function _update(address to, uint256 tokenId, address auth) internal override returns (address) {
        if (_ownerOf(tokenId) != address(0) && _migrating != tokenId) revert Soulbound();
        return super._update(to, tokenId, auth);
    }

    function approve(address, uint256) public pure override(ERC721, IERC721) {
        revert Soulbound();
    }

    function setApprovalForAll(address, bool) public pure override(ERC721, IERC721) {
        revert Soulbound();
    }

    // ------------------------------------------------------------ interfaces

    function supportsInterface(bytes4 interfaceId)
        public
        view
        override(ERC721URIStorage, AccessControl)
        returns (bool)
    {
        return interfaceId == type(IERC5192).interfaceId || super.supportsInterface(interfaceId);
    }
}
