// Copyright 2026 The go-ethereum Authors
// This file is part of the go-ethereum library.
//
// Batch helper for the exp5 EIP-7702 scale experiment. It signs and broadcasts
// large groups of setup and workload transactions using go-ethereum's native
// transaction types, keeping the Python orchestrator focused on experiment
// structure and reporting.

package main

import (
	"bufio"
	"context"
	"crypto/ecdsa"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"math/big"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/ethclient"
	"github.com/holiman/uint256"
)

type accountSpec struct {
	Label      string  `json:"label"`
	Address    string  `json:"address"`
	PrivateKey string  `json:"private_key"`
	Nonce      *uint64 `json:"nonce,omitempty"`
}

type sentRecord struct {
	Kind                 string `json:"kind"`
	Label                string `json:"label"`
	Sender               string `json:"sender"`
	Nonce                uint64 `json:"nonce"`
	GasPriceWei          string `json:"gasPriceWei,omitempty"`
	CalldataPaddingBytes *int   `json:"calldataPaddingBytes,omitempty"`
	Hash                 string `json:"hash"`
	Error                string `json:"error,omitempty"`
}

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

func mustBigWei(name, value string) *big.Int {
	n, ok := new(big.Int).SetString(value, 10)
	if !ok || n.Sign() < 0 {
		log.Fatalf("invalid %s: %q", name, value)
	}
	return n
}

func mustKey(hexkey string) *ecdsa.PrivateKey {
	key, err := crypto.HexToECDSA(strings.TrimPrefix(hexkey, "0x"))
	if err != nil {
		log.Fatalf("invalid private key: %v", err)
	}
	return key
}

func readAccounts(path string) []accountSpec {
	file, err := os.Open(path)
	if err != nil {
		log.Fatalf("open accounts file: %v", err)
	}
	defer file.Close()

	var accounts []accountSpec
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var account accountSpec
		if err := json.Unmarshal([]byte(line), &account); err != nil {
			log.Fatalf("decode account JSONL: %v", err)
		}
		if account.Label == "" || account.PrivateKey == "" || !common.IsHexAddress(account.Address) {
			log.Fatalf("invalid account spec: %s", line)
		}
		accounts = append(accounts, account)
	}
	if err := scanner.Err(); err != nil {
		log.Fatalf("scan accounts file: %v", err)
	}
	return accounts
}

func readGasPrices(path string) []*big.Int {
	if path == "" {
		return nil
	}
	file, err := os.Open(path)
	if err != nil {
		log.Fatalf("open gas price list: %v", err)
	}
	defer file.Close()

	var prices []*big.Int
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		price := mustBigWei("gas-price-wei-list entry", line)
		prices = append(prices, price)
	}
	if err := scanner.Err(); err != nil {
		log.Fatalf("scan gas price list: %v", err)
	}
	return prices
}

func printRecord(record sentRecord) {
	encoded, err := json.Marshal(record)
	if err != nil {
		log.Fatalf("encode sent record: %v", err)
	}
	fmt.Println(string(encoded))
}

func writeRecord(writer *bufio.Writer, record sentRecord) {
	encoded, err := json.Marshal(record)
	if err != nil {
		log.Fatalf("encode sent record: %v", err)
	}
	if _, err := writer.Write(encoded); err != nil {
		log.Fatalf("write pre-signed record: %v", err)
	}
	if err := writer.WriteByte('\n'); err != nil {
		log.Fatalf("write pre-signed record newline: %v", err)
	}
}

func readRecords(path string) []sentRecord {
	file, err := os.Open(path)
	if err != nil {
		log.Fatalf("open pre-signed records: %v", err)
	}
	defer file.Close()
	var records []sentRecord
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var record sentRecord
		if err := json.Unmarshal([]byte(line), &record); err != nil {
			log.Fatalf("decode pre-signed record: %v", err)
		}
		records = append(records, record)
	}
	if err := scanner.Err(); err != nil {
		log.Fatalf("scan pre-signed records: %v", err)
	}
	return records
}

