# tastytrade-exit-manager

Manages exits on an **existing** tastytrade options position. Never opens
positions, never adds size. You enter the trade in the app; this script places
and ratchets the exit orders.

- **Multi-contract single option**: scale-out profit targets + runner with a
  ratcheting trailing stop. Each tranche is an OCO (target + stop) resting at
  the broker; the runner's stop also rests at the broker and only gets moved up.
- **Single contract**: bracket (target + stop) with optional breakeven and
  trailing ratchets.
- **Debit/credit spreads**: profit targets rest at the broker; the stop is
  **software-managed** (script watches net mid and fires a marketable limit
  close) because net-price stops on multi-leg orders are unreliable via API.
  Keep the script alive for spreads, or keep a disaster stop in the app.

## Setup (one time, ~5 min)

1. `my.tastytrade.com` → My Profile → **API** → **OAuth Applications** →
   create an app (read + trade scopes). Save the **client secret**.
2. In the app's **Manage** page → **Create Grant** → save the **refresh token**.
3. Put both in your shell env (e.g. `~/.zshenv` or your secrets manager):

   ```sh
   export TT_CLIENT_SECRET="..."
   export TT_REFRESH_TOKEN="..."
   ```

Requires [uv](https://docs.astral.sh/uv/) — deps are inline, no install step.

Optional: install it as a short command and set up presets:

```sh
chmod +x runner_manager.py
ln -s "$(pwd)/runner_manager.py" ~/.local/bin/ttx   # any name, any dir on PATH
mkdir -p ~/.config/ttx && cp presets.example.toml ~/.config/ttx/presets.toml
```

## Fast path: presets

Presets are named flag bundles in `~/.config/ttx/presets.toml` (see
`presets.example.toml`; override the location with `TTX_PRESETS`). The
`default` preset applies automatically when you pass **no** exit flags, so
managing a fresh fill is just:

```sh
ttx SPY
```

Named presets via `--preset`; explicit flags override preset values:

```sh
ttx SPY --preset spread
ttx SPY --preset default --trail 25%
```

Passing any exit flag (`--scale`/`--target`/`--trail`/`--stop`) without
`--preset` skips presets entirely — one-off commands never silently inherit
preset values.

A preset can name a `one-lot` preset to use instead when the detected position
is a single contract on a single leg (scale-outs need more than 1 unit):
`one-lot = "bracket"` swaps in the bracket preset's target/breakeven/trail
ladder while keeping the rest (e.g. `exit-by`) from the original preset.
CLI flags still override.

## Usage

4 contracts long, sell 2 at +60%, 1 at +100%, trail the last one 30% below
its high; everything stops out at −30% until the first scale fills, then all
stops jump to breakeven; force-flat at 15:50 ET:

```sh
uv run runner_manager.py SPY --qty 4 --entry 1.20 \
  --scale "2@+60%,1@+100%" --trail 30% --stop -30% --exit-by 15:50
```

1 contract, bracket with ratchets:

```sh
uv run runner_manager.py SPY --qty 1 --entry 1.20 \
  --target +100% --stop -30% --be-at +50% --trail-at +80% --trail 25%
```

1 contract, pure runner (no target, trail is the only exit):

```sh
uv run runner_manager.py SPY --qty 1 --entry 1.20 --stop -30% \
  --trail-at +40% --trail 25%
```

Credit spread, close at 50% of max profit, stop at −100% (2x credit):

```sh
uv run runner_manager.py SPY --credit 0.85 --target +50% --stop -100%
```

Debit spread: same as singles but use `--entry` for the net debit. Percent
levels are % of basis (debit paid, or credit received = % of max profit).
A bare number is an absolute net price instead.

Scale quantities can also be percentages of the position —
`--scale "50%@+60%,25%@+100%"` — floored per tranche, zero-quantity tranches
skipped, leftover is still the runner. That's what makes a preset work at any
size: 4 contracts → 2/1/1-runner, 2 → 1/1-runner, 1 → pure runner on the trail.

Defaults: `--exp` today (0DTE), quantity/basis auto-detected from the position
(override with `--entry`/`--credit` — average open price from the API is
worth double-checking).

## Rehearse before trusting it

```sh
uv run runner_manager.py SPY --qty 4 --entry 1.20 \
  --scale "2@+60%,1@+100%" --stop -30% --dry-run
```

`--dry-run` streams real quotes and logs every order it *would* place, every
simulated fill, and every stop ratchet — run it alongside a real trade once
before going live. `--sandbox` targets the cert environment (needs a separate
sandbox account from developer.tastytrade.com; sandbox quotes are unreliable,
`--dry-run` against production data is the better rehearsal).

## Behavior notes

- Stops are **stop-limit** by default, limit = trigger − `--slip` (0.05).
  `--stop-market` if you'd rather guarantee the exit than the price.
- Trailing ratchets are throttled (≥12 s apart, ≥0.03 improvement). Ratcheting
  a stop inside an OCO requires cancel + re-place: ~1 s unprotected window.
- `--und-below` / `--und-above` flatten everything off the *underlying* price —
  steadier trigger than noisy 0DTE option quotes.
- `Ctrl-C` exits and **leaves resting orders working at the broker**. Software
  stops (spreads) die with the script — the warning reminds you.
- Every action is logged to `runner_manager.log` and sent as a macOS
  notification.
- If a flatten can't fill after 6 increasingly aggressive attempts, it stops
  and tells you to close manually.

This manages risk; it doesn't remove it. 0DTE spreads gap, stop-limits can be
jumped, fills aren't guaranteed.
