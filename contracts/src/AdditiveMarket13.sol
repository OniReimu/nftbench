// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {IERC2981} from "@openzeppelin/contracts/interfaces/IERC2981.sol";
import {IERC165} from "@openzeppelin/contracts/utils/introspection/IERC165.sol";

contract AdditiveMarket13 {
    struct Listing {
        address seller;
        uint256 price;
    }

    IERC20 public immutable paymentToken;
    uint256 public immutable flatFee;
    address public immutable feeRecipient;

    mapping(address => mapping(uint256 => Listing)) private _listings;

    event Listed(address indexed collection, uint256 indexed tokenId, address indexed seller, uint256 price);
    event Bought(
        address indexed collection,
        uint256 indexed tokenId,
        address indexed buyer,
        address seller,
        uint256 price,
        uint256 fee,
        uint256 royalty
    );

    constructor(address paymentToken_, uint256 flatFee_, address feeRecipient_) {
        paymentToken = IERC20(paymentToken_);
        flatFee = flatFee_;
        feeRecipient = feeRecipient_;
    }

    function list(address collection, uint256 tokenId, uint256 price) external {
        require(IERC721(collection).ownerOf(tokenId) == msg.sender, "caller must own token");
        require(price > 0, "price must be positive");
        _listings[collection][tokenId] = Listing(msg.sender, price);
        emit Listed(collection, tokenId, msg.sender, price);
    }

    function listingOf(address collection, uint256 tokenId) external view returns (address seller, uint256 price) {
        Listing memory listing = _listings[collection][tokenId];
        return (listing.seller, listing.price);
    }

    function quoteTotal(address collection, uint256 tokenId)
        public
        view
        returns (uint256 price, uint256 fee, uint256 royalty, uint256 totalRequired)
    {
        Listing memory listing = _listings[collection][tokenId];
        price = listing.price;
        fee = flatFee;
        royalty = 0;

        if (IERC165(collection).supportsInterface(type(IERC2981).interfaceId)) {
            try IERC2981(collection).royaltyInfo(tokenId, price) returns (address receiver, uint256 amount) {
                if (receiver != address(0)) {
                    royalty = amount;
                }
            } catch {}
        }
        totalRequired = price + fee + royalty;
    }

    function buy(address collection, uint256 tokenId) external {
        Listing memory listing = _listings[collection][tokenId];
        require(listing.price > 0, "not listed");

        address seller = listing.seller;
        require(IERC721(collection).ownerOf(tokenId) == seller, "seller no longer owns token");

        (uint256 price, uint256 fee, uint256 royalty, uint256 totalRequired) = quoteTotal(collection, tokenId);

        delete _listings[collection][tokenId];

        require(paymentToken.balanceOf(msg.sender) >= totalRequired, "insufficient payment balance");
        require(paymentToken.transferFrom(msg.sender, address(this), totalRequired), "payment failed");

        paymentToken.transfer(seller, price);
        if (fee > 0 && feeRecipient != address(0)) {
            paymentToken.transfer(feeRecipient, fee);
        }
        if (royalty > 0) {
            (address receiver,) = IERC2981(collection).royaltyInfo(tokenId, price);
            if (receiver != address(0)) {
                paymentToken.transfer(receiver, royalty);
            }
        }

        IERC721(collection).transferFrom(seller, msg.sender, tokenId);

        emit Bought(collection, tokenId, msg.sender, seller, price, fee, royalty);
    }
}