func writeRawTransaction(writer *bufio.Writer, tx *types.Transaction) {
	raw, err := tx.MarshalBinary()
	if err != nil {
		log.Fatalf("marshal raw transaction: %v", err)
	}
	if len(raw) > int(^uint32(0)) {
		log.Fatalf("raw transaction too large: %d bytes", len(raw))
	}
	if err := binary.Write(writer, binary.BigEndian, uint32(len(raw))); err != nil {
		log.Fatalf("write raw transaction length: %v", err)
	}
	if _, err := writer.Write(raw); err != nil {
		log.Fatalf("write raw transaction: %v", err)
	}
}

func readRawTransaction(reader *bufio.Reader) ([]byte, error) {
	var size uint32
	if err := binary.Read(reader, binary.BigEndian, &size); err != nil {
		return nil, err
	}
	raw := make([]byte, size)
	if _, err := io.ReadFull(reader, raw); err != nil {
		return nil, err
	}
	return raw, nil
}

func decodeRawTransaction(raw []byte) (*types.Transaction, error) {
	var tx types.Transaction
	if err := tx.UnmarshalBinary(raw); err != nil {
		return nil, err
	}
	return &tx, nil
}

type presignOutput struct {
	rawFile       *os.File
	rawWriter     *bufio.Writer
	recordsFile   *os.File
	recordsWriter *bufio.Writer
}

func openPresignOutput(rawPath string, recordsPath string) *presignOutput {
	rawFile, err := os.Create(rawPath)
	if err != nil {
		log.Fatalf("create pre-signed raw tx file: %v", err)
	}
	recordsFile, err := os.Create(recordsPath)
	if err != nil {
		_ = rawFile.Close()
		log.Fatalf("create pre-signed records file: %v", err)
	}
	return &presignOutput{
		rawFile:       rawFile,
		rawWriter:     bufio.NewWriterSize(rawFile, 1024*1024),
		recordsFile:   recordsFile,
		recordsWriter: bufio.NewWriterSize(recordsFile, 1024*1024),
	}
}

func (output *presignOutput) close() {
	if output == nil {
		return
	}
	if err := output.rawWriter.Flush(); err != nil {
		log.Fatalf("flush pre-signed raw tx file: %v", err)
	}
	if err := output.recordsWriter.Flush(); err != nil {
		log.Fatalf("flush pre-signed records file: %v", err)
	}
	if err := output.rawFile.Close(); err != nil {
		log.Fatalf("close pre-signed raw tx file: %v", err)
	}
	if err := output.recordsFile.Close(); err != nil {
		log.Fatalf("close pre-signed records file: %v", err)
	}
}

func runIndexedWorkers(total int, workers int, fn func(int)) {
	if total <= 0 {
		return
	}
	if workers < 1 {
		workers = 1
	}
	if workers > total {
		workers = total
	}
	jobs := make(chan int)
	var wg sync.WaitGroup
	for worker := 0; worker < workers; worker++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for index := range jobs {
				fn(index)
			}
		}()
	}
	for index := 0; index < total; index++ {
		jobs <- index
	}
	close(jobs)
	wg.Wait()
}

func waitReceipts(ctx context.Context, client *ethclient.Client, hashes []common.Hash, timeout time.Duration) {
	if timeout <= 0 || len(hashes) == 0 {
		return
	}
	deadline := time.Now().Add(timeout)
	remaining := make(map[common.Hash]bool, len(hashes))
	for _, hash := range hashes {
		remaining[hash] = true
	}
	for len(remaining) > 0 && time.Now().Before(deadline) {
		for hash := range remaining {
			receipt, err := client.TransactionReceipt(ctx, hash)
			if err == nil && receipt != nil {
				delete(remaining, hash)
			}
		}
		if len(remaining) > 0 {
			time.Sleep(500 * time.Millisecond)
		}
	}
	if len(remaining) > 0 {
		log.Printf("warning: timed out waiting for %d receipts", len(remaining))
	}
}

func signLegacy(signer types.Signer, key *ecdsa.PrivateKey, nonce uint64, to common.Address, value *big.Int, gas uint64, gasPrice *big.Int, data []byte) (*types.Transaction, error) {
	tx := types.NewTransaction(nonce, to, value, gas, gasPrice, data)
	return types.SignTx(tx, signer, key)
}

