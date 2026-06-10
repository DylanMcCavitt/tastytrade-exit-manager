#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["tastytrade>=12,<13"]
# ///
"""
runner-manager: manages exits on an EXISTING tastytrade options position.

It never opens positions, never adds size, never re-enters after a stop-out.
One position in, managed to flat, done.

Modes (picked automatically from position + flags):
  - multi-contract single option: scale-out profit targets + runner trailing stop
  - single contract: bracket (target + stop) with breakeven / trailing ratchet
  - debit/credit spread: resting profit targets at broker + software stop

Stops:
  - single-leg positions: stop-limit RESTS AT THE BROKER (survives script death);
    the script only ratchets it upward via cancel/replace
  - spreads: profit targets rest at the broker; the stop is SOFTWARE (script
    watches net mid and fires a marketable limit close) -- keep the script alive

Auth: env vars TT_CLIENT_SECRET and TT_REFRESH_TOKEN (tastytrade OAuth grant).
See README.md for setup.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from tastytrade import Account, DXLinkStreamer, Session
from tastytrade.dxfeed import Quote
from tastytrade.utils import TastytradeError
from tastytrade.instruments import Option
from tastytrade.order import (
    NewComplexOrder,
    NewOrder,
    OrderAction,
    OrderStatus,
    OrderTimeInForce,
    OrderType,
)

ET = ZoneInfo("America/New_York")
POLL_SECS = 3            # order/fill poll interval
TRAIL_MIN_SECS = 12      # min seconds between trailing-stop replacements
TRAIL_MIN_DELTA = Decimal("0.03")  # min stop improvement to bother replacing
LOGFILE = "runner_manager.log"


def log(msg: str) -> None:
    line = f"{datetime.now(ET):%H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a") as f:
            f.write(f"{datetime.now(ET):%Y-%m-%d} {line}\n")
    except OSError:
        pass


def notify(msg: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "runner-manager" sound name "Glass"'],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def round_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


# ---------------------------------------------------------------------------
# Position context
# ---------------------------------------------------------------------------

@dataclass
class LegInfo:
    option: Option
    ratio: int  # contracts per unit, signed: + long, - short


@dataclass
class Ctx:
    legs: list[LegInfo]
    units: int            # number of contracts (single) or spreads managed
    basis: Decimal        # per-unit debit paid (>0) or credit received (>0)
    is_credit: bool
    tick: Decimal
    quotes: dict[str, Quote] = field(default_factory=dict)

    @property
    def open_mark(self) -> Decimal:
        # signed value the position was opened at (credit positions are negative)
        return -self.basis if self.is_credit else self.basis

    def _leg_mid(self, leg: LegInfo) -> Decimal | None:
        q = self.quotes.get(leg.option.streamer_symbol)
        if q is None or q.bid_price is None or q.ask_price is None:
            return None
        return (d(q.bid_price) + d(q.ask_price)) / 2

    def mark(self) -> Decimal | None:
        """Signed net mid of one unit (long premium > 0, short premium < 0)."""
        total = Decimal(0)
        for leg in self.legs:
            mid = self._leg_mid(leg)
            if mid is None:
                return None
            total += leg.ratio * mid
        return total

    def hit(self) -> Decimal | None:
        """Signed net price crossing the spread (marketable close)."""
        total = Decimal(0)
        for leg in self.legs:
            q = self.quotes.get(leg.option.streamer_symbol)
            if q is None or q.bid_price is None or q.ask_price is None:
                return None
            px = d(q.bid_price) if leg.ratio > 0 else d(q.ask_price)
            total += leg.ratio * px
        return total

    def pnl(self) -> Decimal | None:
        m = self.mark()
        return None if m is None else m - self.open_mark

    def pnl_to_price(self, pnl: Decimal) -> Decimal:
        """Signed order price (positive = credit, negative = debit) for this pnl."""
        return self.open_mark + pnl

    def parse_level(self, spec: str) -> Decimal:
        """Level -> pnl per unit. '+60%'/'-30%' are % of basis (for credit
        positions, % of max profit). A bare number is an absolute net price:
        sale price for debit positions, cost-to-close for credit positions."""
        spec = spec.strip()
        if spec.endswith("%"):
            return d(spec[:-1]) / 100 * self.basis
        px = d(spec)
        return (self.basis - px) if self.is_credit else (px - self.basis)

    def fmt_pnl(self, pnl: Decimal) -> str:
        pct = pnl / self.basis * 100
        return f"{pnl:+.2f}/unit ({pct:+.0f}%)"


# ---------------------------------------------------------------------------
# Brackets (one tranche = qty + optional target + stop)
# ---------------------------------------------------------------------------

@dataclass
class Bracket:
    name: str
    units: int
    target_pnl: Decimal | None
    stop_pnl: Decimal | None
    soft_stop: bool
    complex_id: int | None = None
    target_oid: int | None = None
    stop_oid: int | None = None
    done: bool = False
    exit_reason: str = ""


class Manager:
    def __init__(self, args, session: Session, account: Account, ctx: Ctx):
        self.args = args
        self.session = session
        self.account = account
        self.ctx = ctx
        self.brackets: list[Bracket] = []
        self.runner: Bracket | None = None
        self.be_done = False
        self.trail_armed = False
        self.high_pnl: Decimal | None = None
        self.last_trail_move = 0.0
        self.dry = args.dry_run
        self._order_cache: dict[int, object] = {}

    # ---- order plumbing ---------------------------------------------------

    def _close_legs(self, units: int):
        legs = []
        for li in self.ctx.legs:
            action = OrderAction.SELL_TO_CLOSE if li.ratio > 0 else OrderAction.BUY_TO_CLOSE
            legs.append(li.option.build_leg(Decimal(abs(li.ratio) * units), action))
        return legs

    def _limit(self, units: int, pnl: Decimal) -> NewOrder:
        return NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            legs=self._close_legs(units),
            price=round_tick(self.ctx.pnl_to_price(pnl), self.ctx.tick),
        )

    def _stop(self, units: int, pnl: Decimal) -> NewOrder:
        trigger = round_tick(self.ctx.pnl_to_price(pnl), self.ctx.tick)
        if self.args.stop_market:
            return NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.STOP,
                legs=self._close_legs(units),
                stop_trigger=trigger,
            )
        slip = d(self.args.slip)
        limit = trigger - slip  # less credit / more debit = marketable
        if not self.ctx.is_credit:
            limit = max(self.ctx.tick, limit)
        return NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.STOP_LIMIT,
            legs=self._close_legs(units),
            price=round_tick(limit, self.ctx.tick),
            stop_trigger=trigger,
        )

    @staticmethod
    def _oid(resp) -> int | None:
        order = getattr(resp, "order", resp)
        return getattr(order, "id", None)

    async def _place_bracket(self, br: Bracket) -> None:
        ctx = self.ctx
        tgt = f"target {ctx.fmt_pnl(br.target_pnl)}" if br.target_pnl is not None else "no target"
        if br.stop_pnl is None:
            stp = "no stop" + (" (trail places one when armed)" if self.args.trail else "")
        else:
            stp = f"stop {ctx.fmt_pnl(br.stop_pnl)}" + (" [software]" if br.soft_stop else "")
        log(f"[{br.name}] x{br.units}: {tgt}, {stp}")
        if self.dry:
            log(f"[{br.name}] DRY RUN -- orders not sent")
            return
        if br.target_pnl is not None and br.stop_pnl is not None and not br.soft_stop:
            oco = NewComplexOrder(orders=[
                self._limit(br.units, br.target_pnl),
                self._stop(br.units, br.stop_pnl),
            ])
            resp = await self.account.place_complex_order(self.session, oco, dry_run=False)
            co = getattr(resp, "complex_order", None)
            br.complex_id = getattr(co, "id", None)
            children = getattr(co, "orders", None) or []
            for child in children:
                if getattr(child, "order_type", None) in (OrderType.STOP, OrderType.STOP_LIMIT):
                    br.stop_oid = child.id
                else:
                    br.target_oid = child.id
            log(f"[{br.name}] OCO placed (complex #{br.complex_id})")
        else:
            if br.target_pnl is not None:
                resp = await self.account.place_order(
                    self.session, self._limit(br.units, br.target_pnl), dry_run=False)
                br.target_oid = self._oid(resp)
                log(f"[{br.name}] target limit placed (#{br.target_oid})")
            if br.stop_pnl is None:
                pass  # nothing to rest; trail/breakeven may place a stop later
            elif not br.soft_stop:
                resp = await self.account.place_order(
                    self.session, self._stop(br.units, br.stop_pnl), dry_run=False)
                br.stop_oid = self._oid(resp)
                log(f"[{br.name}] stop placed (#{br.stop_oid})")
            else:
                log(f"[{br.name}] software stop armed at {ctx.fmt_pnl(br.stop_pnl)}")

    async def _raise_stop(self, br: Bracket, new_pnl: Decimal, why: str) -> None:
        if br.done or (br.stop_pnl is not None and new_pnl <= br.stop_pnl):
            return
        old = br.stop_pnl
        br.stop_pnl = new_pnl
        old_s = self.ctx.fmt_pnl(old) if old is not None else "--"
        msg = f"[{br.name}] stop {old_s} -> {self.ctx.fmt_pnl(new_pnl)} ({why})"
        if self.dry or br.soft_stop:
            log(("DRY RUN " if self.dry else "") + msg)
            notify(msg)
            return
        if br.complex_id is not None:
            # No replace on complex orders: delete OCO, re-place with raised stop.
            # ~1s unprotected window; ratchets are throttled to keep these rare.
            await self.account.delete_complex_order(self.session, br.complex_id)
            br.complex_id = br.target_oid = br.stop_oid = None
            await self._place_bracket(br)
        elif br.stop_oid is not None:
            resp = await self.account.replace_order(
                self.session, br.stop_oid, self._stop(br.units, new_pnl))
            br.stop_oid = self._oid(resp) or br.stop_oid
        else:
            # bracket had no stop until now (trail/breakeven created one)
            resp = await self.account.place_order(
                self.session, self._stop(br.units, new_pnl), dry_run=False)
            br.stop_oid = self._oid(resp)
        log(msg)
        notify(msg)

    async def _cancel_bracket(self, br: Bracket) -> None:
        if self.dry:
            return
        try:
            if br.complex_id is not None:
                await self.account.delete_complex_order(self.session, br.complex_id)
            else:
                for oid in (br.target_oid, br.stop_oid):
                    if oid is not None:
                        await self.account.delete_order(self.session, oid)
        except Exception as e:  # already filled/cancelled is fine
            log(f"[{br.name}] cancel: {e}")
        br.complex_id = br.target_oid = br.stop_oid = None

    async def _status(self, oid: int):
        cached = self._order_cache.get(oid)
        if cached is not None:
            return getattr(cached, "status", None)
        try:
            order = await self.account.get_order(self.session, oid)
            return getattr(order, "status", None)
        except Exception:
            return None

    async def _refresh_orders(self) -> None:
        try:
            live = await self.account.get_live_orders(self.session)
            self._order_cache = {o.id: o for o in live}
        except Exception as e:
            log(f"order poll failed: {e}")

    # ---- emergency / final close ------------------------------------------

    async def flatten(self, brackets: list[Bracket], reason: str) -> None:
        units = sum(b.units for b in brackets if not b.done)
        if units == 0:
            return
        log(f"FLATTEN x{units}: {reason}")
        notify(f"Flattening {units} unit(s): {reason}")
        for b in brackets:
            if not b.done:
                await self._cancel_bracket(b)
        if self.dry:
            for b in brackets:
                if not b.done:
                    b.done, b.exit_reason = True, f"flatten ({reason})"
            log("DRY RUN -- would close at marketable limit")
            return
        for attempt in range(6):
            hit = self.ctx.hit()
            if hit is None:
                await asyncio.sleep(1)
                continue
            price = round_tick(hit - self.ctx.tick * 2 * attempt, self.ctx.tick)
            order = NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.LIMIT,
                legs=self._close_legs(units),
                price=price,
            )
            resp = await self.account.place_order(self.session, order, dry_run=False)
            oid = self._oid(resp)
            log(f"flatten attempt {attempt + 1}: limit {price} (#{oid})")
            for _ in range(4):
                await asyncio.sleep(2)
                if await self._status(oid) == OrderStatus.FILLED:
                    log("flatten filled")
                    for b in brackets:
                        if not b.done:
                            b.done, b.exit_reason = True, f"flatten ({reason})"
                    return
            try:
                await self.account.delete_order(self.session, oid)
            except Exception:
                pass
        log("FLATTEN FAILED after 6 attempts -- CLOSE MANUALLY IN THE APP")
        notify("FLATTEN FAILED -- close manually NOW")

    # ---- main loop ---------------------------------------------------------

    def _all(self) -> list[Bracket]:
        return self.brackets + ([self.runner] if self.runner else [])

    async def run(self) -> None:
        ctx, args = self.ctx, self.args
        for br in self._all():
            await self._place_bracket(br)

        be_at = ctx.parse_level(args.be_at) if args.be_at else None
        trail_at = ctx.parse_level(args.trail_at) if args.trail_at else None
        trail_frac = d(args.trail.rstrip("%")) / 100 if args.trail else None
        exit_by = None
        if args.exit_by:
            h, m = args.exit_by.split(":")
            exit_by = dtime(int(h), int(m))

        und_sym = args.symbol
        log(f"managing {ctx.units} unit(s), basis {ctx.basis} "
            f"{'credit' if ctx.is_credit else 'debit'} -- watching quotes")

        while True:
            await asyncio.sleep(POLL_SECS)
            pnl = ctx.pnl()
            if not self.dry:
                await self._refresh_orders()

            # --- fill detection ---
            for br in self._all():
                if br.done:
                    continue
                if self.dry and pnl is not None:
                    if br.target_pnl is not None and pnl >= br.target_pnl:
                        br.done, br.exit_reason = True, "target (sim)"
                    elif br.stop_pnl is not None and pnl <= br.stop_pnl:
                        br.done, br.exit_reason = True, "stop (sim)"
                    if br.done:
                        log(f"[{br.name}] SIM FILL: {br.exit_reason} at pnl {ctx.fmt_pnl(pnl)}")
                        notify(f"{br.name}: {br.exit_reason}")
                else:
                    if br.target_oid and await self._status(br.target_oid) == OrderStatus.FILLED:
                        br.done, br.exit_reason = True, "target filled"
                    elif br.stop_oid and await self._status(br.stop_oid) == OrderStatus.FILLED:
                        br.done, br.exit_reason = True, "stop filled"
                    if br.done:
                        log(f"[{br.name}] {br.exit_reason}")
                        notify(f"{br.name}: {br.exit_reason}")
                        if br.complex_id is None and br.target_oid and br.stop_oid:
                            # standalone pair (not OCO): cancel the surviving sibling
                            await self._cancel_bracket(br)
                # software stop trigger
                if (not br.done and br.soft_stop and not self.dry
                        and br.stop_pnl is not None
                        and pnl is not None and pnl <= br.stop_pnl):
                    log(f"[{br.name}] software stop hit at {ctx.fmt_pnl(pnl)}")
                    await self.flatten([br], f"{br.name} software stop")

            if all(b.done for b in self._all()):
                break

            # --- breakeven ratchet ---
            scale_fill = any(b.done and b.exit_reason.startswith("target") for b in self.brackets)
            be_trigger = (args.be_after_first_scale and scale_fill) or \
                         (be_at is not None and pnl is not None and pnl >= be_at)
            if be_trigger and not self.be_done:
                self.be_done = True
                for br in self._all():
                    await self._raise_stop(br, Decimal(0), "breakeven")

            # --- trailing (runner, or the single bracket if no scales) ---
            trail_target = self.runner or (self.brackets[0] if len(self.brackets) == 1 else None)
            if trail_frac and trail_target and not trail_target.done and pnl is not None:
                scales_done = all(b.done for b in self.brackets) if self.runner else True
                if not self.trail_armed and (
                        scales_done and self.runner is not None
                        or (trail_at is not None and pnl >= trail_at)
                        or (self.runner is None and trail_at is None)):
                    self.trail_armed = True
                    self.high_pnl = pnl
                    log(f"trailing armed at {ctx.fmt_pnl(pnl)} (trail {args.trail})")
                    notify(f"Trailing armed, {args.trail} below highs")
                if self.trail_armed:
                    if pnl > (self.high_pnl or pnl):
                        self.high_pnl = pnl
                    high_value = ctx.basis + self.high_pnl if not ctx.is_credit else ctx.basis
                    desired = self.high_pnl - trail_frac * high_value
                    now = asyncio.get_event_loop().time()
                    cur_stop = trail_target.stop_pnl
                    if ((cur_stop is None or desired > cur_stop + TRAIL_MIN_DELTA)
                            and now - self.last_trail_move >= TRAIL_MIN_SECS):
                        self.last_trail_move = now
                        await self._raise_stop(trail_target, desired,
                                               f"trail, high {ctx.fmt_pnl(self.high_pnl)}")

            # --- underlying triggers ---
            uq = ctx.quotes.get(und_sym)
            if uq and uq.bid_price is not None and uq.ask_price is not None:
                umid = (d(uq.bid_price) + d(uq.ask_price)) / 2
                if args.und_below and umid <= d(args.und_below):
                    await self.flatten(self._all(), f"{und_sym} {umid} <= {args.und_below}")
                    break
                if args.und_above and umid >= d(args.und_above):
                    await self.flatten(self._all(), f"{und_sym} {umid} >= {args.und_above}")
                    break

            # --- time exit ---
            if exit_by and datetime.now(ET).time() >= exit_by:
                await self.flatten(self._all(), f"exit-by {args.exit_by} ET")
                break

        log("--- session done ---")
        for br in self._all():
            log(f"  {br.name} x{br.units}: {br.exit_reason or 'open'}")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def occ_expiry(symbol: str) -> date | None:
    # OCC: 'SPY   260609C00640000' -> yymmdd at chars 6..12
    try:
        return datetime.strptime(symbol[6:12], "%y%m%d").date()
    except ValueError:
        return None


async def build_ctx(args, session: Session, account: Account) -> Ctx:
    exp = date.fromisoformat(args.exp) if args.exp else datetime.now(ET).date()
    positions = await account.get_positions(session)
    legs_raw = [
        p for p in positions
        if p.underlying_symbol == args.symbol
        and "OPTION" in str(getattr(p, "instrument_type", "")).upper().replace(" ", "_")
        and occ_expiry(p.symbol) == exp
    ]
    # OCC symbol tail: [C|P] + 8-digit strike*1000, e.g. ...C00725000
    if args.strike:
        wanted = {d(s) for s in args.strike.split(",")}
        legs_raw = [p for p in legs_raw if Decimal(p.symbol[-8:]) / 1000 in wanted]
    if args.right:
        r = args.right.strip().upper()[0]
        legs_raw = [p for p in legs_raw if p.symbol[-9].upper() == r]
    if not legs_raw:
        found = sorted({(p.underlying_symbol, occ_expiry(p.symbol)) for p in positions
                        if occ_expiry(p.symbol)})
        filt = "".join([f" strike {args.strike}" if args.strike else "",
                        f" right {args.right}" if args.right else ""])
        log(f"no {args.symbol} option position expiring {exp}{filt}."
            f" open option positions: {found}")
        sys.exit(1)

    signed = []
    for p in legs_raw:
        qty = int(p.quantity)
        if str(getattr(p, "quantity_direction", "Long")) == "Short":
            qty = -qty
        signed.append((p, qty))

    units = math.gcd(*[abs(q) for _, q in signed])
    if args.qty:
        units = min(units, args.qty)

    legs, basis_auto = [], Decimal(0)
    for p, qty in signed:
        ratio = qty // units if abs(qty) % units == 0 else qty
        opt = await Option.get(session, p.symbol)
        legs.append(LegInfo(option=opt, ratio=ratio))
        basis_auto += ratio * d(p.average_open_price)

    if args.entry:
        basis, is_credit = d(args.entry), False
    elif args.credit:
        basis, is_credit = d(args.credit), True
    else:
        is_credit = basis_auto < 0
        basis = abs(basis_auto)

    ctx = Ctx(legs=legs, units=units, basis=basis, is_credit=is_credit, tick=d(args.tick))

    log(f"position: {args.symbol} exp {exp}")
    for li in ctx.legs:
        side = "LONG" if li.ratio > 0 else "SHORT"
        log(f"  {side} {abs(li.ratio)}x per unit  {li.option.symbol}")
    log(f"  units: {units}   basis: {basis} {'credit' if is_credit else 'debit'}"
        f"{' (from avg open price -- override with --entry/--credit)' if not (args.entry or args.credit) else ''}")
    return ctx


def build_brackets(args, ctx: Ctx, mgr: Manager) -> None:
    # preset redirect for single-contract single-leg positions: scale-outs make
    # no sense on 1 unit, so swap in the designated bracket-style preset.
    # CLI-explicit flags still win over the redirected preset's values.
    one_lot = getattr(args, "one_lot_preset", None)
    if one_lot and ctx.units == 1 and len(ctx.legs) == 1:
        explicit = getattr(args, "cli_flags", set())
        sub = load_preset(one_lot) or {}
        if "--scale" not in explicit:
            args.scale = None
        for k, v in sub.items():
            if f"--{k}" not in explicit:
                setattr(args, k.replace("-", "_"), v)
        log(f"1 lot detected -> preset '{one_lot}'")

    soft = args.soft_stop or len(ctx.legs) > 1
    if soft and len(ctx.legs) > 1 and args.stop:
        log("spread detected: stop will be SOFTWARE-managed (keep this script alive);"
            " profit targets rest at the broker")
    stop_pnl = ctx.parse_level(args.stop) if args.stop else None

    if args.scale:
        scaled = 0
        for i, part in enumerate(args.scale.split(","), 1):
            qty_s, level = part.split("@")
            qty_s = qty_s.strip()
            if qty_s.endswith("%"):
                qty = int(ctx.units * Decimal(qty_s[:-1]) / 100)  # floor
            else:
                qty = int(qty_s)
            if qty <= 0:
                log(f"T{i} ({part.strip()}): {qty_s} of {ctx.units} rounds to 0 -- skipped")
                continue
            scaled += qty
            mgr.brackets.append(Bracket(
                name=f"T{i}", units=qty, soft_stop=soft,
                target_pnl=ctx.parse_level(level), stop_pnl=stop_pnl))
        run_qty = ctx.units - scaled
        if run_qty < 0:
            log(f"--scale sells {scaled} but only {ctx.units} units held")
            sys.exit(1)
        if run_qty > 0:
            mgr.runner = Bracket(name="RUNNER", units=run_qty, soft_stop=soft,
                                 target_pnl=None, stop_pnl=stop_pnl)
        if not args.trail:
            args.trail = "25%"
    else:
        target = ctx.parse_level(args.target) if args.target else None
        mgr.brackets.append(Bracket(
            name="POS", units=ctx.units, soft_stop=soft,
            target_pnl=target, stop_pnl=stop_pnl))


async def amain(args) -> None:
    secret = os.environ.get("TT_CLIENT_SECRET")
    token = os.environ.get("TT_REFRESH_TOKEN")
    if not secret or not token:
        log("set TT_CLIENT_SECRET and TT_REFRESH_TOKEN (see README.md)")
        sys.exit(1)
    session = Session(secret, token, is_test=args.sandbox)
    accounts = await Account.get(session)
    acct_no = args.account or os.environ.get("TT_ACCOUNT")
    account = accounts[0]
    if acct_no:
        account = next((a for a in accounts if a.account_number == acct_no), None)
        if account is None:
            log(f"account {acct_no} not found; available:"
                f" {[a.account_number for a in accounts]}")
            sys.exit(1)
    log(f"account {account.account_number}{' [SANDBOX]' if args.sandbox else ''}"
        f"{' [DRY RUN]' if args.dry_run else ''}")

    ctx = await build_ctx(args, session, account)
    mgr = Manager(args, session, account, ctx)
    build_brackets(args, ctx, mgr)

    for br in mgr._all():
        if br.target_pnl is not None:
            tgt = (f"{ctx.fmt_pnl(br.target_pnl)}"
                   f" (sell @{round_tick(ctx.pnl_to_price(br.target_pnl), ctx.tick)})")
        else:
            tgt = "--"
        if br.stop_pnl is not None:
            stop_trig = round_tick(ctx.pnl_to_price(br.stop_pnl), ctx.tick)
            stp = f"{ctx.fmt_pnl(br.stop_pnl)} (trigger @{stop_trig})"
        else:
            stp = "--"
        log(f"plan [{br.name}] x{br.units}  target {tgt}  stop {stp}")
    if not args.yes:
        if input("proceed? [yes/no] ").strip().lower() not in ("y", "yes"):
            sys.exit(0)

    syms = [li.option.streamer_symbol for li in ctx.legs] + [args.symbol]
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, syms)

        async def pump():
            async for q in streamer.listen(Quote):
                ctx.quotes[q.event_symbol] = q

        task = asyncio.create_task(pump())
        # wait for first full set of quotes
        for _ in range(30):
            if ctx.mark() is not None:
                break
            await asyncio.sleep(1)
        else:
            log("no quotes after 30s -- aborting before placing orders")
            sys.exit(1)
        log(f"mark {ctx.mark()}  pnl {ctx.fmt_pnl(ctx.pnl())}")

        # refuse to place a stop the market is already through -- the broker
        # rejects it ("would execute immediately") and the position stays naked
        pnl = ctx.pnl()
        if not args.dry_run and pnl is not None:
            for br in mgr._all():
                if br.stop_pnl is not None and pnl <= br.stop_pnl:
                    trig = round_tick(ctx.pnl_to_price(br.stop_pnl), ctx.tick)
                    log(f"mark {ctx.mark()} is already through the [{br.name}] stop"
                        f" trigger ({trig}, {ctx.fmt_pnl(br.stop_pnl)}). nothing placed."
                        " close the position manually or rerun with a wider --stop.")
                    notify("stop already breached -- nothing placed")
                    sys.exit(1)

        try:
            await mgr.run()
        except KeyboardInterrupt:
            raise
        except TastytradeError as e:
            log(f"broker rejected an order: {e}")
            log("check the app for any resting orders before re-running")
            notify("order rejected -- check the app")
            sys.exit(1)
        finally:
            task.cancel()


PRESETS_PATH = os.path.expanduser(os.environ.get("TTX_PRESETS", "~/.config/ttx/presets.toml"))


def load_preset(name: str) -> dict | None:
    import tomllib
    try:
        with open(PRESETS_PATH, "rb") as f:
            presets = tomllib.load(f)
    except FileNotFoundError:
        return None
    return presets.get(name)


LEVEL_FLAGS = {"--stop", "--target", "--be-at", "--trail-at", "--entry", "--credit",
               "--und-below", "--und-above"}


def merge_negative_values(argv: list[str]) -> list[str]:
    """argparse reads '--stop -100%' as two flags; rewrite to '--stop=-100%'."""
    out, i = [], 0
    while i < len(argv):
        a, nxt = argv[i], argv[i + 1] if i + 1 < len(argv) else ""
        if (a in LEVEL_FLAGS and len(nxt) > 1 and nxt[0] == "-"
                and (nxt[1].isdigit() or nxt[1] == ".")):
            out.append(f"{a}={nxt}")
            i += 2
        else:
            out.append(a)
            i += 1
    return out


def main() -> None:
    sys.argv[1:] = merge_negative_values(sys.argv[1:])
    p = argparse.ArgumentParser(description="manage exits on an existing tastytrade options position")
    p.add_argument("symbol", help="underlying, e.g. SPY")
    p.add_argument("--exp", help="expiration YYYY-MM-DD (default: today / 0DTE)")
    p.add_argument("--qty", type=int, help="units to manage (default: full position)")
    p.add_argument("--strike", help="only manage legs at these strikes, e.g. 715 or 714,715")
    p.add_argument("--right", help="only manage calls (C) or puts (P)")
    p.add_argument("--entry", help="net debit paid per unit (overrides detected basis)")
    p.add_argument("--credit", help="net credit received per unit (credit spreads)")
    p.add_argument("--scale", help='scale-outs as counts or %% of position, e.g. "2@+60%%,1@+100%%"'
                   ' or "50%%@+60%%,25%%@+100%%" (floored; 0-qty tranches skipped); leftover = runner')
    p.add_argument("--target", help="profit target, e.g. +100%% or 2.40; combine freely with"
                   " --stop/--trail or use alone")
    p.add_argument("--stop", help="initial stop, e.g. -30%%; optional -- omit for no stop")
    p.add_argument("--be-after-first-scale", action="store_true", default=True,
                   help="ratchet all stops to breakeven after first scale-out fill (default on)")
    p.add_argument("--no-be", dest="be_after_first_scale", action="store_false")
    p.add_argument("--be-at", help="ratchet stop to breakeven when pnl reaches level, e.g. +50%%")
    p.add_argument("--trail-at", help="arm trailing when pnl reaches level, e.g. +80%%")
    p.add_argument("--trail", help="trail distance below high, e.g. 25%% (default 25%% with --scale)")
    p.add_argument("--exit-by", help="flatten everything at HH:MM ET, e.g. 15:50")
    p.add_argument("--und-below", help="flatten if underlying mid <= level (calls)")
    p.add_argument("--und-above", help="flatten if underlying mid >= level (puts)")
    p.add_argument("--slip", default="0.05", help="stop-limit offset below trigger (default 0.05)")
    p.add_argument("--tick", default="0.01", help="price increment (default 0.01)")
    p.add_argument("--stop-market", action="store_true", help="stop-market instead of stop-limit")
    p.add_argument("--soft-stop", action="store_true",
                   help="script-managed stop instead of broker-resting (forced for spreads)")
    p.add_argument("--account", help="account number (default: first)")
    p.add_argument("--sandbox", action="store_true", help="use the cert/sandbox environment")
    p.add_argument("--dry-run", action="store_true",
                   help="no orders sent; simulates fills from live quotes")
    p.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    p.add_argument("--preset", help=f"named preset from {PRESETS_PATH}; CLI flags override."
                   " With no exit flags at all, the 'default' preset applies automatically")

    # presets fill in defaults before parsing, so explicit flags always win
    argv_flags = {a.split("=", 1)[0] for a in sys.argv[1:] if a.startswith("--")}
    preset_name = None
    if "--preset" in argv_flags:
        pre = argparse.ArgumentParser(add_help=False)
        pre.add_argument("--preset")
        preset_name = pre.parse_known_args()[0].preset
    elif not argv_flags & {"--scale", "--target", "--trail", "--stop"}:
        preset_name = "default"
    one_lot = None
    if preset_name:
        preset = load_preset(preset_name)
        if preset is None and preset_name != "default":
            p.error(f"preset '{preset_name}' not found in {PRESETS_PATH}")
        if preset:
            mapped = {k.replace("-", "_"): v for k, v in preset.items()}
            one_lot = mapped.pop("one_lot", None)
            dests = {a.dest for a in p._actions}
            bad = mapped.keys() - dests
            if bad:
                p.error(f"preset '{preset_name}': unknown keys {sorted(bad)}")
            if one_lot:
                sub = load_preset(one_lot)
                if sub is None:
                    p.error(f"one-lot preset '{one_lot}' not found in {PRESETS_PATH}")
                bad = {k.replace("-", "_") for k in sub} - dests
                if bad:
                    p.error(f"preset '{one_lot}': unknown keys {sorted(bad)}")
            p.set_defaults(**mapped)

    args = p.parse_args()
    args.one_lot_preset = one_lot
    args.cli_flags = argv_flags
    if preset_name and preset:
        log(f"preset '{preset_name}' ({PRESETS_PATH})")

    if not (args.scale or args.target or args.trail or args.stop):
        p.error("give at least one exit: --scale, --target, --stop, or --trail"
                " (combine freely; omitted parts are simply not placed)")

    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        log("interrupted -- resting orders LEFT IN PLACE at the broker"
            " (cancel in the app if unwanted); software stops are now dead")
        notify("runner-manager stopped -- check resting orders")


if __name__ == "__main__":
    main()
