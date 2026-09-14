pragma solidity ^0.8.20;

contract ValueForwarder {
    event Forwarded(address indexed caller, address indexed to, uint256 amount);

    receive() external payable {}

    function forwardTo(address payable to) external payable {
        require(msg.value > 0, "no value");
        (bool ok,) = to.call{value: msg.value}("");
        require(ok, "forward failed");
        emit Forwarded(msg.sender, to, msg.value);
    }
}