func signAndSendLegacy(ctx context.Context, client *ethclient.Client, signer types.Signer, key *ecdsa.PrivateKey, nonce uint64, to common.Address, value *big.Int, gas uint64, gasPrice *big.Int, data []byte) (*types.Transaction, error) {
	signed, err := signLegacy(signer, key, nonce, to, value, gas, gasPrice, data)
	if err == nil {
		err = client.SendTransaction(ctx, signed)
	}
	return signed, err
}

func startingNonce(ctx context.Context, client *ethclient.Client, account accountSpec, from common.Address) (uint64, error) {
	if account.Nonce != nil {
		return *account.Nonce, nil
	}
	return client.PendingNonceAt(ctx, from)
}

func main() {
	mode := flag.String("mode", "", "fund, setcode, normal, or attack")
	rpcURL := flag.String("rpc", "", "EL HTTP RPC URL")
	accountsPath := flag.String("accounts", "", "JSONL account file")
	sponsorKeyHex := flag.String("sponsor-key", "", "private key paying setup transactions")
	delegateAddressHex := flag.String("delegate-address", "", "implementation contract address")
	receiverHex := flag.String("receiver", "", "receiver used in drainAll(address)")
	valueWei := flag.String("value-wei", "0", "transaction value in wei")
	gasPriceWei := flag.String("gas-price-wei", "1000000000", "legacy gas price in wei")
	gasPriceListPath := flag.String("gas-price-wei-list", "", "optional newline-delimited gas price in wei per workload transaction")
	gasFeeCapWei := flag.String("gas-fee-cap-wei", "1000000000", "set-code max fee per gas in wei")
	gasTipCapWei := flag.String("gas-tip-cap-wei", "1000000000", "set-code priority fee per gas in wei")
	gasLimit := flag.Uint64("gas", 21000, "gas limit")
	txsPerSender := flag.Int("txs-per-sender", 1, "number of workload transactions per sender")
	calldataBytes := flag.Int("calldata-bytes", 0, "number of zero bytes to append to workload transaction calldata")
	firstCalldataBytes := flag.Int("first-calldata-bytes", -1, "optional zero-byte calldata length for the first workload transaction per sender in this helper invocation")
	sendOrder := flag.String("send-order", "account", "workload send order: account or round-robin")
	sendWorkers := flag.Int("send-workers", 1, "parallel sender workers for normal/attack workload broadcasts; nonce order is preserved per sender")
	presignRawOutput := flag.String("presign-raw-output", "", "write signed raw workload transactions to this binary file without broadcasting")
	presignRecordsOutput := flag.String("presign-records-output", "", "write lightweight JSONL metadata for --presign-raw-output")
	broadcastRawInput := flag.String("broadcast-raw-input", "", "broadcast signed raw workload transactions from this binary file")
	broadcastRecordsInput := flag.String("broadcast-records-input", "", "read lightweight JSONL metadata for --broadcast-raw-input")
	waitTimeout := flag.Duration("wait", 0, "optional receipt wait timeout")
	timeout := flag.Duration("timeout", 10*time.Minute, "RPC timeout")
	flag.Parse()

	if *mode == "" || *rpcURL == "" || *accountsPath == "" {
		log.Fatalf("--mode, --rpc, and --accounts are required")
	}
	accounts := readAccounts(*accountsPath)
	if len(accounts) == 0 {
		log.Fatalf("no accounts in %s", *accountsPath)
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
	signer := types.LatestSignerForChainID(chainIDBig)
	gasPrice := mustBigWei("gas-price-wei", *gasPriceWei)
	gasPriceList := readGasPrices(*gasPriceListPath)
	value := mustBigWei("value-wei", *valueWei)
	if *calldataBytes < 0 {
		log.Fatalf("--calldata-bytes must be non-negative")
	}
	if *firstCalldataBytes < -1 {
		log.Fatalf("--first-calldata-bytes must be -1 or non-negative")
	}
	if *sendOrder != "account" && *sendOrder != "round-robin" {
		log.Fatalf("--send-order must be account or round-robin")
	}
	if *sendWorkers < 1 {
		log.Fatalf("--send-workers must be positive")
	}
	var sent []common.Hash
	var outputMu sync.Mutex
	presignMode := *presignRawOutput != "" || *presignRecordsOutput != ""
	broadcastMode := *broadcastRawInput != "" || *broadcastRecordsInput != ""
	if presignMode && (*presignRawOutput == "" || *presignRecordsOutput == "") {
		log.Fatalf("--presign-raw-output and --presign-records-output must be provided together")
	}
	if broadcastMode && (*broadcastRawInput == "" || *broadcastRecordsInput == "") {
		log.Fatalf("--broadcast-raw-input and --broadcast-records-input must be provided together")
	}
	if presignMode && broadcastMode {
		log.Fatalf("pre-signing and broadcasting inputs cannot be used in the same helper invocation")
	}
	emitRecord := func(record sentRecord, tx *types.Transaction, err error) {
		if err != nil {
			record.Error = err.Error()
		} else if tx != nil {
			record.Hash = tx.Hash().Hex()
		}
		outputMu.Lock()
		if tx != nil && record.Error == "" {
			sent = append(sent, tx.Hash())
		}
		printRecord(record)
		outputMu.Unlock()
	}
	gasPriceEntriesUsed := 0
	prepareWorkloadGasPrices := func(senderCount int) {
		if len(gasPriceList) == 0 {
			return
		}
		needed := senderCount * *txsPerSender
		if needed > len(gasPriceList) {
			log.Fatalf("gas price list has too few entries: need at least %d, got %d", needed, len(gasPriceList))
		}
		gasPriceEntriesUsed = needed
	}
	gasPriceFor := func(senderIndex int, txIndex int, senderCount int) *big.Int {
		if len(gasPriceList) == 0 {
			return new(big.Int).Set(gasPrice)
		}
		var index int
		if *sendOrder == "round-robin" {
			index = txIndex*senderCount + senderIndex
		} else {
			index = senderIndex*(*txsPerSender) + txIndex
		}
		if index >= len(gasPriceList) {
			log.Fatalf("gas price list has too few entries: need at least %d", index+1)
		}
		return new(big.Int).Set(gasPriceList[index])
	}
	writePresigned := func(recordsWriter *bufio.Writer, rawWriter *bufio.Writer, record sentRecord, tx *types.Transaction) {
		if tx == nil {
			log.Fatalf("cannot pre-sign empty transaction for %s", record.Label)
		}
		record.Hash = tx.Hash().Hex()
		writeRawTransaction(rawWriter, tx)
		writeRecord(recordsWriter, record)
		printRecord(record)
	}
	broadcastPresigned := func(senderCount int) {
		if senderCount <= 0 {
			log.Fatalf("cannot broadcast pre-signed workload with no accounts")
		}
		records := readRecords(*broadcastRecordsInput)
		expected := senderCount * *txsPerSender
		if len(records) != expected {
			log.Fatalf("pre-signed record count mismatch: got %d, expected %d", len(records), expected)
		}
		rawFile, err := os.Open(*broadcastRawInput)
		if err != nil {
			log.Fatalf("open pre-signed raw tx file: %v", err)
		}
		defer rawFile.Close()
		reader := bufio.NewReaderSize(rawFile, 1024*1024)
		sendRaw := func(record sentRecord, raw []byte) {
			tx, err := decodeRawTransaction(raw)
			if err == nil {
				err = client.SendTransaction(ctx, tx)
			}
			record.Error = ""
			emitRecord(record, tx, err)
		}
		if *sendOrder == "round-robin" {
			for txIndex := 0; txIndex < *txsPerSender; txIndex++ {
				raws := make([][]byte, senderCount)
				offset := txIndex * senderCount
				for senderIndex := 0; senderIndex < senderCount; senderIndex++ {
					raw, err := readRawTransaction(reader)
					if err != nil {
						log.Fatalf("read pre-signed raw transaction: %v", err)
					}
					raws[senderIndex] = raw
				}
				runIndexedWorkers(senderCount, *sendWorkers, func(senderIndex int) {
					sendRaw(records[offset+senderIndex], raws[senderIndex])
				})
			}
		} else {
			for senderIndex := 0; senderIndex < senderCount; senderIndex++ {
				for txIndex := 0; txIndex < *txsPerSender; txIndex++ {
					raw, err := readRawTransaction(reader)
					if err != nil {
						log.Fatalf("read pre-signed raw transaction: %v", err)
					}
					sendRaw(records[senderIndex*(*txsPerSender)+txIndex], raw)
				}
			}
		}
		if _, err := readRawTransaction(reader); err != io.EOF {
			if err != nil {
				log.Fatalf("read trailing pre-signed raw transaction: %v", err)
			}
			log.Fatalf("pre-signed raw file has extra transactions")
		}
	}

	switch *mode {
	case "fund":
		if *sponsorKeyHex == "" {
			log.Fatalf("--sponsor-key is required for fund mode")
		}
		sponsorKey := mustKey(*sponsorKeyHex)
		sponsorAddress := crypto.PubkeyToAddress(sponsorKey.PublicKey)
		nonce, err := client.PendingNonceAt(ctx, sponsorAddress)
		if err != nil {
			log.Fatalf("sponsor nonce: %v", err)
		}
		for i, account := range accounts {
			to := common.HexToAddress(account.Address)
			tx, err := signAndSendLegacy(ctx, client, signer, sponsorKey, nonce+uint64(i), to, value, *gasLimit, gasPrice, nil)
			record := sentRecord{Kind: *mode, Label: account.Label, Sender: sponsorAddress.Hex(), Nonce: nonce + uint64(i), GasPriceWei: gasPrice.String()}
			if err != nil {
				record.Error = err.Error()
			} else {
				record.Hash = tx.Hash().Hex()
				sent = append(sent, tx.Hash())
			}
			printRecord(record)
		}
	case "setcode":
		if *sponsorKeyHex == "" || *delegateAddressHex == "" || !common.IsHexAddress(*delegateAddressHex) {
			log.Fatalf("--sponsor-key and valid --delegate-address are required for setcode mode")
		}
		sponsorKey := mustKey(*sponsorKeyHex)
		sponsorAddress := crypto.PubkeyToAddress(sponsorKey.PublicKey)
		sponsorNonce, err := client.PendingNonceAt(ctx, sponsorAddress)
		if err != nil {
			log.Fatalf("sponsor nonce: %v", err)
		}
		for i, account := range accounts {
			authorityKey := mustKey(account.PrivateKey)
			authorityAddress := crypto.PubkeyToAddress(authorityKey.PublicKey)
			authorityNonce, err := client.PendingNonceAt(ctx, authorityAddress)
			if err != nil {
				printRecord(sentRecord{Kind: *mode, Label: account.Label, Sender: authorityAddress.Hex(), Error: err.Error()})
				continue
			}
			auth, err := types.SignSetCode(authorityKey, types.SetCodeAuthorization{
				ChainID: *chainID,
				Address: common.HexToAddress(*delegateAddressHex),
				Nonce:   authorityNonce,
			})
			if err != nil {
				printRecord(sentRecord{Kind: *mode, Label: account.Label, Sender: authorityAddress.Hex(), Error: err.Error()})
				continue
			}
			tx := types.MustSignNewTx(sponsorKey, signer, &types.SetCodeTx{
				ChainID:   chainID,
				Nonce:     sponsorNonce + uint64(i),
				GasTipCap: mustUint256Wei("gas-tip-cap-wei", *gasTipCapWei),
				GasFeeCap: mustUint256Wei("gas-fee-cap-wei", *gasFeeCapWei),
				Gas:       *gasLimit,
				To:        sponsorAddress,
				Value:     uint256.NewInt(0),
				Data:      nil,
				AuthList:  []types.SetCodeAuthorization{auth},
			})
			record := sentRecord{Kind: *mode, Label: account.Label, Sender: authorityAddress.Hex(), Nonce: authorityNonce}
			if err := client.SendTransaction(ctx, tx); err != nil {
				record.Error = err.Error()
			} else {
				record.Hash = tx.Hash().Hex()
				sent = append(sent, tx.Hash())
			}
			printRecord(record)
		}
	case "normal":
		if *txsPerSender <= 0 {
			log.Fatalf("--txs-per-sender must be positive")
		}
		data := make([]byte, *calldataBytes)
		type senderState struct {
			account accountSpec
			key     *ecdsa.PrivateKey
			from    common.Address
			nonce   uint64
			ok      bool
		}
		states := make([]senderState, 0, len(accounts))
		for _, account := range accounts {
			key := mustKey(account.PrivateKey)
			from := crypto.PubkeyToAddress(key.PublicKey)
			nonce, err := startingNonce(ctx, client, account, from)
			if err != nil {
				printRecord(sentRecord{Kind: *mode, Label: account.Label, Sender: from.Hex(), Error: err.Error()})
				continue
			}
			states = append(states, senderState{account: account, key: key, from: from, nonce: nonce, ok: true})
		}
		if broadcastMode {
			broadcastPresigned(len(states))
			break
		}
		var presignOut *presignOutput
		if presignMode {
			presignOut = openPresignOutput(*presignRawOutput, *presignRecordsOutput)
			defer presignOut.close()
		}
		prepareWorkloadGasPrices(len(states))
		sendOne := func(stateIndex int, state senderState, j int) {
			txGasPrice := gasPriceFor(stateIndex, j, len(states))
			label := state.account.Label
			if *txsPerSender > 1 {
				label = fmt.Sprintf("%s-n%d", state.account.Label, j+1)
			}
			record := sentRecord{Kind: *mode, Label: label, Sender: state.from.Hex(), Nonce: state.nonce + uint64(j), GasPriceWei: txGasPrice.String()}
			tx, err := signLegacy(signer, state.key, state.nonce+uint64(j), state.from, value, *gasLimit, txGasPrice, data)
			if presignMode {
				if err != nil {
					emitRecord(record, nil, err)
					return
				}
				writePresigned(presignOut.recordsWriter, presignOut.rawWriter, record, tx)
				return
			}
			if err == nil {
				err = client.SendTransaction(ctx, tx)
			}
			emitRecord(record, tx, err)
		}
		if presignMode && *sendOrder == "round-robin" {
			for j := 0; j < *txsPerSender; j++ {
				for stateIndex, state := range states {
					sendOne(stateIndex, state, j)
				}
			}
		} else if presignMode {
			for stateIndex, state := range states {
				for j := 0; j < *txsPerSender; j++ {
					sendOne(stateIndex, state, j)
				}
			}
		} else if *sendWorkers > 1 && *sendOrder == "round-robin" {
			for j := 0; j < *txsPerSender; j++ {
				txIndex := j
				runIndexedWorkers(len(states), *sendWorkers, func(stateIndex int) {
					sendOne(stateIndex, states[stateIndex], txIndex)
				})
			}
		} else if *sendWorkers > 1 {
			runIndexedWorkers(len(states), *sendWorkers, func(stateIndex int) {
				state := states[stateIndex]
				for j := 0; j < *txsPerSender; j++ {
					sendOne(stateIndex, state, j)
				}
			})
		} else if *sendOrder == "round-robin" {
			for j := 0; j < *txsPerSender; j++ {
				for stateIndex, state := range states {
					sendOne(stateIndex, state, j)
				}
			}
		} else {
			for stateIndex, state := range states {
				for j := 0; j < *txsPerSender; j++ {
					sendOne(stateIndex, state, j)
				}
			}
		}
	case "attack":
		if *receiverHex == "" || !common.IsHexAddress(*receiverHex) {
			log.Fatalf("valid --receiver is required for attack mode")
		}
		if *txsPerSender <= 0 {
			log.Fatalf("--txs-per-sender must be positive")
		}
		receiver := common.HexToAddress(*receiverHex)
		buildAttackData := func(paddingBytes int) []byte {
			data := append([]byte{}, crypto.Keccak256([]byte("drainAll(address)"))[:4]...)
			data = append(data, common.LeftPadBytes(receiver.Bytes(), 32)...)
			if paddingBytes > 0 {
				data = append(data, make([]byte, paddingBytes)...)
			}
			return data
		}
		data := buildAttackData(*calldataBytes)
		firstData := data
		if *firstCalldataBytes >= 0 {
			firstData = buildAttackData(*firstCalldataBytes)
		}
		type senderState struct {
			account accountSpec
			key     *ecdsa.PrivateKey
			from    common.Address
			nonce   uint64
			ok      bool
		}
		states := make([]senderState, 0, len(accounts))
		for _, account := range accounts {
			key := mustKey(account.PrivateKey)
			from := crypto.PubkeyToAddress(key.PublicKey)
			nonce, err := startingNonce(ctx, client, account, from)
			if err != nil {
				printRecord(sentRecord{Kind: *mode, Label: account.Label, Sender: from.Hex(), Error: err.Error()})
				continue
			}
			states = append(states, senderState{account: account, key: key, from: from, nonce: nonce, ok: true})
		}
		if broadcastMode {
			broadcastPresigned(len(states))
			break
		}
		var presignOut *presignOutput
		if presignMode {
			presignOut = openPresignOutput(*presignRawOutput, *presignRecordsOutput)
			defer presignOut.close()
		}
		prepareWorkloadGasPrices(len(states))
		sendOne := func(stateIndex int, state senderState, j int) {
			txGasPrice := gasPriceFor(stateIndex, j, len(states))
			txData := data
			txCalldataBytes := *calldataBytes
			if *firstCalldataBytes >= 0 && j == 0 {
				txData = firstData
				txCalldataBytes = *firstCalldataBytes
			}
			label := fmt.Sprintf("%s-x%d", state.account.Label, j+1)
			record := sentRecord{Kind: *mode, Label: label, Sender: state.from.Hex(), Nonce: state.nonce + uint64(j), GasPriceWei: txGasPrice.String(), CalldataPaddingBytes: &txCalldataBytes}
			tx, err := signLegacy(signer, state.key, state.nonce+uint64(j), state.from, value, *gasLimit, txGasPrice, txData)
			if presignMode {
				if err != nil {
					emitRecord(record, nil, err)
					return
				}
				writePresigned(presignOut.recordsWriter, presignOut.rawWriter, record, tx)
				return
			}
			if err == nil {
				err = client.SendTransaction(ctx, tx)
			}
			emitRecord(record, tx, err)
		}
		if presignMode && *sendOrder == "round-robin" {
			for j := 0; j < *txsPerSender; j++ {
				for stateIndex, state := range states {
					sendOne(stateIndex, state, j)
				}
			}
		} else if presignMode {
			for stateIndex, state := range states {
				for j := 0; j < *txsPerSender; j++ {
					sendOne(stateIndex, state, j)
				}
			}
		} else if *sendWorkers > 1 && *sendOrder == "round-robin" {
			for j := 0; j < *txsPerSender; j++ {
				txIndex := j
				runIndexedWorkers(len(states), *sendWorkers, func(stateIndex int) {
					sendOne(stateIndex, states[stateIndex], txIndex)
				})
			}
		} else if *sendWorkers > 1 {
			runIndexedWorkers(len(states), *sendWorkers, func(stateIndex int) {
				state := states[stateIndex]
				for j := 0; j < *txsPerSender; j++ {
					sendOne(stateIndex, state, j)
				}
			})
		} else if *sendOrder == "round-robin" {
			for j := 0; j < *txsPerSender; j++ {
				for stateIndex, state := range states {
					sendOne(stateIndex, state, j)
				}
			}
		} else {
			for stateIndex, state := range states {
				for j := 0; j < *txsPerSender; j++ {
					sendOne(stateIndex, state, j)
				}
			}
		}
	default:
		log.Fatalf("unknown mode %q", *mode)
	}

	waitReceipts(ctx, client, sent, *waitTimeout)
	if len(gasPriceList) > 0 && gasPriceEntriesUsed < len(gasPriceList) {
		log.Printf("warning: gas price list has %d unused entries", len(gasPriceList)-gasPriceEntriesUsed)
	}
}
