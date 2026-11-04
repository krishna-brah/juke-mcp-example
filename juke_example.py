#!/usr/bin/env python3
"""Use juke's Builder API from your own code: a small, runnable example.

juke (https://www.getjuked.io) is a free, paper-only proving ground on real
Polymarket prices. This script walks the loop a connected system plays:

  1. list this Week's open games and your Book    GET  /builder/board?view=SUMMARY
  2. read one game's markets with live prices     GET  /builder/board?eventId=...
  3. optionally make one paper pick, or record    POST /builder/pick
     one forecast                                 POST /builder/forecast
  4. read your positions back                     GET  /builder/positions
  5. read your record (juke#) back                GET  /builder/record

By default it is a dry run: it prints the requests and sends nothing, so it
runs without a key. Add --send (with JUKE_API_KEY set) to send them for real.
See README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode, urlparse

import requests
from requests.auth import AuthBase

DEFAULT_BASE_URL = "https://www.getjuked.io/api/v3-beta"
USER_AGENT = "juke-mcp-example/0.1"
ARENAS = {"football": "arena:football", "soccer": "arena:soccer", "basketball": "arena:basketball"}

# The shapes juke checks on its side. Checking them here turns a typo into a clear
# message instead of a 4xx.
KEY_PATTERN = re.compile(r"jk_[A-Za-z0-9_-]{40,60}")
ORDER_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{6,64}")
CREDITS_PATTERN = re.compile(r"[0-9]{1,6}(\.[0-9]{1,6})?")

# A game or market takes picks while its state is one of these (before the game, or in play).
OPEN_STATES = {"OPEN", "IN_PLAY_CHECK_REQUIRED"}
# The example asks for a fill no worse than the live price plus this much. If the
# price moves further before the pick lands, juke refuses it and nothing trades.
LIMIT_SLACK = 0.02

# Dry-run stand-ins for values only a live answer can give.
EVENT_PLACEHOLDER = "<eventId-of-the-first-open-game-from-step-1>"
LISTING_PLACEHOLDER = "<listingId-of-the-market-chosen-in-step-2>"
LIMIT_PLACEHOLDER = "<live-price-of-the-chosen-side-plus-0.02>"

# What juke's error codes mean for your next move (see the API reference).
HINTS = {
    "SYSTEM_KEY_REQUIRED": "send your system's key as 'Authorization: Bearer jk_...'",
    "SYSTEM_KEY_INVALID": "the key is wrong, replaced or revoked (a replaced key stops working within a minute)",
    "SYSTEM_RATE_LIMITED": "over 120 reads or 30 writes a minute: wait a minute",
    "PRICE_MOVED": "the live price was worse than your limit and nothing traded: use a new --order-id to try again",
    "MARKET_CLOSED": "that market no longer takes picks or forecasts",
    "ORDER_IN_PROGRESS": "the same order is still being placed: wait, then send it again with the same --order-id",
    "ORDER_OUTCOME_UNKNOWN": "juke could not confirm the outcome: read /builder/positions before doing anything else",
    "NO_ANSWER": "no answer from juke. If this was a pick, it may still have been placed: send it again with the same --order-id (it never trades twice) or read your positions first",
}


# ---------------------------------------------------------------------------
# Requests: what the example sends (and what the dry run prints)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One Builder API request."""

    method: str
    path: str
    params: dict[str, str] | None = None
    body: dict[str, Any] | None = None

    def url(self, base_url: str) -> str:
        # ':' (as in arena:football) and <placeholders> stay readable; real ids never contain '<' or '>'.
        query = f"?{urlencode(self.params, safe=':<>')}" if self.params else ""
        return f"{base_url}{self.path}{query}"


def summary_call(arena_id: str) -> Call:
    return Call("GET", "/builder/board", {"arenaId": arena_id, "view": "SUMMARY"})


def event_call(arena_id: str, event_id: str) -> Call:
    return Call("GET", "/builder/board", {"arenaId": arena_id, "eventId": event_id})


def pick_call(arena_id: str, listing_id: str, side: str, credits: str, order_id: str, limit_price: Any) -> Call:
    """Buy `side` of one market with paper Credits. `credits` is a string; `orderId` is yours and is used once."""
    body = {"arenaId": arena_id, "listingId": listing_id, "side": side, "credits": credits, "orderId": order_id, "limitPrice": limit_price}
    return Call("POST", "/builder/pick", body=body)


def forecast_call(listing_id: str, probability: float) -> Call:
    """Your probability that the market resolves YES. One per market: the first one counts."""
    return Call("POST", "/builder/forecast", body={"listingId": listing_id, "probability": probability})


def positions_call() -> Call:
    return Call("GET", "/builder/positions")


