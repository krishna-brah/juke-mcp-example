"""Tests for juke_example.py. Run from the repository root: python -m unittest -v

No network and no key are needed. A tiny local server stands in for the Builder API;
its answers are synthetic and only follow the shape of the real ones (see the README).
"""

import json
import re
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests

import juke_example as ex

# Built at run time so that no key-shaped text sits in the repository.
FAKE_KEY = "jk_" + "A" * 43
API = "/api/v3-beta/builder"

WEEK = {"cycleId": "test-week", "startsAt": "2099-01-01T00:00:00Z", "endsAt": "2099-01-08T00:00:00Z"}


def listing(listing_id, question, kind, yes=None, no=None, state="OPEN"):
    row = {
        "listingId": listing_id, "question": question, "marketLifecycleState": state,
        "sportsMarketPresentation": {"marketType": kind, "marketTypeLabel": kind.title()},
    }
    if yes is not None:
        row["indicative"] = {"yes": yes, "no": no}  # prices arrive as strings
    return row


def game(event_id, title, state, closes_at, markets=1):
    return {"eventId": event_id, "eventTitle": title, "eventLifecycleState": state, "actionClosesAt": closes_at, "marketCount": markets}


SUMMARY = {"week": WEEK, "board": {"arenas": [{"arenaId": "arena:football", "book": None, "listings": [], "eventGroups": [
    game("event:finished", "Team C at Team D", "FINAL", "2000-01-01T00:00:00Z"),
    game("event:later", "Team E at Team F", "OPEN", "2099-01-03T00:00:00Z"),
    game("event:sooner", "Team A at Team B", "OPEN", "2099-01-02T00:00:00Z", markets=3),
]}]}}
EVENT = {"week": WEEK, "board": {"arenas": [{"arenaId": "arena:football", "listings": [
    listing("listing:total", "Total points over 40.5?", "total", "0.40", "0.61"),
    listing("listing:closed", "Will Team A cover?", "spread", state="CLOSED_AWAITING_RESULT"),
    listing("listing:winner", "Will Team A win?", "moneyline", "0.57", "0.44"),
]}]}}
POSITIONS = {"availability": "AVAILABLE", "positions": [
    {"positionId": "position:one", "listingId": "listing:winner", "side": "YES", "status": "OPEN", "shares": "43.9", "costBasis": "25"},
    {"positionId": "position:two", "listingId": "listing:total", "side": "NO", "status": "SETTLED", "shares": "10", "costBasis": "5"},
]}
RECORD = {"state": "AVAILABLE", "gradedPicks": 0, "games": 0, "verdicts": [
    {"cellId": "OVERALL", "label": "INSUFFICIENT_EVIDENCE", "picks": 0, "events": 0, "picksNeeded": 30, "eventsNeeded": 15},
]}


class FakeJuke:
    """A local stand-in for the Builder API: scripted answers, and a log of every request it receives."""

    def __init__(self, summary=SUMMARY, pick=(200, {"ok": True, "orderId": "echo", "positionId": "position:new"}),
                 forecast=(200, {"ok": True, "forecastId": "forecast:new", "inPlay": False})):
        self.requests = []
        self.answers = {"summary": (200, summary), "event": (200, EVENT), "positions": (200, POSITIONS), "record": (200, RECORD),
                        "pick": pick, "forecast": forecast}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self):
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                query = {key: values[0] for key, values in parse_qs(url.query).items()}
                fake.requests.append({"method": self.command, "path": url.path, "query": query, "body": body,
                                      "authorization": self.headers.get("Authorization"), "content_type": self.headers.get("Content-Type")})
                status, payload = fake.answer(self.command, url.path, query)
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "https://example.invalid/elsewhere")
                self.send_header("Content-Type", "application/json" if isinstance(payload, dict) else "text/html")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode() if isinstance(payload, dict) else payload)

            do_GET = do_POST = handle_any

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def answer(self, method, path, query):
        if path == f"{API}/board":
            return self.answers["event" if "eventId" in query else "summary"]
        if path == f"{API}/pick":
            return self.answers["pick"]
        if path == f"{API}/forecast":
            return self.answers["forecast"]
        if path == f"{API}/positions":
            return self.answers["positions"]
        if path == f"{API}/record":
            return self.answers["record"]
        if path == "/redirect":
            return 302, {}
        if path == "/html":
            return 502, b"<html>bad gateway</html>"
        return 404, {"ok": False, "code": "NOT_FOUND"}

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/api/v3-beta"

    @property
    def env(self):
        return {"JUKE_API_KEY": FAKE_KEY, "JUKE_BASE_URL": self.base_url}

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def seen(self):
        """(method, path after /builder, query keys, body keys) for every request, in order."""
        return [(r["method"], r["path"].removeprefix(API), sorted(r["query"]), sorted(r["body"] or {})) for r in self.requests]


