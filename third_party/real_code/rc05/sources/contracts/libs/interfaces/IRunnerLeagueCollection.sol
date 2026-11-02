// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";

interface IRunnerLeagueCollection is IERC721 {
    struct Action {
        address contractAddress;
        address from;
        address to;
        uint256[] identifiers;
        uint256[] balances;
        bool[] transferable;
    }

    function execute(Action calldata _action) external;
}