def record_call() -> Call:
    return Call("GET", "/builder/record")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class JukeError(Exception):
    """juke refused a request. `code` is the API's own error code, for example PRICE_MOVED."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code


def check_base_url(value: str) -> str:
    """The key may only go to juke (https on getjuked.io) or to your own machine (http on localhost)."""
    url = urlparse(value.strip())
    host = url.hostname or ""
    juke = url.scheme == "https" and (host == "getjuked.io" or host.endswith(".getjuked.io"))
    local = url.scheme == "http" and host in ("localhost", "127.0.0.1")
    if not (juke or local) or url.username or url.password:
        raise ValueError("JUKE_BASE_URL must be https on getjuked.io, or http on localhost or 127.0.0.1: the key is never sent anywhere else")
    return value.strip().rstrip("/")


class BearerAuth(AuthBase):
    """Sets 'Authorization: Bearer <key>'. Passing auth= also stops requests from reading ~/.netrc, which would replace it."""

    def __init__(self, key: str) -> None:
        self.key = key

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        request.headers["Authorization"] = f"Bearer {self.key}"
        return request


class JukeClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, *, timeout: float = 60) -> None:
        self.base_url = check_base_url(base_url)
        self.timeout = timeout
        self._auth = BearerAuth(api_key)
        self._session = requests.Session()

    def send(self, call: Call) -> dict[str, Any]:
        try:
            response = self._session.request(
                call.method,
                call.url(self.base_url),
                json=call.body,
                auth=self._auth,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=self.timeout,
                allow_redirects=False,  # the key goes to one address only
            )
        except requests.RequestException:
            raise JukeError(0, "NO_ANSWER") from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        # juke answers errors as {"ok": false, "code": "..."} with an HTTP status. Anything but a 2xx JSON object is an error.
        if not 200 <= response.status_code < 300 or not isinstance(payload, dict) or payload.get("ok") is False:
            code = payload.get("code") if isinstance(payload, dict) else None
            raise JukeError(response.status_code, code if isinstance(code, str) else f"HTTP_{response.status_code}")
        return payload


# ---------------------------------------------------------------------------
# Reading the answers
# ---------------------------------------------------------------------------


def first_arena(response: Mapping[str, Any]) -> dict[str, Any] | None:
    arenas = (response.get("board") or {}).get("arenas") or []
    return arenas[0] if arenas else None


def open_games(summary: Mapping[str, Any], now: str | None = None) -> list[dict[str, Any]]:
    """The games that still take picks, in the order they stop taking picks."""
    now = now or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    games = []
    for game in (first_arena(summary) or {}).get("eventGroups") or []:
        state = game.get("eventLifecycleState")
        # Older answers carry no state: fall back to the time the game stops taking picks.
        takes_picks = state in OPEN_STATES if state is not None else str(game.get("actionClosesAt", "")) > now
        if takes_picks and game.get("eventId"):
            games.append(game)
    return sorted(games, key=lambda game: str(game.get("actionClosesAt", "~")))


def as_price(value: Any) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if 0 < price < 1 else None


def live_price(listing: Mapping[str, Any], side: str) -> float | None:
    """What buying `side` costs right now (0 to 1), or None when nobody is selling it. Prices arrive as strings."""
    for field in ("indicative", "indicativeWide"):
        price = as_price((listing.get(field) or {}).get(side.lower()))
        if price is not None:
            return price
    one_sided = listing.get("indicativeOneSided") or {}
    return as_price(one_sided.get("price")) if one_sided.get("side") == side else None


def choose_market(listings: Sequence[Mapping[str, Any]], side: str) -> tuple[Mapping[str, Any], float] | None:
    """The first winner (moneyline) market with a live price for `side`, else the first priced market.

    Returns the market and that price. This is where your own model goes: return the market you want to act on.
    """
    priced = [(listing, live_price(listing, side)) for listing in listings]
    priced = [(listing, price) for listing, price in priced if price is not None]
    winners = [(listing, price) for listing, price in priced if (listing.get("sportsMarketPresentation") or {}).get("marketType") == "moneyline"]
    return (winners or priced or [None])[0]


def fmt_price(price: float | None) -> str:
    return "-" if price is None else f"{price:.2f}"


def week_line(summary: Mapping[str, Any]) -> str:
    week = summary.get("week")
    return f"Week {week.get('cycleId')}, ends {week.get('endsAt')}" if week else "No Week is open right now"


def book_line(arena: Mapping[str, Any] | None) -> str:
    book = (arena or {}).get("book")
    if not book:
        return "Book: none yet this Week (it opens with your first pick)"
    reserve = book.get("reserve") or {}
    credits = reserve.get("available") or reserve.get("amount") or book.get("initialReserve")
    return f"Book: {book.get('state')}, {credits} Credits left"


def game_line(game: Mapping[str, Any]) -> str:
    return f"{game.get('actionClosesAt')}  {game.get('eventTitle')}  (markets: {game.get('marketCount')})"


def market_line(listing: Mapping[str, Any]) -> str:
    presentation = listing.get("sportsMarketPresentation") or {}
    kind = presentation.get("marketTypeLabel") or presentation.get("marketType") or "market"
    prices = f"YES {fmt_price(live_price(listing, 'YES'))} / NO {fmt_price(live_price(listing, 'NO'))}"
    return f"{listing.get('listingId')}  [{kind}] {listing.get('question')}  {prices}"


def position_lines(payload: Mapping[str, Any]) -> list[str]:
    positions = [p for p in payload.get("positions") or [] if isinstance(p, dict)]
    held = [p for p in positions if p.get("status") == "OPEN"]
    lines = [f"positions: {len(positions)}, open: {len(held)}"]
    for p in held[:10]:
        lines.append(f"{p.get('side')} {p.get('shares')} shares, cost {p.get('costBasis')}  {p.get('question') or p.get('listingId')}")
    return lines


def record_lines(record: Mapping[str, Any]) -> list[str]:
    """A short read of your juke#. The full answer has every cell: run with --raw to see it."""
    if record.get("state") != "AVAILABLE":
        return [f"not available yet ({record.get('code') or record.get('state')})"]
    lines = [f"{record.get('gradedPicks', 0)} graded picks across {record.get('games', 0)} games"]
    for cell in record.get("verdicts") or []:
        if isinstance(cell, dict) and cell.get("cellId") == "OVERALL":
            lines.append(f"overall: {cell.get('label')}; still needs {cell.get('picksNeeded', 0)} picks across {cell.get('eventsNeeded', 0)} games")
    return lines


