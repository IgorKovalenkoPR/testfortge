"""Tests for engine.recorder_parser — Playwright codegen output → AutomationStep[]."""
from __future__ import annotations

from engine.recorder_parser import parse_codegen_output


CODEGEN_LOGIN_FLOW = '''
import re
from playwright.async_api import Playwright, async_playwright, expect


async def run(playwright: Playwright) -> None:
    browser = await playwright.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto("https://app.example.com/login")
    await page.get_by_label("Email").fill("user@example.com")
    await page.get_by_label("Password").fill("hunter2")
    await page.get_by_role("button", name="Sign in").click()
    await context.close()
    await browser.close()


async def main() -> None:
    async with async_playwright() as playwright:
        await run(playwright)
'''


def test_basic_login_flow():
    steps = parse_codegen_output(CODEGEN_LOGIN_FLOW)
    assert len(steps) == 4
    assert steps[0].action == "goto"
    assert steps[0].target == "https://app.example.com/login"
    assert steps[1].action == "fill"
    assert steps[1].target == "label=Email"
    assert steps[1].value == "user@example.com"
    assert steps[2].action == "fill"
    assert steps[2].target == "label=Password"
    assert steps[2].value == "hunter2"
    assert steps[3].action == "click"
    assert steps[3].target == 'role=button[name="Sign in"]'


def test_test_id_locator():
    src = '''
async def run(playwright):
    await page.get_by_test_id("submit-btn").click()
'''
    steps = parse_codegen_output(src)
    assert len(steps) == 1
    assert steps[0].action == "click"
    assert steps[0].target == "data-testid=submit-btn"


def test_press_keyboard():
    src = '''
async def run(playwright):
    await page.get_by_label("Search").press("Enter")
'''
    steps = parse_codegen_output(src)
    assert len(steps) == 1
    assert steps[0].action == "press"
    assert steps[0].value == "Enter"


def test_select_dropdown():
    src = '''
async def run(playwright):
    await page.get_by_label("Country").select_option("UA")
'''
    steps = parse_codegen_output(src)
    assert len(steps) == 1
    assert steps[0].action == "select"
    assert steps[0].target == "label=Country"
    assert steps[0].value == "UA"


def test_nested_locator_chain():
    src = '''
async def run(playwright):
    await page.locator("#sidebar").get_by_text("Settings").click()
'''
    steps = parse_codegen_output(src)
    assert len(steps) == 1
    assert steps[0].action == "click"
    assert steps[0].target == "#sidebar >> text=Settings"


def test_fill_with_apostrophe_value():
    src = '''
async def run(playwright):
    await page.get_by_label("Name").fill("O'Reilly")
'''
    steps = parse_codegen_output(src)
    assert len(steps) == 1
    assert steps[0].action == "fill"
    assert steps[0].value == "O'Reilly"


def test_check_and_uncheck():
    src = '''
async def run(playwright):
    await page.get_by_label("Subscribe").check()
    await page.get_by_label("Marketing").uncheck()
'''
    steps = parse_codegen_output(src)
    assert len(steps) == 2
    assert steps[0].action == "check"
    assert steps[0].target == "label=Subscribe"
    assert steps[0].value == ""
    assert steps[1].action == "check"
    assert steps[1].target == "label=Marketing"
    assert steps[1].value == "false"


def test_ignores_browser_lifecycle():
    """Lines like browser.close(), context.new_page() must NOT become steps."""
    src = '''
async def run(playwright):
    browser = await playwright.chromium.launch()
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto("https://x.com")
    await context.close()
    await browser.close()
'''
    steps = parse_codegen_output(src)
    assert len(steps) == 1
    assert steps[0].action == "goto"
    assert steps[0].target == "https://x.com"


def test_placeholder_alt_title_role_without_name():
    src = '''
async def run(playwright):
    await page.get_by_placeholder("Search...").click()
    await page.get_by_alt_text("Logo").click()
    await page.get_by_title("Help").click()
    await page.get_by_role("link").click()
'''
    steps = parse_codegen_output(src)
    assert [s.target for s in steps] == [
        "placeholder=Search...",
        "alt=Logo",
        "title=Help",
        "role=link",
    ]


def test_preserves_source_order():
    """Steps must come back in the same order they appear in the source."""
    src = '''
async def run(playwright):
    await page.goto("https://a.test")
    await page.get_by_role("button", name="One").click()
    await page.get_by_role("button", name="Two").click()
    await page.get_by_role("button", name="Three").click()
'''
    steps = parse_codegen_output(src)
    assert [s.value if s.action == "goto" else s.target for s in steps] == [
        "",  # goto's value is empty, target carries URL
        'role=button[name="One"]',
        'role=button[name="Two"]',
        'role=button[name="Three"]',
    ]


def test_empty_source():
    assert parse_codegen_output("") == []


def test_malformed_syntax_returns_empty():
    """A truncated recording (CLI killed mid-write) should not crash callers."""
    assert parse_codegen_output("async def run(playwright):\n    await page.go") == []


def test_non_codegen_python_yields_no_steps():
    """Arbitrary Python without page.X calls returns []."""
    assert parse_codegen_output("x = 1\nprint('hello world')\ndef f(): return 42") == []


def test_raw_field_carries_unparsed_source():
    src = '''
async def run(playwright):
    await page.get_by_role("button", name="Save").click()
'''
    steps = parse_codegen_output(src)
    assert len(steps) == 1
    # ast.unparse strips redundant whitespace but preserves semantics.
    assert "page.get_by_role" in steps[0].raw
    assert "click" in steps[0].raw