def run_main(argv, env=None):
    out, err = [], []
    code = ex.main(argv, env={} if env is None else env, out=out.append, err=err.append)
    return code, "\n".join(out), "\n".join(err)


class AddressTests(unittest.TestCase):
    def test_urls_keep_colons_and_placeholders_readable(self):
        call = ex.event_call("arena:football", "<eventId-from-step-1>")
        self.assertEqual(call.url(ex.DEFAULT_BASE_URL), f"{ex.DEFAULT_BASE_URL}/builder/board?arenaId=arena:football&eventId=<eventId-from-step-1>")

    def test_the_key_only_goes_to_juke_or_localhost(self):
        for ok in ("https://www.getjuked.io/api/v3-beta", "https://getjuked.io/x/", "http://localhost:3000/api", "http://127.0.0.1:9/api"):
            with self.subTest(ok=ok):
                self.assertEqual(ex.check_base_url(ok), ok.rstrip("/"))
        for refused in ("http://www.getjuked.io/api", "https://example.com/api", "https://getjuked.io.example.com/api",
                        "https://evilgetjuked.io/api", "https://" "user:pass" "@www.getjuked.io/api", "http://127.0.0.1.example.com/api", "ftp://localhost/"):
            with self.subTest(refused=refused):
                with self.assertRaises(ValueError):
                    ex.check_base_url(refused)


class ReadingTests(unittest.TestCase):
    def test_open_games_skips_finished_ones_and_puts_the_soonest_first(self):
        self.assertEqual([g["eventId"] for g in ex.open_games(SUMMARY)], ["event:sooner", "event:later"])

    def test_open_games_falls_back_to_the_time_picks_stop_when_there_is_no_state(self):
        summary = {"board": {"arenas": [{"eventGroups": [
            {"eventId": "event:past", "actionClosesAt": "2000-01-01T00:00:00Z"},
            {"eventId": "event:future", "actionClosesAt": "2099-01-01T00:00:00Z"},
        ]}]}}
        self.assertEqual([g["eventId"] for g in ex.open_games(summary, now="2026-11-04T00:00:00Z")], ["event:future"])

    def test_no_arena_means_no_games(self):
        self.assertEqual(ex.open_games({"board": {"arenas": []}}), [])
        self.assertEqual(ex.open_games({}), [])

    def test_live_price_reads_two_sided_wide_and_one_sided_prices_as_numbers(self):
        self.assertEqual(ex.live_price({"indicative": {"yes": "0.57", "no": "0.44"}}, "YES"), 0.57)
        self.assertEqual(ex.live_price({"indicative": {"yes": "0.57", "no": "0.44"}}, "NO"), 0.44)
        self.assertEqual(ex.live_price({"indicativeWide": {"yes": "0.30", "no": "0.80"}}, "NO"), 0.80)
        one_sided = {"indicativeOneSided": {"side": "NO", "price": "0.90"}}
        self.assertEqual(ex.live_price(one_sided, "NO"), 0.90)
        self.assertIsNone(ex.live_price(one_sided, "YES"))
        self.assertIsNone(ex.live_price({}, "YES"))
        self.assertIsNone(ex.live_price({"indicative": {"yes": "1.00", "no": "0"}}, "YES"))

    def test_choose_market_prefers_a_winner_market_that_has_a_price_for_the_side(self):
        listings = EVENT["board"]["arenas"][0]["listings"]
        market, price = ex.choose_market(listings, "YES")
        self.assertEqual((market["listingId"], price), ("listing:winner", 0.57))
        self.assertEqual(ex.choose_market(listings[:2], "NO")[0]["listingId"], "listing:total")
        self.assertIsNone(ex.choose_market([listings[1]], "YES"))