# ---------------------------------------------------------------------------
# The two ways to run it
# ---------------------------------------------------------------------------

Output = Callable[[str], None]


def describe(call: Call, base_url: str) -> list[str]:
    lines = [f"{call.method} {call.url(base_url)}"]
    if call.body is not None:
        lines += json.dumps(call.body, indent=2).splitlines()
    return lines


def plan_steps(args: argparse.Namespace, order_id: str) -> list[tuple[str, Call]]:
    """The requests a real run makes with these options, in order. Ids that come from a live answer are placeholders."""
    arena_id = ARENAS[args.arena]
    steps = [
        ("This Week's open games and your Book", summary_call(arena_id)),
        ("One game's markets with live prices", event_call(arena_id, EVENT_PLACEHOLDER)),
    ]
    if args.pick:
        steps.append(("One paper pick", pick_call(arena_id, LISTING_PLACEHOLDER, args.side, args.credits, order_id, LIMIT_PLACEHOLDER)))
    elif args.forecast is not None:
        steps.append(("One forecast (your chance that the chosen market resolves YES)", forecast_call(LISTING_PLACEHOLDER, args.forecast)))
    return steps + [("Your positions", positions_call()), ("Your record (juke#)", record_call())]


def dry_run(args: argparse.Namespace, base_url: str, order_id: str, out: Output) -> None:
    """Print the requests a real run makes, in order. No network, no key."""
    out("DRY RUN: nothing is sent and no key is needed. These are the requests a real run would make.")
    out(f"Base URL: {base_url}")
    out("Every request carries:  Authorization: Bearer jk_...   (your system's key, read from JUKE_API_KEY and never printed)")
    out("")
    for number, (label, call) in enumerate(plan_steps(args, order_id), start=1):
        out(f"{number}. {label}")
        for line in describe(call, base_url):
            out(f"   {line}")
    out("")
    if args.pick or args.forecast is not None:
        out("To send these for real, add --send (with JUKE_API_KEY set). The write goes out only with --send.")
    else:
        out("--pick or --forecast P adds one write to the list. --send (with JUKE_API_KEY set) sends the requests.")


