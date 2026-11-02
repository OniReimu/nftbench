// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC1155} from "@openzeppelin/contracts/token/ERC1155/IERC1155.sol";
import {IERC2981} from "@openzeppelin/contracts/interfaces/IERC2981.sol";

contract BundleMarket14 {
    IERC20 public immutable paymentToken;
    IERC1155 public immutable collection;
    uint256 public constant price = 100_000_000;
    uint256 public constant fee = 2_000_000;
    address public seller;
    bool public includesB;
    bool public active;

    constructor(address paymentToken_, address collection_) {
        paymentToken = IERC20(paymentToken_);
        collection = IERC1155(collection_);
    }

    function list(bool includesB_) external {
        require(seller == address(0), "listing exists");
        require(collection.balanceOf(msg.sender, 1) >= 1, "item A required");
        if (includesB_) require(collection.balanceOf(msg.sender, 2) >= 1, "item B required");
        seller = msg.sender;
        includesB = includesB_;
        active = true;
    }

    function quote(uint256[] calldata allocations, bool payRoyalties) public view returns (uint256 total) {
        require(allocations.length == (includesB ? 2 : 1), "allocation length");
        uint256 consideration;
        total = price + fee;
        for (uint256 i; i < allocations.length; ++i) {
            consideration += allocations[i];
            if (payRoyalties) {
                (, uint256 royalty) = IERC2981(address(collection)).royaltyInfo(i + 1, allocations[i]);
                total += royalty;
            }
        }
        require(consideration == price, "allocation total");
    }

    function buy(uint256[] calldata allocations, bool payRoyalties) external {
        require(active, "listing inactive");
        uint256 total = quote(allocations, payRoyalties);
        active = false;
        require(paymentToken.transferFrom(msg.sender, address(this), total), "payment failed");
        require(paymentToken.transfer(seller, price), "seller payment failed");
        for (uint256 i; i < allocations.length; ++i) {
            if (payRoyalties) {
                (address receiver, uint256 royalty) = IERC2981(address(collection)).royaltyInfo(i + 1, allocations[i]);
                if (royalty > 0) require(paymentToken.transfer(receiver, royalty), "royalty payment failed");
            }
            collection.safeTransferFrom(seller, msg.sender, i + 1, 1, "");
        }
    }
}
