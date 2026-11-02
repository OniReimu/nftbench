// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {IERC1155} from "@openzeppelin/contracts/token/ERC1155/IERC1155.sol";

contract DepositRouter13 {
    event BundleDeposited(
        address indexed sender,
        address indexed vault,
        address avatarToken,
        uint256 avatarId,
        address equipmentToken,
        uint256 equipmentId,
        uint256 equipmentAmount
    );

    function depositBundle(
        address vault,
        address avatarToken,
        uint256 avatarId,
        address equipmentToken,
        uint256 equipmentId,
        uint256 equipmentAmount
    ) external {
        IERC721(avatarToken).safeTransferFrom(msg.sender, vault, avatarId);
        IERC1155(equipmentToken).safeTransferFrom(msg.sender, vault, equipmentId, equipmentAmount, "");
        emit BundleDeposited(msg.sender, vault, avatarToken, avatarId, equipmentToken, equipmentId, equipmentAmount);
    }
}