def run(client: JukeClient, args: argparse.Namespace, order_id: str, out: Output) -> int:
    """Send the requests for real. Returns the exit code."""
    arena_id = ARENAS[args.arena]
    side = args.side if args.pick else "YES"
    wants_write = args.pick or args.forecast is not None
    write_done = False
    number = 0

    def step(label: str, call: Call) -> dict[str, Any]:
        nonlocal number
        number += 1
        out(f"{number}. {label}")
        for line in describe(call, client.base_url):
            out(f"   {line}")
        payload = client.send(call)
        if args.raw:
            out(json.dumps(payload, indent=2))
        return payload

    def say(text: str) -> None:
        out(f"   -> {text}")

    def item(text: str) -> None:
        out(f"        {text}")

    try:
        summary = step("This Week's open games and your Book", summary_call(arena_id))
        arena = first_arena(summary)
        say(week_line(summary))
        say(book_line(arena))
        games = open_games(summary)
        say(f"open games: {len(games)}" + (" (in the order they stop taking picks)" if games else ""))
        for game in games[:5]:
            item(game_line(game))

        chosen = None
        if games:
            board = step("One game's markets with live prices", event_call(arena_id, games[0]["eventId"]))
            listings = (first_arena(board) or {}).get("listings") or []
            say(f"markets: {len(listings)}")
            for listing in listings[:10]:
                item(market_line(listing))
            if len(listings) > 10:
                item(f"... and {len(listings) - 10} more")
            chosen = choose_market(listings, side)

        if wants_write and chosen is None:
            say("no open market with a live price to act on right now, so nothing was written")
        elif wants_write and chosen is not None:
            market, price = chosen
            say(f"chosen market: {market.get('question')}")
            if args.pick:
                limit = round(min(price + LIMIT_SLACK, 0.99), 4)
                call = pick_call(arena_id, market["listingId"], side, args.credits, order_id, limit)
                label = f"One paper pick: {args.credits} Credits on {side} at about {fmt_price(price)}, limit {limit}"
            else:
                call = forecast_call(market["listingId"], args.forecast)
                label = f"One forecast: {args.forecast} that the market resolves YES"
            try:
                result = step(label, call)
            except JukeError as error:
                report_error(error, out)
            else:
                write_done = True
                if args.pick:
                    replayed = " (replayed: this orderId was already placed, nothing traded again)" if result.get("replayed") else ""
                    say(f"placed: orderId {result.get('orderId')}, positionId {result.get('positionId')}{replayed}")
                else:
                    say(f"recorded: forecastId {result.get('forecastId')}")

        held = step("Your positions", positions_call())
        first, *rest = position_lines(held)
        say(first)
        for line in rest:
            item(line)
        mine = step("Your record (juke#)", record_call())
        for line in record_lines(mine):
            say(line)
    except JukeError as error:
        report_error(error, out)
        return 1
    return 0 if write_done or not wants_write else 1


def report_error(error: JukeError, out: Output) -> None:
    hint = HINTS.get(error.code)
    what = "no answer from juke" if error.status == 0 else f"juke answered {error.status} {error.code}"
    out(f"   !! {what}" + (f": {hint}" if hint else ""))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def credits_arg(value: str) -> str:
    if not CREDITS_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("credits is a number as text, for example 25")
    return value


def order_id_arg(value: str) -> str:
    if not ORDER_ID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("an order id is 6 to 64 letters, digits, _ or -")
    return value


def probability_arg(value: str) -> float:
    try:
        probability = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("a probability is a number, for example 0.62") from None
    if not 0 < probability < 1:
        raise argparse.ArgumentTypeError("a probability is above 0 and below 1")
    return probability


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Walk juke's Builder API: games, markets, one optional paper pick or forecast, then your record. "
        "A dry run unless you pass --send."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the requests and send nothing (the default; no key needed)")
    mode.add_argument("--send", action="store_true", help="send the requests for real (needs JUKE_API_KEY)")
    write = parser.add_mutually_exclusive_group()
    write.add_argument("--pick", action="store_true", help="include one paper pick (permanent: it goes on your system's record)")
    write.add_argument("--forecast", type=probability_arg, metavar="P", help="include one forecast: your chance (0 to 1) that the chosen market resolves YES")
    parser.add_argument("--arena", choices=sorted(ARENAS), default="football")
    parser.add_argument("--side", choices=["YES", "NO"], default="YES", help="the side --pick buys (default YES)")
    parser.add_argument("--credits", type=credits_arg, default="25", help="paper Credits for --pick, as text (default 25)")
    parser.add_argument("--order-id", type=order_id_arg, help="your id for the pick; send the same one to retry safely (default: a new one)")
    parser.add_argument("--raw", action="store_true", help="also print each full answer from juke")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    out: Output = print,
    err: Output = lambda text: print(text, file=sys.stderr),
) -> int:
    env = os.environ if env is None else env
    args = parse_args(argv)
    try:
        base_url = check_base_url(env.get("JUKE_BASE_URL") or DEFAULT_BASE_URL)
    except ValueError as error:
        err(f"error: {error}")
        return 2
    order_id = args.order_id or f"example-{uuid.uuid4().hex[:16]}"
    if not args.send:
        dry_run(args, base_url, order_id, out)
        return 0
    key = env.get("JUKE_API_KEY", "").strip()
    if not KEY_PATTERN.fullmatch(key):
        err("error: set JUKE_API_KEY to your system's key (jk_...). See README.md, or leave out --send for a dry run.")
        return 2
    return run(JukeClient(key, base_url), args, order_id, out)


if __name__ == "__main__":
    sys.exit(main())
