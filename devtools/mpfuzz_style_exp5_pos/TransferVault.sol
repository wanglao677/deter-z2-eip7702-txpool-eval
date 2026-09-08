pragma solidity ^0.8.20;

contract TransferVault {
    constructor() payable {}

    receive() external payable {}

    function transferTo(address payable to, uint256 amount) external {
        require(address(this).balance >= amount, "insufficient contract balance");
        (bool ok,) = to.call{value: amount}("");
        require(ok, "transfer failed");
    }
}
