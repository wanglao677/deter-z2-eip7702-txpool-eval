pragma solidity ^0.8.20;

contract OneShotDrain {
    bool public drained;
    address payable public sink;

    constructor(address payable sink_) payable {
        sink = sink_;
    }

    function drain() external {
        require(!drained, "already drained");
        drained = true;
        (bool ok,) = sink.call{value: address(this).balance}("");
        require(ok, "transfer failed");
    }
}
