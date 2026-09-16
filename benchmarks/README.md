# Benchmarks

## `rtl_dataset/`

Twenty real-world RTL designs (CPUs, AES, UART, SPI, I2C, PCIe, an LSTM, …),
kept as a pool of candidates for turning into ASTRA designs. They are **not**
wired into the flow as-is: a design needs a `designs/<name>/` folder with a
`config.json` and an SDC first (see "Adding a design" in
[`docs/guide.md`](../docs/guide.md)). `designs/vending_machine/` was made from
`vending_machine.v0.v` this way.

## `sec_cases/`

Fixed inputs for [`tools/secbench.py`](../tools/secbench.py), which runs the
equivalence checker over known-good rewrites and deliberately broken mutants
with no model in the loop. It is the soundness gate for SEC: if any mutant is
reported equivalent, the bench exits non-zero.

| folder | contents |
|---|---|
| `netproc/` | eight mutants (`m*.v`), each with a `.txt` saying what was broken |
| `dual_clock/` | a correct adder-tree rewrite, a `+`→`-` mutant, and two unions that read an undeclared signal (the false pass once found and fixed) |
| `vending_machine/` | two correct operand-mux rewrites (`good_*`) and six mutants (`m_*`) |

```bash
make shell
python3 tools/secbench.py vending_machine \
    --case good:pass:benchmarks/sec_cases/vending_machine/good_t1_operand_mux.v \
    --case sell:fail:benchmarks/sec_cases/vending_machine/m_sell_flip.v
```

Results go to `runs/_secbench/<design>-<stamp>/`.
