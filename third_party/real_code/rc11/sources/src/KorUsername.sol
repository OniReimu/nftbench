// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Strings} from "@openzeppelin/contracts/utils/Strings.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";

/// @title ERC-5192 Minimal Soulbound interface
interface IERC5192 {
    /// @notice Emitted when the locking status is set to locked.
    event Locked(uint256 tokenId);
    /// @notice Emitted when the locking status is set to unlocked.
    event Unlocked(uint256 tokenId);
    /// @notice Returns the locking status of a Soulbound Token.
    function locked(uint256 tokenId) external view returns (bool);
}

/// @title KorUsername — soulbound (non-transferable) username registry
/// @notice One username per wallet, minted onchain. Names are permanently
///         bound to their holder's address (ERC-5192 soulbound). The campaign's
///         source of truth for who owns which KOR username.
/// @dev    Pure registry: the token has NO on-chain artwork. All visual
///         metadata (the polaroid/passport image, JSON) is served off-chain by
///         the API via `baseTokenURI`; `tokenURI` is just a pointer at it.
///
///         Two mint paths:
///           • `mint(name, deadline, signature)` — a user self-mints, paying
///             their own gas, presenting an EIP-712 permit signed by the
///             `authorizer` (the API's signing key). The API only signs names
///             its policy allows — the RESERVED LIST lives in the API's
///             database (scales to millions of names at zero onchain cost),
///             and the chain enforces it by refusing unsigned mints.
///           • `mintTo` / `mintBatch`   — the backend `minter` (a relayer/worker)
///                                        mints ON BEHALF of a user and pays gas;
///                                        the token still lands in the USER's
///                                        wallet, so it stays soulbound to them.
///                                        No permit needed — this is also how
///                                        reserved names are deliberately
///                                        assigned to their intended holders.
///
///         Onchain reserved backstop: the owner additionally curates a small
///         blocklist (`setReserved`) that the PUBLIC mint path refuses EVEN
///         WITH a valid permit — defense in depth for crown-jewel names, so a
///         compromised authorizer key still can't leak them. Seeded at deploy
///         from packages/shared/reserved-usernames.json.
///
///         Permit replay is harmless by construction: a permit binds
///         (to, name, deadline), the name can only ever be minted once
///         (NameTaken) and the wallet can only ever hold one name
///         (AlreadyRegistered) — so no nonces are needed.
///
///         tokenId is a 1-based incremental counter. Name format is validated
///         onchain and mirrors the off-chain rule used by the API:
///         3–32 chars, lowercase [a-z0-9-], no leading/trailing hyphen.
contract KorUsername is ERC721, IERC5192, Ownable, EIP712 {
    // ─── Errors ───────────────────────────────────────────────────────────
    error Soulbound();
    error NameTaken();
    error AlreadyRegistered();
    error InvalidName();
    error NonexistentName();
    error NotMinter();
    error LengthMismatch();
    error NameReserved();
    error PermitExpired();
    error InvalidSignature();

    // ─── State ────────────────────────────────────────────────────────────
    uint256 private _nextId = 1;

    /// @notice The authorized minter (backend worker/relayer) allowed to call
    ///         `mintTo` / `mintBatch`. Separate from `owner` on purpose: this is
    ///         a hot key on a server, so it can ONLY mint — never set metadata or
    ///         change ownership. `owner` (ideally a cold wallet/multisig) rotates
    ///         it via `setMinter`.
    address public minter;

    /// @notice The API's mint-permit signing key. Public `mint` requires an
    ///         EIP-712 permit signed by this address — the server's database
    ///         holds the (arbitrarily large) reserved list and simply refuses
    ///         to sign blocked names. Like `minter`, this is a hot key that can
    ///         ONLY approve mints; `owner` rotates it via `setAuthorizer`.
    ///         Set to address(0) to pause public minting entirely.
    address public authorizer;

    /// @dev EIP-712 struct hash for mint permits. `name` is hashed per the
    ///      EIP-712 rules for dynamic string members.
    bytes32 private constant MINT_PERMIT_TYPEHASH =
        keccak256("MintPermit(address to,string name,uint256 deadline)");

    // name → tokenId (0 = unregistered)
    mapping(string => uint256) private _nameToTokenId;
    // tokenId → name
    mapping(uint256 => string) private _tokenIdToName;
    // owner → name (empty string = none)
    mapping(address => string) private _ownerToName;

    // keccak256(name) → reserved. Blocks the PUBLIC mint path only; the minter
    // paths bypass it so reserved names can be deliberately assigned. Keyed by
    // hash so lookups never copy the string. Reserving a name that fails
    // _isValidName is inert (it could never mint anyway), and a reservation
    // becomes moot once the name is minted (NameTaken fires first).
    mapping(bytes32 => bool) private _reserved;

    // Off-chain metadata base. When set (owner-only), tokenURI serves
    // `<base><tokenId>` so metadata (incl. the API-rendered image) is hosted;
    // when empty, tokenURI falls back to a minimal name-only on-chain JSON.
    string private _baseTokenURI;

    // ─── Events ───────────────────────────────────────────────────────────
    event Minted(address indexed owner, uint256 indexed tokenId, string name);
    event BaseTokenURISet(string uri);
    event MinterSet(address indexed minter);
    event AuthorizerSet(address indexed authorizer);
    event ReservedSet(string name, bool reserved);

    constructor() ERC721("KOR Username", "KOR") Ownable(msg.sender) EIP712("KOR Username", "1") {
        // The deployer is the initial minter + authorizer for convenience;
        // rotate to the backend worker / API signer post-deploy via
        // `setMinter` / `setAuthorizer`.
        minter = msg.sender;
        emit MinterSet(msg.sender);
        authorizer = msg.sender;
        emit AuthorizerSet(msg.sender);
    }

    // ─── Access control ─────────────────────────────────────────────────────

    modifier onlyMinter() {
        if (msg.sender != minter) revert NotMinter();
        _;
    }

    /// @notice Rotate the authorized minter (the backend worker). Owner-only.
    ///         Set to address(0) to disable relayed minting entirely.
    function setMinter(address newMinter) external onlyOwner {
        minter = newMinter;
        emit MinterSet(newMinter);
    }

    /// @notice Rotate the mint-permit signer (the API's authorizer key).
    ///         Owner-only. Set to address(0) to pause public minting.
    function setAuthorizer(address newAuthorizer) external onlyOwner {
        authorizer = newAuthorizer;
        emit AuthorizerSet(newAuthorizer);
    }

    // ─── Reserved names (owner) ────────────────────────────────────────────

    /// @notice Reserve (or release) a batch of names. Owner-only. Reserved
    ///         names cannot be self-minted by the public — only assigned via
    ///         the minter paths (`mintTo` / `mintBatch`). Releasing a name
    ///         returns it to the public pool. Already-minted names are
    ///         unaffected either way (mint state always wins).
    function setReserved(string[] calldata names, bool reserved) external onlyOwner {
        for (uint256 i = 0; i < names.length; i++) {
            _reserved[keccak256(bytes(names[i]))] = reserved;
            emit ReservedSet(names[i], reserved);
        }
    }

    /// @notice True if `name` is on the reserved blocklist (regardless of
    ///         whether it has since been minted via a privileged path).
    function isReserved(string calldata name) external view returns (bool) {
        return _reserved[keccak256(bytes(name))];
    }

    // ─── Metadata source (owner) ───────────────────────────────────────────

    /// @notice Point tokenURI at a hosted metadata base (e.g.
    ///         "https://id.korprotocol.io/usernames/assets/"). Set to "" to revert to the
    ///         minimal on-chain JSON. Only affects the metadata pointer — never
    ///         ownership, transferability, or who owns which name.
    function setBaseTokenURI(string calldata uri) external onlyOwner {
        _baseTokenURI = uri;
        emit BaseTokenURISet(uri);
    }

    /// @notice The current off-chain metadata base ("" = on-chain JSON).
    function baseTokenURI() external view returns (string memory) {
        return _baseTokenURI;
    }

    // ─── Mint ─────────────────────────────────────────────────────────────

    /// @notice Self-mint a soulbound username to the caller (caller pays gas),
    ///         presenting an EIP-712 permit signed by the `authorizer`. The
    ///         permit binds (caller, name, deadline) — it cannot be used by a
    ///         different wallet or for a different name, and replay is inert
    ///         (the name/wallet checks make a second use revert). Reverts if
    ///         the permit is expired or not signed by the authorizer, or if the
    ///         name is malformed, taken, onchain-reserved, or the caller
    ///         already owns one.
    /// @param name      Desired username (lowercase [a-z0-9-], 3–32 chars).
    /// @param deadline  Unix time the permit expires (signed into the permit).
    /// @param signature Authorizer's EIP-712 signature over
    ///                  MintPermit(to=msg.sender, name, deadline).
    /// @return tokenId The freshly minted token id.
    function mint(string calldata name, uint256 deadline, bytes calldata signature)
        external
        returns (uint256 tokenId)
    {
        if (block.timestamp > deadline) revert PermitExpired();
        bytes32 digest = _hashTypedDataV4(
            keccak256(abi.encode(MINT_PERMIT_TYPEHASH, msg.sender, keccak256(bytes(name)), deadline))
        );
        if (ECDSA.recover(digest, signature) != authorizer) revert InvalidSignature();
        return _register(msg.sender, name, false);
    }

    /// @notice Mint a soulbound username to `to` on their behalf. Minter-only;
    ///         the backend worker calls this (paying gas) after the user has
    ///         proven ownership of `to` off-chain (SIWE). The token binds to
    ///         `to`, NOT to the worker — so it stays soulbound to the user.
    ///         Bypasses the reserved blocklist: this is the assignment path
    ///         for reserved names.
    /// @param to   The user's wallet, which will own the soulbound username.
    /// @param name Desired username (lowercase [a-z0-9-], 3–32 chars).
    /// @return tokenId The freshly minted token id.
    function mintTo(address to, string calldata name) external onlyMinter returns (uint256 tokenId) {
        return _register(to, name, true);
    }

    /// @notice Mint many soulbound usernames in one transaction. Minter-only.
    ///         Amortizes the per-tx overhead across the batch. Atomic: if ANY
    ///         entry is invalid, taken, or a recipient already owns a name, the
    ///         whole batch reverts and nothing is minted — so callers must
    ///         validate/reserve names off-chain before batching.
    /// @param to    Recipient wallets, one per name (same length as `names`).
    /// @param names Desired usernames, one per recipient.
    /// @return ids  The minted token ids, in order.
    function mintBatch(address[] calldata to, string[] calldata names)
        external
        onlyMinter
        returns (uint256[] memory ids)
    {
        if (to.length != names.length) revert LengthMismatch();
        ids = new uint256[](to.length);
        for (uint256 i = 0; i < to.length; i++) {
            ids[i] = _register(to[i], names[i], true);
        }
    }

    /// @dev Shared registration: validate, claim the name, bind it to `to`,
    ///      and mint the soulbound token. Used by every mint path.
    ///      `allowReserved` is true only on the privileged minter paths.
    function _register(address to, string memory name, bool allowReserved)
        internal
        returns (uint256 tokenId)
    {
        if (!_isValidName(name)) revert InvalidName();
        if (_nameToTokenId[name] != 0) revert NameTaken();
        if (!allowReserved && _reserved[keccak256(bytes(name))]) revert NameReserved();
        if (bytes(_ownerToName[to]).length != 0) revert AlreadyRegistered();

        tokenId = _nextId++;
        _nameToTokenId[name] = tokenId;
        _tokenIdToName[tokenId] = name;
        _ownerToName[to] = name;

        _safeMint(to, tokenId);

        emit Minted(to, tokenId, name);
        emit Locked(tokenId);
    }

    // ─── Views ────────────────────────────────────────────────────────────

    /// @notice True if `name` is well-formed, unclaimed, AND not reserved —
    ///         i.e. mintable by the public. (A reserved name is unavailable to
    ///         the public even though the minter could still assign it; check
    ///         `tokenIdOfName(name) == 0` for raw unclaimed state.)
    function available(string calldata name) external view returns (bool) {
        return _isValidName(name) && _nameToTokenId[name] == 0 && !_reserved[keccak256(bytes(name))];
    }

    /// @notice The name owned by `owner`, or "" if none.
    function nameOf(address owner) external view returns (string memory) {
        return _ownerToName[owner];
    }

    /// @notice The address that owns `name`. Reverts if unregistered.
    function ownerOfName(string calldata name) external view returns (address) {
        uint256 tokenId = _nameToTokenId[name];
        if (tokenId == 0) revert NonexistentName();
        return ownerOf(tokenId);
    }

    /// @notice The name bound to `tokenId`.
    function nameByTokenId(uint256 tokenId) external view returns (string memory) {
        _requireOwned(tokenId);
        return _tokenIdToName[tokenId];
    }

    /// @notice The tokenId bound to `name` (0 if unregistered).
    function tokenIdOfName(string calldata name) external view returns (uint256) {
        return _nameToTokenId[name];
    }

    // ─── ERC-5192 (soulbound) ─────────────────────────────────────────────

    /// @inheritdoc IERC5192
    function locked(uint256 tokenId) external view returns (bool) {
        _requireOwned(tokenId);
        return true; // every KOR username is permanently soulbound
    }

    function supportsInterface(bytes4 interfaceId) public view override(ERC721) returns (bool) {
        return interfaceId == type(IERC5192).interfaceId || super.supportsInterface(interfaceId);
    }

    /// @dev Block every transfer. Minting (from == 0) is the only allowed move;
    ///      burning is not exposed. Wallet-to-wallet transfers revert.
    function _update(address to, uint256 tokenId, address auth)
        internal
        override
        returns (address)
    {
        address from = _ownerOf(tokenId);
        if (from != address(0) && to != address(0)) revert Soulbound();
        return super._update(to, tokenId, auth);
    }

    // ─── Metadata ─────────────────────────────────────────────────────────

    /// @notice ERC-721 metadata pointer. When a hosted base is set, serves
    ///         `<base><tokenId>` (the API renders the JSON + polaroid image
    ///         off-chain). Before the base is set, returns a minimal name-only
    ///         JSON so the token is never broken — but carries no image.
    /// @dev    `name` is validated to [a-z0-9-] by _isValidName, so it contains
    ///         no quotes/backslashes and needs no JSON escaping.
    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        _requireOwned(tokenId);

        if (bytes(_baseTokenURI).length != 0) {
            return string.concat(_baseTokenURI, Strings.toString(tokenId));
        }

        string memory name = _tokenIdToName[tokenId];
        return string.concat(
            'data:application/json;utf8,{"name":"', name, '.kor",',
            '"description":"Soulbound KOR username. Non-transferable, permanently bound to its holder on Base.",',
            '"attributes":[',
            '{"trait_type":"username","value":"', name, '"},',
            '{"trait_type":"soulbound","value":"true"},',
            '{"trait_type":"token_id","value":"', Strings.toString(tokenId), '"}]}'
        );
    }

    // ─── Name validation ──────────────────────────────────────────────────

    /// @dev Mirrors USERNAME_RE in the API: ^[a-z0-9](?:[a-z0-9-]{1,30}[a-z0-9])?$
    ///      i.e. 3–32 chars, lowercase alphanumeric + hyphen, no leading/
    ///      trailing hyphen. (A 1–2 char name fails the length check below.)
    function _isValidName(string memory name) internal pure returns (bool) {
        bytes memory b = bytes(name);
        uint256 len = b.length;
        if (len < 3 || len > 32) return false;

        for (uint256 i = 0; i < len; i++) {
            bytes1 c = b[i];
            bool isLower = (c >= 0x61 && c <= 0x7a); // a-z
            bool isDigit = (c >= 0x30 && c <= 0x39); // 0-9
            bool isHyphen = (c == 0x2d); // -
            if (!isLower && !isDigit && !isHyphen) return false;
            // No leading or trailing hyphen.
            if (isHyphen && (i == 0 || i == len - 1)) return false;
        }
        return true;
    }
}
