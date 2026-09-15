"""When generation does not finish, the screen has to say why.

The operator's screenshot: a modal reading *"Generation could not finish —
try again"*, *"Server returned errors — retrying directly"*, and a Retry
button, after 1 m 25 s. Three separate things were wrong with that screen
and none of them was the generation itself:

* the poll loop interpreted exactly one status code, 404. A **401
  `session_expired`** — which `engine/session_timeout` returns to a JSON
  caller, with a message saying what happened — counted as one of ten
  consecutive "errors" and was reported as an unstable server;
* "retrying directly" described a synchronous fallback that was
  deliberately removed for 502'ing the single worker. Nothing was
  retrying, and the operator waited for it;
* the asynchronous worker — the path the UI takes — asked the generator for
  no crawl diagnostics and returned none, so a blocked or timed-out crawl
  produced a thinner pack with nothing on screen to account for it. The
  synchronous POST has collected and flashed those all along.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = pathlib.Path(__file__).resolve().parent.parent
PAGE = (REPO / "templates" / "test_cases.html").read_text(encoding="utf-8")


class TestThePollLoopTellsTheTruth:

    def test_a_session_that_ended_is_not_reported_as_a_server_fault(self):
        assert "session_expired" in PAGE and "res.status === 401" in PAGE, (
            "the submit path handled expiry and the poll loop did not, so "
            "an expiry mid-run printed 'Server returned errors'")
        # And it routes to the reload prompt rather than the generic one.
        expiry_block = PAGE.split("res.status === 401", 1)[1][:400]
        assert "showReloadRequired()" in expiry_block

    def test_the_servers_own_explanation_is_shown(self):
        assert "res.body.message" in PAGE, (
            "the reason was parsed and then discarded in favour of a "
            "fixed sentence, so every distinct failure read the same")

    def test_nothing_claims_to_be_retrying(self):
        from engine.i18n import en, ua
        for name, table in (("en", en), ("ua", ua)):
            strings = table.TRANSLATIONS
            assert "retrying directly" not in strings["tc_gen_unstable"], name
            assert "повторюємо напряму" not in strings["tc_gen_unstable"], name
            assert ("Retry" in strings["tc_gen_unstable"]
                    or "Повторити" in strings["tc_gen_unstable"]), (
                f"{name}: now that it no longer claims a retry is under "
                f"way, it has to name the control that actually starts one")

    def test_the_network_case_has_its_own_key(self):
        """It used to reuse ``tc_gen_offline`` with a *different* English
        default from the one the dictionary defines, so a transport
        failure and a logic failure printed the same sentence."""
        from engine.i18n import en, ua
        for table in (en, ua):
            assert "tc_gen_poll_network" in table.TRANSLATIONS
        assert "tc_gen_poll_network" in PAGE


class TestTheAsyncWorkerReportsWhatItCouldNotReach:

    def test_it_asks_the_generator_for_crawl_errors(self):
        source = (REPO / "routes" / "generation.py").read_text(
            encoding="utf-8")
        worker = source.split("def _worker(", 1)[1].split(
            "job_id = get_queue().submit", 1)[0]
        assert "crawl_errors_out=crawl_errors" in worker, (
            "the sync POST passes this and the async worker did not, so "
            "the path the UI takes reported no crawl failure ever")
        assert '"crawl_errors": crawl_errors' in worker

    def test_the_drain_flashes_them(self):
        source = (REPO / "routes" / "generation.py").read_text(
            encoding="utf-8")
        drain = source.split("def _drain_tc_job_into_session", 1)[1].split(
            "def _drain_cl_job_into_session", 1)[0]
        assert "crawl_errors" in drain and "crawl_partial" in drain

    def test_a_url_that_yields_nothing_is_not_silent(self, monkeypatch):
        """``_run_site_aware`` returns ``None`` when the crawl itself
        failed. That was an ``if`` with no ``else``: the pack came back
        shorter and the screen said nothing."""
        source = (REPO / "routes" / "generation.py").read_text(
            encoding="utf-8")
        worker = source.split("def _worker(", 1)[1].split(
            "job_id = get_queue().submit", 1)[0]
        assert "returned nothing" in worker
