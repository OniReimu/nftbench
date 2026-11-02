// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {SoulboundCredential} from "./SoulboundCredential.sol";
import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";

/// @title MWX Protocol Credential — Génération 1
/// @notice Six séries (sections 19-20 du brief / A.5 du whitepaper), figées dans le code :
///
///         Série 0 (I)   : valeur réf.  $50   | supply 15 000 | capacité     4 | daily cap   2 MWX
///         Série 1 (II)  : valeur réf.  $200  | supply 10 000 | capacité    24 | daily cap  20 MWX
///         Série 2 (III) : valeur réf.  $500  | supply  7 500 | capacité    60 | daily cap  60 MWX
///         Série 3 (IV)  : valeur réf. $1 000 | supply  5 000 | capacité   180 | daily cap 182 MWX
///         Série 4 (V)   : valeur réf. $2 500 | supply  3 000 | capacité   450 | daily cap 400 MWX
///         Série 5 (VI)  : valeur réf. $5 000 | supply  1 500 | capacité 1 000 | daily cap 900 MWX
///
///         Aucune limite de quantité par wallet (section 20) : seule la supply globale de chaque
///         série borne le mint. Acquisition Directe et Acquisition par Burn consomment la MÊME
///         supply (sections 23-24) — garanti structurellement : les compteurs vivent ici, et le
///         MWXProtocolCredentialSaleRelay (unique MINTER_ROLE) n'est qu'un point d'entrée.
///         Un credential actif participe à 100 % de sa capacité, sans tâches Contributor (section 19).
contract MWXProtocolCredential is SoulboundCredential {
    uint8 public constant SERIES_I = 0;
    uint8 public constant SERIES_II = 1;
    uint8 public constant SERIES_III = 2;
    uint8 public constant SERIES_IV = 3;
    uint8 public constant SERIES_V = 4;
    uint8 public constant SERIES_VI = 5;

    constructor(address initialAdmin) ERC721("MWX Protocol Credential - Generation 1", "MWXPC") {
        _grantRole(DEFAULT_ADMIN_ROLE, initialAdmin);
    }

    /// @notice Mint réservé au MWXProtocolCredentialSaleRelay (MINTER_ROLE), pour les DEUX routes.
    function mint(address to, uint8 series) external onlyRole(MINTER_ROLE) returns (uint256 tokenId) {
        return _mintCredential(to, series);
    }

    // ------------------------------------------------------------------
    // Configuration Génération 1 — constantes de code, aucun setter (sections 60-61)
    // ------------------------------------------------------------------
    function _tierCount() internal pure override returns (uint8) {
        return 6;
    }

    function _tierConfig(uint8 series)
        internal
        pure
        override
        returns (uint32 maxSupply, uint128 nominalCapacity, uint128 dailyCap)
    {
        if (series == SERIES_I) return (15_000, 4e18, 2e18);
        if (series == SERIES_II) return (10_000, 24e18, 20e18);
        if (series == SERIES_III) return (7_500, 60e18, 60e18);
        if (series == SERIES_IV) return (5_000, 180e18, 182e18);
        if (series == SERIES_V) return (3_000, 450e18, 400e18);
        return (1_500, 1_000e18, 900e18); // SERIES_VI (series < _tierCount() déjà garanti)
    }

    /// @dev Aucune règle de possession par wallet pour les Protocol Credentials (section 20).
    function _checkAndRecordOwnershipRule(address, uint8) internal override {}
}
