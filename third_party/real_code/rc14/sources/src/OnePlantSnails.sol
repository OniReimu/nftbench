// SPDX-License-Identifier: MIT
/**
            official website: https://oneplantart.github.io/
**/

pragma solidity ^0.8.29;

interface IERC721Receiver {
    function onERC721Received(address operator, address from, uint256 tokenId, bytes calldata data) external returns (bytes4);
}

contract OnePlantSnails {
    string public constant name = "OnePlant Snails";
    string public constant symbol = "OPS";

    uint256 public constant MAX_SUPPLY = 3333;

    uint256 private constant OP_COUNT_1_HOLDERS = 536;
    uint256 private constant OP_COUNT_1_ALLOCATION = 1;
    uint256 private constant OP_COUNT_1_SPOTS = 536;

    uint256 private constant OP_COUNT_2_TO_6_HOLDERS = 284;
    uint256 private constant OP_COUNT_2_TO_6_ALLOCATION = 3;
    uint256 private constant OP_COUNT_2_TO_6_SPOTS = 852;

    uint256 private constant OP_COUNT_7_TO_20_HOLDERS = 53;
    uint256 private constant OP_COUNT_7_TO_20_ALLOCATION = 5;
    uint256 private constant OP_COUNT_7_TO_20_SPOTS = 265;

    uint256 private constant OP_COUNT_21_PLUS_HOLDERS = 49;
    uint256 private constant OP_COUNT_21_PLUS_ALLOCATION = 10;
    uint256 private constant OP_COUNT_21_PLUS_SPOTS = 490;

    uint256 private constant OP_HOLDER_ALLOWLIST_PEOPLE = 922;
    uint256 public constant OP_HOLDER_ALLOWLIST_SPOTS = 2143;
    uint256 public constant NON_OP_SPOTS = MAX_SUPPLY - OP_HOLDER_ALLOWLIST_SPOTS;
    uint256 public constant ALLOWLIST_MINT_DURATION = 48 hours;
    uint256 private constant FREE_MINT_MAX_PER_WALLET = 1;
    uint256 private constant FREE_MINT_MAX_PER_BLOCK = 5;
    uint256 private constant MIN_FREE_MINT_ETH_BALANCE = 0.01 ether;
    uint256 private constant ROYALTY_FEE_DENOMINATOR = 10000;
    uint256 private constant DEFAULT_ROYALTY_BPS = 100;

    address public owner;
    address public immutable royaltyReceiver;

    uint256 public totalSupply;
    uint256 public opAllowlistMinted;
    uint256 public collabMinted;
    uint256 public freeMinted;
    uint256 public opAllowlistAllocatedSpots;
    uint256 public collabAllocatedSpots;
    uint256 public allowlistMintStartedAt;

    bool public allowlistMintEnabled;
    bool public freeMintEnabled;
    bool public revealed;

    string private _hiddenURI;
    string private _baseTokenURI;

    mapping(uint256 => address) private _owners;
    mapping(address => uint256) private _balances;
    mapping(uint256 => address) private _tokenApprovals;
    mapping(address => mapping(address => bool)) private _operatorApprovals;

    mapping(address => uint256) public opHolderAllocation;
    mapping(address => uint256) public opAllowlistMintedBy;
    mapping(address => uint256) public collabAllocation;
    mapping(address => uint256) public collabMintedBy;
    mapping(address => uint256) public freeMintedBy;
    mapping(uint256 => uint256) public freeMintedInBlock;

    event Transfer(address indexed from, address indexed to, uint256 indexed tokenId);
    event Approval(address indexed owner, address indexed approved, uint256 indexed tokenId);
    event ApprovalForAll(address indexed owner, address indexed operator, bool approved);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event OpHolderSpotsSet(uint256 spotsPerWallet, uint256 walletCount);
    event OpHolderWalletSpotSet(address indexed account, uint256 previousAllocation, uint256 newAllocation);
    event RevealStateSet(bool revealed);
    event BaseURISet(string baseURI);
    event HiddenURISet(string hiddenURI);
    event MintConfigSet(bool allowlistEnabled, bool freeMintEnabled);
    event AllowlistMintWindowStarted(uint256 startedAt, uint256 endsAt);
    event CollabSpotsSet(uint256 spotsPerWallet, uint256 walletCount);
    event CollabWalletSpotSet(address indexed account, uint256 previousAllocation, uint256 newAllocation);

    modifier onlyOwner() {
        require(msg.sender == owner, "NOT_OWNER");
        _;
    }

    modifier onlyDuringAllowlistMint() {
        require(_allowlistMintOpen(), "ALLOWLIST_CLOSED");
        _;
    }

    constructor(string memory hiddenURI_, address royaltyReceiver_) {
        require(royaltyReceiver_ != address(0), "ZERO_ROYALTY_RECEIVER");
        owner = msg.sender;
        royaltyReceiver = royaltyReceiver_;
        _hiddenURI = hiddenURI_;
        emit OwnershipTransferred(address(0), msg.sender);
        emit HiddenURISet(hiddenURI_);
    }

    function supportsInterface(bytes4 interfaceId) external pure returns (bool) {
        return interfaceId == 0x01ffc9a7 || interfaceId == 0x80ac58cd || interfaceId == 0x5b5e139f || interfaceId == 0x2a55205a;
    }

    function royaltyInfo(uint256, uint256 salePrice) external view returns (address receiver, uint256 royaltyAmount) {
        receiver = royaltyReceiver;
        royaltyAmount = (salePrice * DEFAULT_ROYALTY_BPS) / ROYALTY_FEE_DENOMINATOR;
    }

    function balanceOf(address account) public view returns (uint256) {
        require(account != address(0), "ZERO_ADDRESS");
        return _balances[account];
    }

    function ownerOf(uint256 tokenId) public view returns (address) {
        address tokenOwner = _owners[tokenId];
        require(tokenOwner != address(0), "NOT_MINTED");
        return tokenOwner;
    }

    function getApproved(uint256 tokenId) public view returns (address) {
        require(_exists(tokenId), "NOT_MINTED");
        return _tokenApprovals[tokenId];
    }

    function isApprovedForAll(address account, address operator) public view returns (bool) {
        return _operatorApprovals[account][operator];
    }

    function approve(address to, uint256 tokenId) external {
        address tokenOwner = ownerOf(tokenId);
        require(msg.sender == tokenOwner || isApprovedForAll(tokenOwner, msg.sender), "NOT_AUTHORIZED");
        _tokenApprovals[tokenId] = to;
        emit Approval(tokenOwner, to, tokenId);
    }

    function setApprovalForAll(address operator, bool approved) external {
        require(operator != msg.sender, "SELF_APPROVAL");
        _operatorApprovals[msg.sender][operator] = approved;
        emit ApprovalForAll(msg.sender, operator, approved);
    }

    function transferFrom(address from, address to, uint256 tokenId) public {
        require(_isApprovedOrOwner(msg.sender, tokenId), "NOT_AUTHORIZED");
        _transfer(from, to, tokenId);
    }

    function safeTransferFrom(address from, address to, uint256 tokenId) external {
        safeTransferFrom(from, to, tokenId, "");
    }

    function safeTransferFrom(address from, address to, uint256 tokenId, bytes memory data) public {
        transferFrom(from, to, tokenId);
        require(_checkOnERC721Received(from, to, tokenId, data), "UNSAFE_RECIPIENT");
    }

    function tokenURI(uint256 tokenId) external view returns (string memory) {
        require(_exists(tokenId), "NOT_MINTED");
        if (!revealed) {
            return _hiddenURI;
        }
        return string.concat(_baseTokenURI, _toString(tokenId), ".json");
    }

    function allowlistMintEndsAt() external view returns (uint256) {
        return _allowlistMintEndsAt();
    }

    function allowlistMintOpen() external view returns (bool) {
        return _allowlistMintOpen();
    }

    function allowlistMintExpired() external view returns (bool) {
        return _allowlistMintExpired();
    }

    function nonOpMinted() external view returns (uint256) {
        return _nonOpMinted();
    }

    function remainingOpAllowlistMints(address account) public view returns (uint256) {
        uint256 allocation = opHolderAllocation[account];
        uint256 alreadyMinted = opAllowlistMintedBy[account];
        if (allocation <= alreadyMinted) {
            return 0;
        }
        return allocation - alreadyMinted;
    }

    function remainingCollabMints(address account) public view returns (uint256) {
        uint256 allocation = collabAllocation[account];
        uint256 alreadyMinted = collabMintedBy[account];
        if (allocation <= alreadyMinted) {
            return 0;
        }
        return allocation - alreadyMinted;
    }

    function walletMintInfo(address account)
        external
        view
        returns (
            uint256 opAllocation,
            uint256 opMintedByWallet,
            uint256 opRemaining,
            uint256 collabWalletAllocation,
            uint256 collabMintedByWallet,
            uint256 collabRemaining,
            uint256 freeMintedByWallet,
            uint256 allowlistEndsAt,
            bool isAllowlistOpen,
            bool isFreeMintOpen
        )
    {
        opAllocation = opHolderAllocation[account];
        opMintedByWallet = opAllowlistMintedBy[account];
        opRemaining = remainingOpAllowlistMints(account);
        collabWalletAllocation = collabAllocation[account];
        collabMintedByWallet = collabMintedBy[account];
        collabRemaining = remainingCollabMints(account);
        freeMintedByWallet = freeMintedBy[account];
        allowlistEndsAt = _allowlistMintEndsAt();
        isAllowlistOpen = _allowlistMintOpen();
        isFreeMintOpen = freeMintEnabled;
    }

    function mintOpAllowlist() external onlyDuringAllowlistMint {
        uint256 quantity = remainingOpAllowlistMints(msg.sender);
        _mintOpAllowlist(msg.sender, quantity);
    }

    function mintCollab() external onlyDuringAllowlistMint {
        uint256 quantity = remainingCollabMints(msg.sender);
        _mintCollab(msg.sender, quantity);
    }

    function freeMint() external {
        require(freeMintEnabled, "FREE_MINT_CLOSED");
        _enforcePlainEOA();
        require(msg.sender.balance >= MIN_FREE_MINT_ETH_BALANCE, "LOW_ETH_BALANCE");
        require(freeMintedBy[msg.sender] < FREE_MINT_MAX_PER_WALLET, "FREE_MINT_ALREADY_USED");
        require(freeMintedInBlock[block.number] < FREE_MINT_MAX_PER_BLOCK, "FREE_MINT_BLOCK_LIMIT");
        require(totalSupply + 1 <= MAX_SUPPLY, "MAX_SUPPLY_EXCEEDED");
        if (!_allowlistMintExpired()) {
            require(_nonOpMinted() + 1 <= NON_OP_SPOTS, "NON_OP_SPOTS_EXCEEDED");
        }

        freeMintedBy[msg.sender] += 1;
        freeMintedInBlock[block.number] += 1;
        freeMinted += 1;
        _mintSequential(msg.sender, 1);
    }

    function _mintOpAllowlist(address account, uint256 quantity) private {
        require(quantity > 0, "NO_OP_MINTS_AVAILABLE");
        require(opAllowlistMintedBy[account] + quantity <= opHolderAllocation[account], "ALLOCATION_EXCEEDED");
        require(opAllowlistMinted + quantity <= OP_HOLDER_ALLOWLIST_SPOTS, "OP_SPOTS_EXCEEDED");
        require(totalSupply + quantity <= MAX_SUPPLY, "MAX_SUPPLY_EXCEEDED");

        opAllowlistMintedBy[account] += quantity;
        opAllowlistMinted += quantity;
        _mintSequential(account, quantity);
    }

    function _mintCollab(address account, uint256 quantity) private {
        require(quantity > 0, "NO_COLLAB_MINTS_AVAILABLE");
        require(collabMintedBy[account] + quantity <= collabAllocation[account], "COLLAB_ALLOCATION_EXCEEDED");
        require(_nonOpMinted() + quantity <= NON_OP_SPOTS, "NON_OP_SPOTS_EXCEEDED");
        require(totalSupply + quantity <= MAX_SUPPLY, "MAX_SUPPLY_EXCEEDED");

        collabMintedBy[account] += quantity;
        collabMinted += quantity;
        _mintSequential(account, quantity);
    }

    function setOPHolderSpots(uint256 spots, address[] calldata accounts) external onlyOwner {
        require(_validOpHolderSpots(spots), "INVALID_OP_SPOTS");
        uint256 allocated = opAllowlistAllocatedSpots;
        for (uint256 i = 0; i < accounts.length; i++) {
            address account = accounts[i];
            require(account != address(0), "ZERO_ADDRESS");
            require(spots >= opAllowlistMintedBy[account], "BELOW_MINTED");
            uint256 previous = opHolderAllocation[account];
            allocated = allocated - previous + spots;
            opHolderAllocation[account] = spots;
            emit OpHolderWalletSpotSet(account, previous, spots);
        }
        require(allocated <= OP_HOLDER_ALLOWLIST_SPOTS, "OP_ALLOCATION_CAP_EXCEEDED");
        opAllowlistAllocatedSpots = allocated;
        emit OpHolderSpotsSet(spots, accounts.length);
    }

    function setCollabSpots(uint256 spots, address[] calldata accounts) external onlyOwner {
        require(spots <= NON_OP_SPOTS, "SPOTS_TOO_HIGH");
        uint256 allocated = collabAllocatedSpots;
        for (uint256 i = 0; i < accounts.length; i++) {
            address account = accounts[i];
            require(account != address(0), "ZERO_ADDRESS");
            require(spots >= collabMintedBy[account], "BELOW_MINTED");
            uint256 previous = collabAllocation[account];
            allocated = allocated - previous + spots;
            collabAllocation[account] = spots;
            emit CollabWalletSpotSet(account, previous, spots);
        }
        require(allocated <= NON_OP_SPOTS, "COLLAB_ALLOCATION_CAP_EXCEEDED");
        collabAllocatedSpots = allocated;
        emit CollabSpotsSet(spots, accounts.length);
    }

    function setMintConfig(bool allowlistEnabled, bool freeEnabled) external onlyOwner {
        bool startsNewAllowlistWindow = allowlistEnabled && !allowlistMintEnabled;
        allowlistMintEnabled = allowlistEnabled;
        freeMintEnabled = freeEnabled;
        if (startsNewAllowlistWindow) {
            allowlistMintStartedAt = block.timestamp;
            emit AllowlistMintWindowStarted(block.timestamp, block.timestamp + ALLOWLIST_MINT_DURATION);
        }
        emit MintConfigSet(allowlistEnabled, freeEnabled);
    }

    function setBaseURI(string calldata newBaseURI) external onlyOwner {
        _baseTokenURI = newBaseURI;
        emit BaseURISet(newBaseURI);
    }

    function setHiddenURI(string calldata newHiddenURI) external onlyOwner {
        _hiddenURI = newHiddenURI;
        emit HiddenURISet(newHiddenURI);
    }

    function setRevealed(bool value) external onlyOwner {
        revealed = value;
        emit RevealStateSet(value);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "ZERO_ADDRESS");
        address previousOwner = owner;
        owner = newOwner;
        emit OwnershipTransferred(previousOwner, newOwner);
    }

    function renounceOwnership() external onlyOwner {
        address previousOwner = owner;
        owner = address(0);
        emit OwnershipTransferred(previousOwner, address(0));
    }

    function _validOpHolderSpots(uint256 spots) private pure returns (bool) {
        return spots == 0
            || spots == OP_COUNT_1_ALLOCATION
            || spots == OP_COUNT_2_TO_6_ALLOCATION
            || spots == OP_COUNT_7_TO_20_ALLOCATION
            || spots == OP_COUNT_21_PLUS_ALLOCATION;
    }

    function _allowlistMintEndsAt() private view returns (uint256) {
        if (allowlistMintStartedAt == 0) {
            return 0;
        }
        return allowlistMintStartedAt + ALLOWLIST_MINT_DURATION;
    }

    function _allowlistMintOpen() private view returns (bool) {
        uint256 endsAt = _allowlistMintEndsAt();
        return allowlistMintEnabled && endsAt != 0 && block.timestamp < endsAt;
    }

    function _allowlistMintExpired() private view returns (bool) {
        uint256 endsAt = _allowlistMintEndsAt();
        return endsAt != 0 && block.timestamp >= endsAt;
    }

    function _nonOpMinted() private view returns (uint256) {
        return collabMinted + freeMinted;
    }

    function _mintSequential(address to, uint256 quantity) private {
        require(to != address(0), "ZERO_ADDRESS");
        for (uint256 i = 0; i < quantity; i++) {
            uint256 tokenId = totalSupply + 1;
            totalSupply = tokenId;
            _balances[to] += 1;
            _owners[tokenId] = to;
            emit Transfer(address(0), to, tokenId);
        }
    }

    function _transfer(address from, address to, uint256 tokenId) private {
        require(ownerOf(tokenId) == from, "WRONG_FROM");
        require(to != address(0), "ZERO_ADDRESS");
        delete _tokenApprovals[tokenId];
        _balances[from] -= 1;
        _balances[to] += 1;
        _owners[tokenId] = to;
        emit Transfer(from, to, tokenId);
    }

    function _exists(uint256 tokenId) private view returns (bool) {
        return _owners[tokenId] != address(0);
    }

    function _isApprovedOrOwner(address spender, uint256 tokenId) private view returns (bool) {
        address tokenOwner = ownerOf(tokenId);
        return spender == tokenOwner || getApproved(tokenId) == spender || isApprovedForAll(tokenOwner, spender);
    }

    function _checkOnERC721Received(address from, address to, uint256 tokenId, bytes memory data) private returns (bool) {
        if (to.code.length == 0) {
            return true;
        }
        try IERC721Receiver(to).onERC721Received(msg.sender, from, tokenId, data) returns (bytes4 retval) {
            return retval == IERC721Receiver.onERC721Received.selector;
        } catch {
            return false;
        }
    }

    function _enforcePlainEOA() private view {
        require(msg.sender == tx.origin, "EOA_ONLY");
        require(msg.sender.code.length == 0, "EOA_CODE_NOT_ALLOWED");
    }

    function _toString(uint256 value) private pure returns (string memory) {
        if (value == 0) {
            return "0";
        }
        uint256 temp = value;
        uint256 digits;
        while (temp != 0) {
            digits++;
            temp /= 10;
        }
        bytes memory buffer = new bytes(digits);
        while (value != 0) {
            digits -= 1;
            buffer[digits] = bytes1(uint8(48 + uint256(value % 10)));
            value /= 10;
        }
        return string(buffer);
    }
}
