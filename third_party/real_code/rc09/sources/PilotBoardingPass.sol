// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title Pilot Boarding Pass NFT
 * @notice Official contract of the Pilot Boarding Pass collection on BNB Chain.
 * @dev Verified source. All rights reserved by Pilot Team.
 *
 * Official Links:
 *  - Website: https://pilotagent.org
 */

// Minimal interface for ERC20 recovery
interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

// ERC165 interface for introspection
interface IERC165 {
    function supportsInterface(bytes4 interfaceId) external view returns (bool);
}

// Minimal ERC721 interface for marketplaces
interface IERC721 is IERC165 {
    event Transfer(address indexed from, address indexed to, uint256 indexed tokenId);
    event Approval(address indexed owner, address indexed approved, uint256 indexed tokenId);
    event ApprovalForAll(address indexed owner, address indexed operator, bool approved);
    function balanceOf(address owner) external view returns (uint256 balance);
    function ownerOf(uint256 tokenId) external view returns (address owner);
    function safeTransferFrom(address from, address to, uint256 tokenId) external;
    function transferFrom(address from, address to, uint256 tokenId) external;
    function approve(address to, uint256 tokenId) external;
    function getApproved(uint256 tokenId) external view returns (address operator);
    function setApprovalForAll(address operator, bool _approved) external;
    function isApprovedForAll(address owner, address operator) external view returns (bool);
    function safeTransferFrom(address from, address to, uint256 tokenId, bytes calldata data) external;
}

interface IERC721Metadata {
    function name() external view returns (string memory);
    function symbol() external view returns (string memory);
    function tokenURI(uint256 tokenId) external view returns (string memory);
}

interface IERC721Receiver {
    function onERC721Received(address operator, address from, uint256 tokenId, bytes calldata data) external returns (bytes4);
}

/**
 * @title PilotBoardingPass
 * @notice Soulbound NFT boarding pass with dynamic miles and metadata
 * @dev Flight PX001 - The journey begins here
 * Implements ERC-721 with transfer restrictions and ERC-5192 for soulbound tokens
 */
