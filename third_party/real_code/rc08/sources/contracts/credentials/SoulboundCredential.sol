// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/// @title SoulboundCredential — base commune MWC Pass / MWX Protocol Credential (Génération 1)
/// @notice Credential ERC-721 strictement soulbound (ERC-5192) :
///         - aucun transfert, aucune approbation, aucun burn, aucune réassignation administrative,
///           aucune récupération, aucune migration de wallet (sections 13 et 22 du brief) ;
///         - données de tier/série, capacité, daily cap et timestamps NON éditables (section 60) :
///           configurations figées dans le code, horodatage écrit une seule fois au mint ;
///         - durée de vie : 30 × MONTH (constante « mois grégorien moyen », R-2) à partir du mint ;
///           après expiration le credential reste on-chain, visible et non transférable, mais sa
///           capacité effective devient nulle (section 14) — il n'est jamais brûlé et ne rouvre
///           aucun slot de supply Génération 1.
abstract contract SoulboundCredential is ERC721, AccessControl {
    // ------------------------------------------------------------------
    // Rôles et constantes de temps (R-2 : convention unique du protocole)
    // ------------------------------------------------------------------
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");

    /// @notice Mois grégorien moyen exact : 365,2425 jours / 12.
    uint64 public constant MONTH = 2_629_746;
    /// @notice Durée de vie protocolaire : 30 mois = 78 892 380 s (≈ 913 jours).
    uint64 public constant LIFETIME = 30 * MONTH;

    // ------------------------------------------------------------------
    // Stockage
    // ------------------------------------------------------------------
    struct CredentialData {
        uint8 tier; // tier (Pass) ou série (Protocol Credential)
        uint64 mintTimestamp; // écrit une seule fois — l'expiration en dérive
    }

    mapping(uint256 => CredentialData) internal _credentials;
    /// @notice Compteurs de supply Génération 1 par tier/série — seuls garants du max supply.
    mapping(uint8 => uint32) public mintedPerTier;
    uint256 public nextTokenId; // premier tokenId = 1

    /// @dev Index append-only des tokenIds par wallet — exact à vie car strictement soulbound :
    ///      aucun transfert ni burn n'étant possible, rien n'est jamais retiré de l'index.
    mapping(address => uint256[]) private _ownedTokens;

    string private _baseTokenURI;

    // ------------------------------------------------------------------
    // Événements
    // ------------------------------------------------------------------
    /// @dev ERC-5192 : émis au mint, le credential est verrouillé à vie.
    event Locked(uint256 tokenId);
    event CredentialMinted(
        address indexed to,
        uint256 indexed tokenId,
        uint8 indexed tier,
        uint64 mintTimestamp,
        uint64 expiryTimestamp
    );
    event BaseURIUpdated(string newBaseURI);

    // ------------------------------------------------------------------
    // Erreurs
    // ------------------------------------------------------------------
    error NonTransferable();
    error ApprovalsDisabled();
    error InvalidTier();
    error TierSupplyExhausted();

    // ------------------------------------------------------------------
    // Hooks de configuration — figés dans le code des contrats concrets
    // ------------------------------------------------------------------
    function _tierCount() internal pure virtual returns (uint8);

    /// @return maxSupply       Supply maximale Génération 1 du tier/série.
    /// @return nominalCapacity Capacité nominale, échelle 1e18 (convention unique, section 18 du brief).
    /// @return dailyCap        Daily Cap par credential, en wei de MWX.
    function _tierConfig(uint8 tier)
        internal
        pure
        virtual
        returns (uint32 maxSupply, uint128 nominalCapacity, uint128 dailyCap);

    /// @dev Règle de possession (Pass : 1 par tier par wallet ; Protocol Credential : aucune limite).
    function _checkAndRecordOwnershipRule(address to, uint8 tier) internal virtual;

    // ------------------------------------------------------------------
    // Mint (réservé aux Sale Relays via MINTER_ROLE)
    // ------------------------------------------------------------------
    function _mintCredential(address to, uint8 tier) internal returns (uint256 tokenId) {
        if (tier >= _tierCount()) revert InvalidTier();
        (uint32 maxSupply,,) = _tierConfig(tier);
        if (mintedPerTier[tier] >= maxSupply) revert TierSupplyExhausted();

        _checkAndRecordOwnershipRule(to, tier);
        mintedPerTier[tier] += 1;

        tokenId = ++nextTokenId;
        uint64 nowTs = uint64(block.timestamp);
        _credentials[tokenId] = CredentialData({tier: tier, mintTimestamp: nowTs});

        // _mint (et non _safeMint) : aucun callback onERC721Received, donc aucune réentrance
        // possible depuis un destinataire contractuel ; un soulbound est « coincé » par conception.
        _mint(to, tokenId);
        _ownedTokens[to].push(tokenId);

        emit CredentialMinted(to, tokenId, tier, nowTs, nowTs + LIFETIME);
        emit Locked(tokenId);
    }

    // ------------------------------------------------------------------
    // Application stricte du soulbound
    // ------------------------------------------------------------------
    /// @dev Seul le mint (propriétaire actuel == adresse zéro) est autorisé.
    ///      Tout transfert ET tout burn (to == 0 avec un propriétaire existant) revert.
    function _update(address to, uint256 tokenId, address auth) internal override returns (address) {
        if (_ownerOf(tokenId) != address(0)) revert NonTransferable();
        return super._update(to, tokenId, auth);
    }

    function approve(address, uint256) public pure override {
        revert ApprovalsDisabled();
    }

    function setApprovalForAll(address, bool) public pure override {
        revert ApprovalsDisabled();
    }

    // ------------------------------------------------------------------
    // ERC-5192
    // ------------------------------------------------------------------
    /// @notice Toujours verrouillé, à vie.
    function locked(uint256 tokenId) external view returns (bool) {
        _requireOwned(tokenId);
        return true;
    }

    function supportsInterface(bytes4 interfaceId) public view override(ERC721, AccessControl) returns (bool) {
        return interfaceId == 0xb45a3c0e /* IERC5192 */ || super.supportsInterface(interfaceId);
    }

    // ------------------------------------------------------------------
    // Vues (section 84 du brief)
    // ------------------------------------------------------------------
    function tierOf(uint256 tokenId) public view returns (uint8) {
        _requireOwned(tokenId);
        return _credentials[tokenId].tier;
    }

    function mintTimestampOf(uint256 tokenId) public view returns (uint64) {
        _requireOwned(tokenId);
        return _credentials[tokenId].mintTimestamp;
    }

    function expiryOf(uint256 tokenId) public view returns (uint64) {
        _requireOwned(tokenId);
        return _credentials[tokenId].mintTimestamp + LIFETIME;
    }

    function nominalCapacityOf(uint256 tokenId) public view returns (uint128 nominalCapacity) {
        _requireOwned(tokenId);
        (, nominalCapacity,) = _tierConfig(_credentials[tokenId].tier);
    }

    function dailyCapOf(uint256 tokenId) public view returns (uint128 dailyCap) {
        _requireOwned(tokenId);
        (,, dailyCap) = _tierConfig(_credentials[tokenId].tier);
    }

    /// @notice Actif à l'instant t : minté, et t strictement avant l'expiration.
    function isActiveAt(uint256 tokenId, uint64 timestamp) public view returns (bool) {
        _requireOwned(tokenId);
        CredentialData storage data = _credentials[tokenId];
        return data.mintTimestamp <= timestamp && timestamp < data.mintTimestamp + LIFETIME;
    }

    /// @notice Règle d'éligibilité figée (R-8) — snapshot à la FIN d'epoch :
    ///         éligible à l'epoch E ssi mintTs ≤ fin(E) ET expiry > fin(E).
    ///         Participe à poids plein à l'epoch de son propre mint ; ne participe pas
    ///         à l'epoch au cours duquel il expire. Même règle côté moteur off-chain et côté claim.
    function isEligibleForEpoch(uint256 tokenId, uint64 epochId) public view returns (bool) {
        _requireOwned(tokenId);
        CredentialData storage data = _credentials[tokenId];
        uint256 epochEndExclusive = (uint256(epochId) + 1) * 1 days;
        return uint256(data.mintTimestamp) < epochEndExclusive
            && uint256(data.mintTimestamp) + LIFETIME >= epochEndExclusive;
    }

    /// @notice Tous les tokenIds (= saleIds) détenus par un wallet sur CE contrat, en une lecture.
    ///         Invariant : tokensOfOwner(wallet).length == balanceOf(wallet), à vie.
    function tokensOfOwner(address wallet) external view returns (uint256[] memory) {
        return _ownedTokens[wallet];
    }

    /// @notice Alias métier : le tokenId EST le saleId — séquence unique de CE contrat (le Pass et
    ///         le Protocol Credential démarrent chacun à 1, indépendamment), +1 à chaque mint quelle
    ///         que soit la route (vente publique, burn, admin) et quel que soit le tier/série,
    ///         jamais réutilisée, jamais trouée (un mint qui revert n'incrémente pas).
    function lastSaleId() external view returns (uint256) {
        return nextTokenId;
    }

    function tierConfig(uint8 tier)
        external
        pure
        returns (uint32 maxSupply, uint128 nominalCapacity, uint128 dailyCap)
    {
        if (tier >= _tierCount()) revert InvalidTier();
        return _tierConfig(tier);
    }

    function remainingSupply(uint8 tier) external view returns (uint32) {
        if (tier >= _tierCount()) revert InvalidTier();
        (uint32 maxSupply,,) = _tierConfig(tier);
        return maxSupply - mintedPerTier[tier];
    }

    // ------------------------------------------------------------------
    // Métadonnées (opérationnel, non économique)
    // ------------------------------------------------------------------
    function setBaseURI(string calldata newBaseURI) external onlyRole(DEFAULT_ADMIN_ROLE) {
        _baseTokenURI = newBaseURI;
        emit BaseURIUpdated(newBaseURI);
    }

    function _baseURI() internal view override returns (string memory) {
        return _baseTokenURI;
    }
}
