// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title ComebackTrustAnchor
/// @notice Immutable repository-owner anchors for Comeback supervision.
/// @dev This contract deliberately has no administrator, upgrade path, owner
///      rotation, payable entry point, or external call.
contract ComebackTrustAnchor {
    bytes32 public constant DOMAIN = keccak256("COMEBACK_BASE_ANCHOR_V1");

    struct Anchor {
        address owner;
        bytes32 initialInterventionId;
        uint64 claimedAt;
        uint64 activatedAt;
    }

    mapping(bytes32 anchorKey_ => Anchor anchor) private _anchors;

    error AnchorNotClaimed(bytes32 anchorKey);
    error NotAnchorOwner(bytes32 anchorKey, address caller);
    error ZeroInterventionId();
    error AnchorAlreadyActivated(bytes32 anchorKey);

    event AnchorClaimed(
        bytes32 indexed anchorKey, bytes12 indexed repoId, address indexed owner, bytes32 nonce, uint64 claimedAt
    );
    event AnchorActivated(bytes32 indexed anchorKey, bytes32 indexed initialInterventionId, address indexed owner);

    /// @notice Derive the owner-specific anchor key for a repository.
    function anchorKey(bytes12 repoId, bytes32 nonce, address owner) public view returns (bytes32) {
        return keccak256(abi.encode(DOMAIN, block.chainid, address(this), repoId, nonce, owner));
    }

    /// @notice Claim the caller's owner-specific anchor key.
    /// @dev Repeating the same claim is idempotent and preserves claimedAt.
    function claim(bytes12 repoId, bytes32 nonce) external returns (bytes32 key) {
        key = anchorKey(repoId, nonce, msg.sender);
        Anchor storage anchor = _anchors[key];

        if (anchor.owner == address(0)) {
            uint64 claimedAt = uint64(block.timestamp);
            anchor.owner = msg.sender;
            anchor.claimedAt = claimedAt;
            emit AnchorClaimed(key, repoId, msg.sender, nonce, claimedAt);
        }
    }

    /// @notice Permanently bind the first Sibyl intervention to a claimed anchor.
    function activate(bytes32 key, bytes32 initialInterventionId) external {
        Anchor storage anchor = _anchors[key];

        if (anchor.owner == address(0)) revert AnchorNotClaimed(key);
        if (msg.sender != anchor.owner) revert NotAnchorOwner(key, msg.sender);
        if (initialInterventionId == bytes32(0)) revert ZeroInterventionId();
        if (anchor.initialInterventionId != bytes32(0)) revert AnchorAlreadyActivated(key);

        uint64 activatedAt = uint64(block.timestamp);
        anchor.initialInterventionId = initialInterventionId;
        anchor.activatedAt = activatedAt;

        emit AnchorActivated(key, initialInterventionId, msg.sender);
    }

    /// @notice Read an anchor without exposing mutable storage access.
    function anchorOf(bytes32 key)
        external
        view
        returns (address owner, bytes32 initialInterventionId, uint64 claimedAt, uint64 activatedAt)
    {
        Anchor storage anchor = _anchors[key];
        return (anchor.owner, anchor.initialInterventionId, anchor.claimedAt, anchor.activatedAt);
    }
}
