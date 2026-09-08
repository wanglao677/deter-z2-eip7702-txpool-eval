// Copyright 2026 The go-ethereum Authors
// This file is part of the go-ethereum library.
//
// This helper is intentionally small and local to the exp5 devtool. It signs and
// broadcasts a single EIP-7702 set-code transaction using go-ethereum's native
// transaction types, so the Python experiment script does not need to reimplement
// type-4 transaction signing.

package main

import (
	"context"
	"crypto/ecdsa"
	"flag"
	"fmt"
	"log"
	"math/big"
	"strings"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/ethclient"
	"github.com/holiman/uint256"
)

func mustUint256Wei(name, value string) *uint256.Int {
	n, ok := new(big.Int).SetString(value, 10)
	if !ok || n.Sign() < 0 {
		log.Fatalf("invalid %s: %q", name, value)
	}
	u, overflow := uint256.FromBig(n)
	if overflow {
		log.Fatalf("%s overflows uint256: %q", name, value)
	}
	return u
}

func mustKey(hexkey string) *ecdsa.PrivateKey {
	key, err := crypto.HexToECDSA(strings.TrimPrefix(hexkey, "0x"))
	if err != nil {
		log.Fatalf("invalid private key: %v", err)
	}
	return key
}

func main() {
	rpcURL := flag.String("rpc", "", "EL HTTP RPC URL")
	sponsorKeyHex := flag.String("sponsor-key", "", "private key paying for the set-code transaction")
	authorityKeyHex := flag.String("authority-key", "", "private key authorizing the delegated account")
	delegateAddressHex := flag.String("delegate-address", "", "implementation contract address")
	toAddressHex := flag.String("to", "", "transaction destination, usually the authority address")
	gasLimit := flag.Uint64("gas", 250000, "set-code transaction gas limit")
	gasFeeCapWei := flag.String("gas-fee-cap-wei", "10000000000", "EIP-1559 max fee per gas in wei")
	gasTipCapWei := flag.String("gas-tip-cap-wei", "10000000000", "EIP-1559 priority fee per gas in wei")
	timeout := flag.Duration("timeout", 15*time.Second, "RPC timeout")
	flag.Parse()

	if *rpcURL == "" || *sponsorKeyHex == "" || *authorityKeyHex == "" || *delegateAddressHex == "" || *toAddressHex == "" {
		log.Fatalf("--rpc, --sponsor-key, --authority-key, --delegate-address, and --to are required")
	}
	if !common.IsHexAddress(*delegateAddressHex) || !common.IsHexAddress(*toAddressHex) {
		log.Fatalf("delegate-address and to must be hex Ethereum addresses")
	}

	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()

	client, err := ethclient.DialContext(ctx, *rpcURL)
	if err != nil {
		log.Fatalf("dial RPC: %v", err)
	}
	defer client.Close()

	chainIDBig, err := client.ChainID(ctx)
	if err != nil {
		log.Fatalf("chain id: %v", err)
	}
	chainID, overflow := uint256.FromBig(chainIDBig)
	if overflow {
		log.Fatalf("chain id overflows uint256: %v", chainIDBig)
	}

	sponsorKey := mustKey(*sponsorKeyHex)
	authorityKey := mustKey(*authorityKeyHex)
	sponsorAddress := crypto.PubkeyToAddress(sponsorKey.PublicKey)
	authorityAddress := crypto.PubkeyToAddress(authorityKey.PublicKey)

	sponsorNonce, err := client.PendingNonceAt(ctx, sponsorAddress)
	if err != nil {
		log.Fatalf("sponsor nonce: %v", err)
	}
	authorityNonce, err := client.PendingNonceAt(ctx, authorityAddress)
	if err != nil {
		log.Fatalf("authority nonce: %v", err)
	}

	auth, err := types.SignSetCode(authorityKey, types.SetCodeAuthorization{
		ChainID: *chainID,
		Address: common.HexToAddress(*delegateAddressHex),
		Nonce:   authorityNonce,
	})
	if err != nil {
		log.Fatalf("sign authorization: %v", err)
	}

	tx := types.MustSignNewTx(sponsorKey, types.LatestSignerForChainID(chainIDBig), &types.SetCodeTx{
		ChainID:   chainID,
		Nonce:     sponsorNonce,
		GasTipCap: mustUint256Wei("gas-tip-cap-wei", *gasTipCapWei),
		GasFeeCap: mustUint256Wei("gas-fee-cap-wei", *gasFeeCapWei),
		Gas:       *gasLimit,
		To:        common.HexToAddress(*toAddressHex),
		Value:     uint256.NewInt(0),
		Data:      nil,
		AuthList:  []types.SetCodeAuthorization{auth},
	})
	if err := client.SendTransaction(ctx, tx); err != nil {
		log.Fatalf("send set-code tx: %v", err)
	}

	fmt.Printf("hash=%s\n", tx.Hash())
	fmt.Printf("sponsor=%s nonce=%d\n", sponsorAddress, sponsorNonce)
	fmt.Printf("authority=%s authNonce=%d\n", authorityAddress, authorityNonce)
	fmt.Printf("delegate=%s\n", common.HexToAddress(*delegateAddressHex))
}
