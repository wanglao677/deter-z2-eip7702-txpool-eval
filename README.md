# Deter-Z2 EIP-7702 Txpool Evaluation

This repository contains curated local-evaluation artifacts for Deter-Z2, an EIP-7702 transaction-pool attack experiment evaluated against Ethereum execution clients.

The project currently includes scripts for Besu and Erigon Kurtosis PoS devnets, phased workload evaluation, mainnet-derived fee sampling, EIP-7702 block scanning, receipt collection, and paper-style plotting.

## Layout

- `devtools/mpfuzz_style_exp5_pos/`: experiment, collection, and plotting scripts.
- `kurtosis/`: selected Besu/Erigon ethereum-package network parameter files.
- `results/`: curated summaries and selected CSV artifacts.

## Notes

This is a curated export, not a full go-ethereum fork. The Go helper programs are intended to run from a compatible go-ethereum checkout because they import go-ethereum packages.

Private keys, account dumps, local `.env` files, Python virtualenvs, raw Kurtosis data, and large uncurated `scale_runs/` directories are intentionally excluded.