contract PilotBoardingPass is IERC721, IERC721Metadata {
    
    // ============ Structs ============
    
    struct BoardingPass {
        uint256 seatRow;        // 1, 2, 3...
        string seatLetter;      // A, B, C, D, E
        uint256 miles;          // Current miles balance
        uint256 milesEarned;    // Total miles earned
        uint256 milesSpent;     // Total miles spent
        uint256 mintedAt;       // Mint timestamp
        uint256 lastUpdated;    // Last update timestamp
        uint256 bannedUntil;    // Ban expiry timestamp (0 = not banned)
        mapping(bytes32 => string) customText;   // Extensible text fields
        mapping(bytes32 => uint256) customNumber; // Extensible number fields
    }
    
    // ============ State Variables ============
    
    string public override name = "Pilot Boarding Pass";
    string public override symbol = "PX001";
    string public baseURI = "";
    string public staticFilename = "NFT001.json";
    bool public individualURI = false; // true: each token has its own URI
    string public contractMetadataURI;


    uint256 public totalSupply;
    uint256 public mintPrice = 0; // Initially free
    uint256 public initialMiles = 100; // Bonus miles on mint
    bool public mintingActive = true;
    
    // Flexible tier system
    uint256[] public tierThresholds;
    
    // Ownership and roles
    address public owner;
    mapping(address => bool) public isAdmin;
    mapping(address => bool) public isMilesOperator;
    mapping(address => bool) public isEditor;
    
    // NFT data
    mapping(uint256 => address) private _owners;
    mapping(address => uint256) private _balances;
    
    // Boarding pass data
    mapping(uint256 => BoardingPass) public boardingPasses;
    mapping(address => uint256) public addressToTokenId;
    mapping(address => bool) public hasMinted;
    
    // ============ Events ============
    
    event MilesUpdated(uint256 indexed tokenId, uint256 oldMiles, uint256 newMiles);
    event UserBanned(uint256 indexed tokenId, uint256 until);
    event UserUnbanned(uint256 indexed tokenId);
    event MetadataUpdated(uint256 indexed tokenId, string field);
    event MetadataUpdate(uint256 _tokenId);
    event BatchMetadataUpdate(uint256 _fromTokenId, uint256 _toTokenId);
    event AdminAdded(address indexed admin);
    event AdminRemoved(address indexed admin);
    event MilesOperatorAdded(address indexed operator);
    event MilesOperatorRemoved(address indexed operator);
    event EditorAdded(address indexed editor);
    event EditorRemoved(address indexed editor);
    event MintPriceUpdated(uint256 oldPrice, uint256 newPrice);
    event MintingToggled(bool active);
    event BaseURIUpdated(string newURI);
    event ContractURIUpdated(string newURI);
    event TierThresholdsUpdated(uint256[] thresholds);
    event Locked(uint256 tokenId); // ERC-5192
    
    // ============ Modifiers ============
    
    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }
    
    modifier onlyAdmin() {
        require(msg.sender == owner || isAdmin[msg.sender], "Not admin");
        _;
    }
    
    modifier onlyMilesOperator() {
        require(msg.sender == owner || isAdmin[msg.sender] || isMilesOperator[msg.sender], "Not miles operator");
        _;
    }
    
    modifier onlyEditor() {
        require(msg.sender == owner || isAdmin[msg.sender] || isEditor[msg.sender], "Not editor");
        _;
    }
    
    modifier tokenExists(uint256 tokenId) {
        require(_owners[tokenId] != address(0), "Token does not exist");
        _;
    }
    
    // ============ Constructor ============
    
    constructor() {
        owner = msg.sender;
        isAdmin[msg.sender] = true;
        
        // Initialize default tiers: 0=Economy, 500=Business, 1500=First, 3000=Captain
        tierThresholds.push(0);
        tierThresholds.push(500);
        tierThresholds.push(1500);
        tierThresholds.push(3000);
    }
    
    // ============ Receive Function ============
    
    /**
     * @notice Receive function to handle direct ETH transfers (triggers mint)
     */
    receive() external payable {
        mint();
    }
    
    /**
     * @notice Fallback function
     */
    fallback() external payable {
        revert("Function does not exist");
    }
    
    // ============ Minting Functions ============
    
    /**
     * @notice Mint a boarding pass NFT
     * @dev One per address, soulbound, assigns sequential seat
     */
    function mint() public payable {
        require(mintingActive, "Minting is not active");
        require(!hasMinted[msg.sender], "Already minted");
        require(msg.value >= mintPrice, "Insufficient payment");
        
        uint256 tokenId = totalSupply + 1;
        totalSupply = tokenId;
        
        // Assign seat (1A, 1B, 1C... 1J, 2A, 2B... 2J) - Wide body 3-4-3 config
        uint256 seatRow = ((tokenId - 1) / 10) + 1;
        uint256 seatIndex = (tokenId - 1) % 10;
        string[10] memory letters = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"];  // 10 seats
        
        BoardingPass storage pass = boardingPasses[tokenId];
        pass.seatRow = seatRow;
        pass.seatLetter = letters[seatIndex];
        pass.miles = initialMiles;
        pass.milesEarned = initialMiles;
        pass.milesSpent = 0;
        pass.mintedAt = block.timestamp;
        pass.lastUpdated = block.timestamp;
        pass.bannedUntil = 0;
        
        _mint(msg.sender, tokenId);
        hasMinted[msg.sender] = true;
        addressToTokenId[msg.sender] = tokenId;
        
        // Refund excess payment
        if (msg.value > mintPrice) {
            (bool success, ) = msg.sender.call{value: msg.value - mintPrice}("");
            require(success, "Refund failed");
        }
    }
    
    /**
     * @notice Admin mint for specific address
     */
    function adminMint(address to) external onlyAdmin {
        require(!hasMinted[to], "Already minted");
        
        uint256 tokenId = totalSupply + 1;
        totalSupply = tokenId;
        
        // Assign seat (1A, 1B, 1C... 1J, 2A, 2B... 2J) - Wide body 3-4-3 config
        uint256 seatRow = ((tokenId - 1) / 10) + 1;
        uint256 seatIndex = (tokenId - 1) % 10;
        string[10] memory letters = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"];  // 10 seats
        
        BoardingPass storage pass = boardingPasses[tokenId];
        pass.seatRow = seatRow;
        pass.seatLetter = letters[seatIndex];
        pass.miles = initialMiles;
        pass.milesEarned = initialMiles;
        pass.milesSpent = 0;
        pass.mintedAt = block.timestamp;
        pass.lastUpdated = block.timestamp;
        pass.bannedUntil = 0;
        
        _mint(to, tokenId);
        hasMinted[to] = true;
        addressToTokenId[to] = tokenId;
    }
    
    // ============ Miles Management ============
    
    /**
     * @notice Add miles to a boarding pass
     */
    function addMiles(uint256 tokenId, uint256 amount) external onlyMilesOperator tokenExists(tokenId) {
        BoardingPass storage pass = boardingPasses[tokenId];
        uint256 oldMiles = pass.miles;
        
        pass.miles += amount;
        pass.milesEarned += amount;
        pass.lastUpdated = block.timestamp;
        
        emit MilesUpdated(tokenId, oldMiles, pass.miles);
    }
    
    /**
     * @notice Deduct miles from a boarding pass
     */
    function deductMiles(uint256 tokenId, uint256 amount) external onlyMilesOperator tokenExists(tokenId) {
        BoardingPass storage pass = boardingPasses[tokenId];
        require(pass.miles >= amount, "Insufficient miles");
        
        uint256 oldMiles = pass.miles;
        
        pass.miles -= amount;
        pass.milesSpent += amount;
        pass.lastUpdated = block.timestamp;
        
        emit MilesUpdated(tokenId, oldMiles, pass.miles);
    }
    
    /**
     * @notice Set miles (absolute value)
     */
    function setMiles(uint256 tokenId, uint256 newMiles) external onlyMilesOperator tokenExists(tokenId) {
        BoardingPass storage pass = boardingPasses[tokenId];
        uint256 oldMiles = pass.miles;
        
        pass.miles = newMiles;
        pass.lastUpdated = block.timestamp;
        
        // Adjust earned/spent totals
        if (newMiles > oldMiles) {
            pass.milesEarned += (newMiles - oldMiles);
        } else if (newMiles < oldMiles) {
            pass.milesSpent += (oldMiles - newMiles);
        }
        
        emit MilesUpdated(tokenId, oldMiles, pass.miles);
    }
    
    /**
     * @notice Check if token is locked (ERC-5192)
     * @dev All tokens are permanently soulbound
     * @param tokenId Token ID to check
     * @return Always returns true for existing tokens
     */
    function locked(uint256 tokenId) external view returns (bool) {
        require(_owners[tokenId] != address(0), "Token does not exist");
        return true;
    }
    
    /**
     * @notice Get user's current tier based on miles
     * @param user Address of the user
     * @return Tier index (0=first tier, 1=second tier, etc.)
     */
    function getUserTier(address user) public view returns (uint256) {
        require(hasMinted[user], "No NFT minted");
        uint256 tokenId = addressToTokenId[user];
        uint256 miles = boardingPasses[tokenId].milesEarned;
        
        // Iterate backwards through tiers to find the highest qualifying tier
        for (uint256 i = tierThresholds.length; i > 0; i--) {
            if (miles >= tierThresholds[i - 1]) {
                return i - 1;
            }
        }
        return 0;
    }
    
    /**
     * @notice Get user's tier by token ID
     * @param tokenId Token ID
     * @return Tier index
     */
    function getTierByTokenId(uint256 tokenId) public view tokenExists(tokenId) returns (uint256) {
        uint256 miles = boardingPasses[tokenId].milesEarned;
        
        for (uint256 i = tierThresholds.length; i > 0; i--) {
            if (miles >= tierThresholds[i - 1]) {
                return i - 1;
            }
        }
        return 0;
    }

    function getSeatString(uint256 tokenId) public view returns (string memory) {
        BoardingPass storage pass = boardingPasses[tokenId];
        return string(abi.encodePacked(_toString(pass.seatRow), pass.seatLetter));
        // Returns "1A", "2C", etc.
    }
    
    /**
     * @notice Get all tier thresholds
     * @return Array of tier thresholds
     */
    function getTierThresholds() public view returns (uint256[] memory) {
        return tierThresholds;
    }
    
    /**
     * @notice Get number of tiers
     * @return Number of tiers
     */
    function getTierCount() public view returns (uint256) {
        return tierThresholds.length;
    }
    
    // ============ User Metadata Management ============
    
    /**
     * @notice Update user text metadata
     * @param tokenId Token ID
     * @param key Field identifier (use keccak256 hash for common fields like "name", "discord", etc.)
     * @param value Text value to store
     */
    function setCustomText(uint256 tokenId, bytes32 key, string memory value) 
        external 
        onlyEditor 
        tokenExists(tokenId) 
    {
        boardingPasses[tokenId].customText[key] = value;
        emit MetadataUpdated(tokenId, string(abi.encodePacked(key)));
    }
    
    /**
     * @notice Update user number metadata
     * @param tokenId Token ID  
     * @param key Field identifier
     * @param value Number value to store
     */
    function setCustomNumber(uint256 tokenId, bytes32 key, uint256 value) 
        external 
        onlyEditor 
        tokenExists(tokenId) 
    {
        boardingPasses[tokenId].customNumber[key] = value;
        emit MetadataUpdated(tokenId, string(abi.encodePacked(key)));
    }
    
    /**
     * @notice Get custom text metadata
     */
    function getCustomText(uint256 tokenId, bytes32 key) external view returns (string memory) {
        return boardingPasses[tokenId].customText[key];
    }
    
    /**
     * @notice Get custom number metadata
     */
    function getCustomNumber(uint256 tokenId, bytes32 key) external view returns (uint256) {
        return boardingPasses[tokenId].customNumber[key];
    }
    
    // ============ Ban Management ============
    
    /**
     * @notice Ban a user until a specific timestamp
     */
    function banUser(uint256 tokenId, uint256 until) external onlyAdmin tokenExists(tokenId) {
        require(until > block.timestamp, "Ban time must be in future");
        boardingPasses[tokenId].bannedUntil = until;
        emit UserBanned(tokenId, until);
    }
    
    /**
     * @notice Unban a user
     */
    function unbanUser(uint256 tokenId) external onlyAdmin tokenExists(tokenId) {
        boardingPasses[tokenId].bannedUntil = 0;
        emit UserUnbanned(tokenId);
    }
    
    /**
     * @notice Check if user is currently banned
     */
    function isBanned(uint256 tokenId) external view returns (bool) {
        return boardingPasses[tokenId].bannedUntil > block.timestamp;
    }
    
    // ============ Admin Functions ============
    
    /**
     * @notice Toggle minting on/off
     */
    function toggleMinting() external onlyAdmin {
        mintingActive = !mintingActive;
        emit MintingToggled(mintingActive);
    }
    
    /**
     * @notice Set mint price
     */
    function setMintPrice(uint256 newPrice) external onlyAdmin {
        uint256 oldPrice = mintPrice;
        mintPrice = newPrice;
        emit MintPriceUpdated(oldPrice, newPrice);
    }
    
    /**
     * @notice Set initial miles for new mints
     */
    function setInitialMiles(uint256 newInitialMiles) external onlyAdmin {
        initialMiles = newInitialMiles;
    }
    
    /**
     * @notice Update base URI for metadata
     */
    function setBaseURI(string memory newURI) external onlyAdmin {
        baseURI = newURI;
        emit BaseURIUpdated(newURI);
        // Notify all tokens updated
        if (totalSupply > 0) {
            emit BatchMetadataUpdate(1, totalSupply);
        }
    }

    function setIndividualURI(bool enabled) external onlyAdmin {
        individualURI = enabled;
        // Notify all tokens updated (1 to totalSupply)
        if (totalSupply > 0) {
            emit BatchMetadataUpdate(1, totalSupply);
        }
    }

    function setStaticFilename(string memory filename) external onlyAdmin {
        staticFilename = filename;
        // Only affects tokens if in static mode
        if (!individualURI && totalSupply > 0) {
            emit BatchMetadataUpdate(1, totalSupply);
        }
    }

    function setContractURI(string memory uri) external onlyAdmin {
        contractMetadataURI = uri;
        emit ContractURIUpdated(uri); // Optional but good practice
    }
    
    /**
     * @notice Update tier thresholds
     * @param newThresholds Array of new thresholds (must be in ascending order)
     */
    function setTierThresholds(uint256[] memory newThresholds) external onlyAdmin {
        require(newThresholds.length > 0, "Need at least one tier");
        require(newThresholds[0] == 0, "First tier must start at 0");
        
        // Verify ascending order
        for (uint256 i = 1; i < newThresholds.length; i++) {
            require(newThresholds[i] > newThresholds[i - 1], "Thresholds must be in ascending order");
        }
        
        // Clear existing thresholds
        delete tierThresholds;
        
        // Set new thresholds
        for (uint256 i = 0; i < newThresholds.length; i++) {
            tierThresholds.push(newThresholds[i]);
        }
        
        emit TierThresholdsUpdated(newThresholds);
    }
    
    // ============ Role Management ============
    
    function addAdmin(address admin) external onlyAdmin {
        isAdmin[admin] = true;
        emit AdminAdded(admin);
    }
    
    function removeAdmin(address admin) external onlyOwner {
        isAdmin[admin] = false;
        emit AdminRemoved(admin);
    }
    
    function addMilesOperator(address operator) external onlyAdmin {
        isMilesOperator[operator] = true;
        emit MilesOperatorAdded(operator);
    }
    
    function removeMilesOperator(address operator) external onlyAdmin {
        isMilesOperator[operator] = false;
        emit MilesOperatorRemoved(operator);
    }
    
    function addEditor(address editor) external onlyAdmin {
        isEditor[editor] = true;
        emit EditorAdded(editor);
    }
    
    function removeEditor(address editor) external onlyAdmin {
        isEditor[editor] = false;
        emit EditorRemoved(editor);
    }
    
    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "Invalid address");
        owner = newOwner;
    }

    /**
    * @notice Check all roles for an address
    * @param account Address to check
    * @return isOwner_ Whether the address is the owner
    * @return isAdmin_ Whether the address is an admin
    * @return isMilesOperator_ Whether the address is a miles operator
    * @return isEditor_ Whether the address is an editor
    */
    function getRoles(address account) external view returns (
        bool isOwner_,
        bool isAdmin_,
        bool isMilesOperator_,
        bool isEditor_
    ) {
        return (
            account == owner,
            isAdmin[account],
            isMilesOperator[account],
            isEditor[account]
        );
    }
    
    // ============ Emergency Functions ============
    
    /**
     * @notice Withdraw ETH from contract
     */
    function withdrawETH() external onlyOwner {
        uint256 balance = address(this).balance;
        require(balance > 0, "No ETH to withdraw");
        (bool success, ) = owner.call{value: balance}("");
        require(success, "Withdrawal failed");
    }
    
    /**
     * @notice Recover accidentally sent ERC20 tokens
     * @dev Uses low-level call to handle ALL tokens (standard and non-standard like USDT)
     */
    function recoverERC20(address token, uint256 amount) external onlyOwner {
        require(token != address(0), "Invalid token");
        
        // Use low-level call to handle tokens that don't return bool (like USDT)
        (bool success, bytes memory data) = token.call(
            abi.encodeWithSelector(IERC20.transfer.selector, owner, amount)
        );
        
        // Check success: either call succeeded with no return data, or returned true
        require(
            success && (data.length == 0 || abi.decode(data, (bool))),
            "Token transfer failed"
        );
    }
    
    // ============ View Functions ============
    
    /**
     * @notice Get full boarding pass data
     */
    function getBoardingPass(uint256 tokenId) external view returns (
        string memory seat,
        uint256 miles,
        uint256 milesEarned,
        uint256 milesSpent,
        uint256 tier,
        uint256 mintedAt,
        uint256 lastUpdated,
        bool banned,
        uint256 bannedUntil
    ) {
        require(_owners[tokenId] != address(0), "Token does not exist");
        BoardingPass storage pass = boardingPasses[tokenId];
        uint256 currentTier = getTierByTokenId(tokenId);
        string memory _seat = getSeatString(tokenId);
        
        return (
            _seat,
            pass.miles,
            pass.milesEarned,
            pass.milesSpent,
            currentTier,
            pass.mintedAt,
            pass.lastUpdated,
            pass.bannedUntil > block.timestamp,
            pass.bannedUntil
        );
    }
    
    /**
     * @notice Get token ID for an address
     */
    function getTokenIdByAddress(address addr) external view returns (uint256) {
        require(hasMinted[addr], "Address has not minted");
        return addressToTokenId[addr];
    }
    
    // ============ Soulbound Implementation ============
    
    /**
     * @notice Override transfer functions to make tokens soulbound
     */
    function _beforeTokenTransfer(address from, address to, uint256 tokenId) internal view {
        require(from == address(0) || to == address(0), "Soulbound: transfers disabled");
    }
    
    function transferFrom(address from, address to, uint256 tokenId) public override {
        _beforeTokenTransfer(from, to, tokenId);
        revert("Soulbound: transfers disabled");
    }
    
    function safeTransferFrom(address from, address to, uint256 tokenId) public override {
        _beforeTokenTransfer(from, to, tokenId);
        revert("Soulbound: transfers disabled");
    }
    
    function safeTransferFrom(address from, address to, uint256 tokenId, bytes memory data) public override {
        _beforeTokenTransfer(from, to, tokenId);
        revert("Soulbound: transfers disabled");
    }
    
    // ============ ERC721 Implementation ============
    
    function _mint(address to, uint256 tokenId) internal {
        require(to != address(0), "Cannot mint to zero address");
        require(_owners[tokenId] == address(0), "Token already exists");
        
        _balances[to] += 1;
        _owners[tokenId] = to;
        
        emit Transfer(address(0), to, tokenId);
        emit Locked(tokenId); // ERC-5192: Token is permanently locked
    }
    
    function balanceOf(address _owner) public view override returns (uint256) {
        require(_owner != address(0), "Invalid address");
        return _balances[_owner];
    }
    
    function ownerOf(uint256 tokenId) public view override returns (address) {
        address _owner = _owners[tokenId];
        require(_owner != address(0), "Token does not exist");
        return _owner;
    }
    
    function approve(address to, uint256 tokenId) public override {
        revert("Soulbound: approvals disabled");
    }
    
    function getApproved(uint256 tokenId) public view override returns (address) {
        require(_owners[tokenId] != address(0), "Token does not exist");
        return address(0); // Always return zero address for soulbound
    }
    
    function setApprovalForAll(address operator, bool approved) public override {
        revert("Soulbound: approvals disabled");
    }
    
    function isApprovedForAll(address _owner, address _operator) public view override returns (bool) {
        return false; // Always false for soulbound
    }
    /*
    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        require(_owners[tokenId] != address(0), "Token does not exist");
        
        if (bytes(baseURI).length == 0) {
            return "";
        }
        
        return string(abi.encodePacked(baseURI, _toString(tokenId), ".json"));
    }*/

    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        require(_owners[tokenId] != address(0), "Token does not exist");
        if (bytes(baseURI).length == 0) return "";
        
        if (individualURI) {
            return string(abi.encodePacked(baseURI, _toString(tokenId), ".json"));
        } else {
            return string(abi.encodePacked(baseURI, staticFilename));
        }
    }

    function contractURI() external view returns (string memory) {
        return contractMetadataURI;
    }
    
    function supportsInterface(bytes4 interfaceId) public pure override returns (bool) {
        return
            interfaceId == type(IERC721).interfaceId ||
            interfaceId == type(IERC721Metadata).interfaceId ||
            interfaceId == 0xb45a3c0e || // ERC-5192 (Soulbound)
            interfaceId == 0x49064906 || // ERC-4906 (Metadata Update)
            interfaceId == 0x01ffc9a7; // ERC165
    }
    
    /**
     * @notice Get token by index (for enumeration)
     */
    function tokenByIndex(uint256 index) external view returns (uint256) {
        require(index < totalSupply, "Index out of bounds");
        return index + 1; // Token IDs start at 1
    }
    
    /**
     * @notice Get token of owner by index
     * @dev Since each address can only have one token, index must be 0
     */
    function tokenOfOwnerByIndex(address _owner, uint256 index) external view returns (uint256) {
        require(index == 0, "Index out of bounds");
        require(hasMinted[_owner], "Owner has no tokens");
        return addressToTokenId[_owner];
    }
    
    // ============ Utility Functions ============
    
    function _toString(uint256 value) internal pure returns (string memory) {
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