class ClientTests(unittest.TestCase):
    def test_it_sends_the_key_as_a_bearer_header_and_a_json_body(self):
        with FakeJuke() as fake:
            client = ex.JukeClient(FAKE_KEY, fake.base_url)
            client.send(ex.record_call())
            client.send(ex.forecast_call("listing:winner", 0.62))
        read, write = fake.requests
        self.assertEqual(read["authorization"], f"Bearer {FAKE_KEY}")
        self.assertEqual(write["authorization"], f"Bearer {FAKE_KEY}")
        self.assertEqual(write["content_type"], "application/json")
        self.assertEqual(write["body"], {"listingId": "listing:winner", "probability": 0.62})

    def test_error_answers_become_juke_errors_with_the_api_code(self):
        with FakeJuke(pick=(409, {"ok": False, "code": "PRICE_MOVED"})) as fake:
            client = ex.JukeClient(FAKE_KEY, fake.base_url)
            with self.assertRaises(ex.JukeError) as caught:
                client.send(ex.pick_call("arena:football", "listing:winner", "YES", "25", "order-0001", 0.6))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "PRICE_MOVED"))

    def test_a_non_json_error_still_raises(self):
        with FakeJuke() as fake:
            client = ex.JukeClient(FAKE_KEY, fake.base_url.replace("/api/v3-beta", ""))
            with self.assertRaises(ex.JukeError) as caught:
                client.send(ex.Call("GET", "/html"))
        self.assertEqual((caught.exception.status, caught.exception.code), (502, "HTTP_502"))

    def test_a_redirect_is_not_followed_so_the_key_stays_put(self):
        with FakeJuke() as fake:
            client = ex.JukeClient(FAKE_KEY, fake.base_url.replace("/api/v3-beta", ""))
            with self.assertRaises(ex.JukeError) as caught:
                client.send(ex.Call("GET", "/redirect"))
        self.assertEqual((caught.exception.status, caught.exception.code), (302, "HTTP_302"))
        self.assertEqual(len(fake.requests), 1)

    def test_no_answer_is_reported_as_such(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        client = ex.JukeClient(FAKE_KEY, f"http://127.0.0.1:{port}/api/v3-beta", timeout=2)
        with self.assertRaises(ex.JukeError) as caught:
            client.send(ex.record_call())
        self.assertEqual((caught.exception.status, caught.exception.code), (0, "NO_ANSWER"))


class SendTests(unittest.TestCase):
    def test_send_alone_only_reads_and_in_order(self):
        with FakeJuke() as fake:
            code, out, _ = run_main(["--send"], fake.env)
        self.assertEqual(code, 0)
        self.assertEqual(fake.seen(), [
            ("GET", "/board", ["arenaId", "view"], []),
            ("GET", "/board", ["arenaId", "eventId"], []),
            ("GET", "/positions", [], []),
            ("GET", "/record", [], []),
        ])
        self.assertEqual(fake.requests[1]["query"]["eventId"], "event:sooner")  # the game that stops taking picks first
        self.assertIn("positions: 2, open: 1", out)
        self.assertIn("overall: INSUFFICIENT_EVIDENCE; still needs 30 picks across 15 games", out)

    def test_pick_buys_the_chosen_market_with_a_limit_above_the_live_price(self):
        with FakeJuke() as fake:
            code, out, _ = run_main(["--send", "--pick", "--order-id", "order-0001"], fake.env)
        self.assertEqual(code, 0)
        post = next(r for r in fake.requests if r["method"] == "POST")
        self.assertEqual(post["path"], f"{API}/pick")
        self.assertEqual(post["body"], {"arenaId": "arena:football", "listingId": "listing:winner", "side": "YES",
                                        "credits": "25", "orderId": "order-0001", "limitPrice": 0.59})
        self.assertIn("placed: orderId echo, positionId position:new", out)

    def test_a_no_pick_is_limited_from_the_no_price(self):
        with FakeJuke() as fake:
            run_main(["--send", "--pick", "--side", "NO", "--credits", "10", "--order-id", "order-0002"], fake.env)
        post = next(r for r in fake.requests if r["method"] == "POST")
        self.assertEqual((post["body"]["side"], post["body"]["credits"], post["body"]["limitPrice"]), ("NO", "10", 0.46))

    def test_forecast_sends_a_probability_for_yes(self):
        with FakeJuke() as fake:
            code, out, _ = run_main(["--send", "--forecast", "0.62"], fake.env)
        post = next(r for r in fake.requests if r["method"] == "POST")
        self.assertEqual((code, post["path"], post["body"]), (0, f"{API}/forecast", {"listingId": "listing:winner", "probability": 0.62}))
        self.assertIn("recorded: forecastId forecast:new", out)

    def test_a_refused_pick_is_explained_and_the_reads_still_happen(self):
        with FakeJuke(pick=(409, {"ok": False, "code": "PRICE_MOVED"})) as fake:
            code, out, _ = run_main(["--send", "--pick"], fake.env)
        self.assertEqual(code, 1)
        self.assertIn("juke answered 409 PRICE_MOVED", out)
        self.assertIn("use a new --order-id", out)
        self.assertEqual([r["path"].removeprefix(API) for r in fake.requests][-2:], ["/positions", "/record"])

    def test_a_replayed_pick_says_nothing_traded_again(self):
        with FakeJuke(pick=(200, {"ok": True, "orderId": "echo", "positionId": "position:new", "replayed": True})) as fake:
            _, out, _ = run_main(["--send", "--pick"], fake.env)
        self.assertIn("replayed", out)

    def test_a_failed_read_stops_the_run_with_the_api_code(self):
        with FakeJuke() as fake:
            fake.answers["summary"] = (401, {"ok": False, "code": "SYSTEM_KEY_INVALID"})
            code, out, _ = run_main(["--send", "--pick"], fake.env)
        self.assertEqual(code, 1)
        self.assertIn("SYSTEM_KEY_INVALID", out)
        self.assertEqual(len(fake.requests), 1)

    def test_with_no_open_game_nothing_is_written(self):
        empty = {"week": WEEK, "board": {"arenas": [{"arenaId": "arena:football", "eventGroups": [game("event:finished", "Team C at Team D", "FINAL", "2000-01-01T00:00:00Z")]}]}}
        with FakeJuke(summary=empty) as fake:
            code, out, _ = run_main(["--send", "--pick"], fake.env)
        self.assertEqual(code, 1)
        self.assertNotIn("POST", [r["method"] for r in fake.requests])
        self.assertIn("nothing was written", out)

    def test_the_key_is_never_printed(self):
        with FakeJuke(pick=(409, {"ok": False, "code": "PRICE_MOVED"})) as fake:
            _, out, err = run_main(["--send", "--pick", "--raw"], fake.env)
        self.assertNotIn(FAKE_KEY, out + err)
        self.assertNotIn(FAKE_KEY[3:], out + err)


class DryRunTests(unittest.TestCase):
    def test_nothing_is_sent_and_no_key_is_needed(self):
        with mock.patch.object(requests.Session, "request", side_effect=AssertionError("a dry run must not touch the network")):
            code, out, err = run_main(["--pick", "--order-id", "order-0001"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("DRY RUN", out)
        self.assertIn("POST https://www.getjuked.io/api/v3-beta/builder/pick", out)
        self.assertIn('"orderId": "order-0001"', out)
        self.assertIn("<listingId-of-the-market-chosen-in-step-2>", out)

    def test_dry_run_is_the_default_and_the_flag_says_so_explicitly(self):
        self.assertEqual(run_main([])[1], run_main(["--dry-run"])[1])

    def test_the_write_appears_only_when_asked_for(self):
        self.assertNotIn("POST", run_main([])[1])
        self.assertIn("/builder/forecast", run_main(["--forecast", "0.62"])[1])
        self.assertIn("/builder/pick", run_main(["--pick"])[1])

    def test_a_key_in_the_environment_is_never_printed(self):
        _, out, err = run_main(["--pick"], {"JUKE_API_KEY": FAKE_KEY})
        self.assertNotIn(FAKE_KEY, out + err)

    def test_the_plan_matches_what_a_real_run_sends(self):
        for argv in ([], ["--pick"], ["--forecast", "0.62"]):
            with self.subTest(argv=argv), FakeJuke() as fake:
                run_main(["--send", *argv, "--order-id", "order-0001"], fake.env)
                args = ex.parse_args([*argv, "--order-id", "order-0001"])
                planned = [(c.method, c.path.removeprefix("/builder"), sorted(c.params or {}), sorted(c.body or {})) for _, c in ex.plan_steps(args, "order-0001")]
                self.assertEqual(planned, fake.seen())


class CommandLineTests(unittest.TestCase):
    def test_send_without_a_key_stops_before_any_request(self):
        with mock.patch.object(requests.Session, "request", side_effect=AssertionError("no request without a key")):
            code, out, err = run_main(["--send"])
        self.assertEqual(code, 2)
        self.assertIn("JUKE_API_KEY", err)
        self.assertEqual(out, "")

    def test_a_key_with_a_stray_line_break_is_trimmed_not_sent_broken(self):
        with FakeJuke() as fake:
            code, _, _ = run_main(["--send"], {**fake.env, "JUKE_API_KEY": FAKE_KEY + "\n"})
        self.assertEqual(code, 0)
        self.assertEqual(fake.requests[0]["authorization"], f"Bearer {FAKE_KEY}")

    def test_the_placeholder_from_env_example_is_refused(self):
        code, _, err = run_main(["--send"], {"JUKE_API_KEY": "jk_your_system_key"})
        self.assertEqual(code, 2)
        self.assertIn("JUKE_API_KEY", err)

    def test_a_base_url_that_could_leak_the_key_is_refused_even_for_a_dry_run(self):
        code, _, err = run_main([], {"JUKE_BASE_URL": "https://example.com/api"})
        self.assertEqual(code, 2)
        self.assertIn("never sent anywhere else", err)

    def test_bad_arguments_are_refused(self):
        for argv in (["--credits", "-5"], ["--credits", "lots"], ["--forecast", "1.5"], ["--forecast", "0"], ["--order-id", "no"],
                     ["--pick", "--forecast", "0.5"], ["--send", "--dry-run"], ["--arena", "tennis"], ["--side", "MAYBE"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit) as caught:
                    ex.parse_args(argv)
                self.assertEqual(caught.exception.code, 2)

    def test_a_new_order_id_is_made_when_none_is_given_and_it_is_valid(self):
        _, out, _ = run_main(["--pick"])
        order_id = re.search(r'"orderId": "([^"]+)"', out).group(1)
        self.assertIsNotNone(ex.ORDER_ID_PATTERN.fullmatch(order_id))


class RepositoryTests(unittest.TestCase):
    def test_no_key_shaped_text_is_committed(self):
        root = Path(__file__).resolve().parent.parent
        skip = {".git", ".venv", "venv", "__pycache__"}
        for path in root.rglob("*"):
            if path.is_file() and not skip & set(path.relative_to(root).parts) and path.name != ".env":
                text = path.read_text(errors="ignore")
                self.assertIsNone(re.search(r"jk_[A-Za-z0-9_-]{40,}", text), f"key-shaped text in {path.relative_to(root)}")


if __name__ == "__main__":
    unittest.main()
