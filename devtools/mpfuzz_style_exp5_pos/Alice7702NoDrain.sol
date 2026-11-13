// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Phase 1 control: ABI-compatible with Alice7702Drain, with no value transfer.
contract Alice7702NoDrain {
    event NoDrain(address indexed caller, address indexed receiver, uint256 balance);

    receive() external payable {}

    fallback() external payable {}

    function drainAll(address payable receiver) external {
        emit NoDrain(msg.sender, receiver, address(this).balance);
    }
}
