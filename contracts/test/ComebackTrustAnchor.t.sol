// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ComebackTrustAnchor} from "../src/ComebackTrustAnchor.sol";

interface Vm {
    struct Log {
        bytes32[] topics;
        bytes data;
        address emitter;
    }

    function deal(address account, uint256 newBalance) external;
    function getRecordedLogs() external returns (Log[] memory logs);
    function recordLogs() external;
    function warp(uint256 newTimestamp) external;
}

contract AnchorActor {
    function claim(ComebackTrustAnchor anchor, bytes12 repoId, bytes32 nonce) external returns (bytes32) {
        return anchor.claim(repoId, nonce);
    }

    function activate(ComebackTrustAnchor anchor, bytes32 key, bytes32 interventionId) external {
        anchor.activate(key, interventionId);
    }
}

contract ComebackTrustAnchorTest {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    bytes12 private constant REPO_ID = hex"0102030405060708090a0b0c";
    bytes32 private constant NONCE = keccak256("repository nonce");
    bytes32 private constant INTERVENTION_ID = keccak256("initial intervention");

    ComebackTrustAnchor private anchor;
    AnchorActor private owner;
    AnchorActor private attacker;

    function setUp() public {
        vm.warp(1_800_000_000);
        anchor = new ComebackTrustAnchor();
        owner = new AnchorActor();
        attacker = new AnchorActor();
    }

    function testAnchorKeyUsesExactDomainChainContractRepoNonceAndOwner() public view {
        bytes32 expected =
            keccak256(abi.encode(anchor.DOMAIN(), block.chainid, address(anchor), REPO_ID, NONCE, address(owner)));

        _assertEqBytes32(anchor.anchorKey(REPO_ID, NONCE, address(owner)), expected, "unexpected anchor key");
    }

    function testDomainMatchesClientSpecification() public view {
        _assertEqBytes32(
            anchor.DOMAIN(), keccak256("COMEBACK_BASE_ANCHOR_V1"), "domain does not match client specification"
        );
    }

    function testDifferentWalletsDeriveDifferentKeys() public view {
        bytes32 ownerKey = anchor.anchorKey(REPO_ID, NONCE, address(owner));
        bytes32 attackerKey = anchor.anchorKey(REPO_ID, NONCE, address(attacker));

        _assertTrue(ownerKey != attackerKey, "wallets shared an anchor key");
    }

    function testClaimBindsKeyToCaller() public {
        bytes32 key = owner.claim(anchor, REPO_ID, NONCE);
        (address recordedOwner, bytes32 initialId, uint64 claimedAt, uint64 activatedAt) = anchor.anchorOf(key);

        _assertEqAddress(recordedOwner, address(owner), "owner not recorded");
        _assertEqBytes32(initialId, bytes32(0), "claim activated anchor");
        _assertEqUint64(claimedAt, uint64(block.timestamp), "claim timestamp mismatch");
        _assertEqUint64(activatedAt, uint64(0), "activation timestamp was set");
    }

    function testClaimEmitsPortableAnchorDetails() public {
        vm.recordLogs();
        bytes32 key = owner.claim(anchor, REPO_ID, NONCE);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        _assertEqUint256(logs.length, 1, "unexpected claim event count");
        _assertEqAddress(logs[0].emitter, address(anchor), "wrong claim event emitter");
        _assertEqUint256(logs[0].topics.length, 4, "wrong claim topic count");
        _assertEqBytes32(
            logs[0].topics[0],
            keccak256("AnchorClaimed(bytes32,bytes12,address,bytes32,uint64)"),
            "wrong claim signature"
        );
        _assertEqBytes32(logs[0].topics[1], key, "wrong claim key topic");
        _assertEqBytes32(logs[0].topics[2], bytes32(REPO_ID), "wrong repository topic");
        _assertEqBytes32(logs[0].topics[3], bytes32(uint256(uint160(address(owner)))), "wrong claim owner topic");
        _assertEqBytes32(
            keccak256(logs[0].data), keccak256(abi.encode(NONCE, uint64(block.timestamp))), "wrong claim event data"
        );
    }

    function testRepeatedClaimIsIdempotentAndPreservesClaimedAt() public {
        bytes32 firstKey = owner.claim(anchor, REPO_ID, NONCE);
        (,, uint64 firstClaimedAt,) = anchor.anchorOf(firstKey);

        vm.warp(block.timestamp + 1 days);
        bytes32 secondKey = owner.claim(anchor, REPO_ID, NONCE);
        (address recordedOwner,, uint64 secondClaimedAt,) = anchor.anchorOf(firstKey);

        _assertEqBytes32(secondKey, firstKey, "repeat claim changed key");
        _assertEqAddress(recordedOwner, address(owner), "repeat claim changed owner");
        _assertEqUint64(secondClaimedAt, firstClaimedAt, "repeat claim changed timestamp");
    }

    function testAnotherWalletCannotOverwriteClaim() public {
        bytes32 ownerKey = owner.claim(anchor, REPO_ID, NONCE);
        bytes32 attackerKey = attacker.claim(anchor, REPO_ID, NONCE);
        (address recordedOwner,,,) = anchor.anchorOf(ownerKey);
        (address recordedAttacker,,,) = anchor.anchorOf(attackerKey);

        _assertTrue(ownerKey != attackerKey, "attacker reached owner key");
        _assertEqAddress(recordedOwner, address(owner), "attacker overwrote owner");
        _assertEqAddress(recordedAttacker, address(attacker), "attacker claim missing");
    }

    function testOwnerCanActivateClaimExactlyOnce() public {
        bytes32 key = owner.claim(anchor, REPO_ID, NONCE);
        vm.warp(block.timestamp + 1);

        owner.activate(anchor, key, INTERVENTION_ID);
        (address recordedOwner, bytes32 initialId, uint64 claimedAt, uint64 activatedAt) = anchor.anchorOf(key);

        _assertEqAddress(recordedOwner, address(owner), "activation changed owner");
        _assertEqBytes32(initialId, INTERVENTION_ID, "intervention not recorded");
        _assertEqUint64(claimedAt, uint64(block.timestamp - 1), "claim timestamp changed");
        _assertEqUint64(activatedAt, uint64(block.timestamp), "activation timestamp mismatch");

        (bool success,) = address(owner)
            .call(abi.encodeCall(AnchorActor.activate, (anchor, key, keccak256("replacement intervention"))));
        _assertTrue(!success, "anchor activated twice");

        (, bytes32 preservedId,, uint64 preservedActivatedAt) = anchor.anchorOf(key);
        _assertEqBytes32(preservedId, INTERVENTION_ID, "second activation replaced intervention");
        _assertEqUint64(preservedActivatedAt, activatedAt, "second activation changed timestamp");
    }

    function testActivationEmitsAnchorInterventionAndOwner() public {
        bytes32 key = owner.claim(anchor, REPO_ID, NONCE);
        vm.recordLogs();

        owner.activate(anchor, key, INTERVENTION_ID);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        _assertEqUint256(logs.length, 1, "unexpected activation event count");
        _assertEqAddress(logs[0].emitter, address(anchor), "wrong activation event emitter");
        _assertEqUint256(logs[0].topics.length, 4, "wrong activation topic count");
        _assertEqBytes32(
            logs[0].topics[0], keccak256("AnchorActivated(bytes32,bytes32,address)"), "wrong activation signature"
        );
        _assertEqBytes32(logs[0].topics[1], key, "wrong activation key topic");
        _assertEqBytes32(logs[0].topics[2], INTERVENTION_ID, "wrong intervention topic");
        _assertEqBytes32(logs[0].topics[3], bytes32(uint256(uint160(address(owner)))), "wrong activation owner topic");
        _assertEqUint256(logs[0].data.length, 0, "activation event unexpectedly had data");
    }

    function testNonOwnerCannotActivateClaim() public {
        bytes32 key = owner.claim(anchor, REPO_ID, NONCE);

        (bool success,) = address(attacker).call(abi.encodeCall(AnchorActor.activate, (anchor, key, INTERVENTION_ID)));
        _assertTrue(!success, "non-owner activated anchor");

        (, bytes32 initialId,, uint64 activatedAt) = anchor.anchorOf(key);
        _assertEqBytes32(initialId, bytes32(0), "non-owner stored intervention");
        _assertEqUint64(activatedAt, uint64(0), "non-owner set activation time");
    }

    function testCannotActivateUnknownAnchor() public {
        bytes32 unknownKey = keccak256("unknown anchor");
        (bool success,) =
            address(owner).call(abi.encodeCall(AnchorActor.activate, (anchor, unknownKey, INTERVENTION_ID)));

        _assertTrue(!success, "unknown anchor activated");
    }

    function testCannotActivateWithZeroInterventionId() public {
        bytes32 key = owner.claim(anchor, REPO_ID, NONCE);
        (bool success,) = address(owner).call(abi.encodeCall(AnchorActor.activate, (anchor, key, bytes32(0))));

        _assertTrue(!success, "zero intervention activated anchor");
    }

    function testContractRejectsEther() public {
        vm.deal(address(this), 1 ether);

        (bool success,) = address(anchor).call{value: 1 wei}("");

        _assertTrue(!success, "anchor accepted ether");
        _assertEqUint256(address(anchor).balance, uint256(0), "anchor retained ether");
    }

    function _assertTrue(bool condition, string memory message) private pure {
        require(condition, message);
    }

    function _assertEqAddress(address actual, address expected, string memory message) private pure {
        require(actual == expected, message);
    }

    function _assertEqBytes32(bytes32 actual, bytes32 expected, string memory message) private pure {
        require(actual == expected, message);
    }

    function _assertEqUint64(uint64 actual, uint64 expected, string memory message) private pure {
        require(actual == expected, message);
    }

    function _assertEqUint256(uint256 actual, uint256 expected, string memory message) private pure {
        require(actual == expected, message);
    }
}
