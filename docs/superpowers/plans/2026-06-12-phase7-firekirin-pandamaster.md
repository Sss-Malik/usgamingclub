# Phase 7 — Firekirin + Pandamaster (3.0.303 portal aliases) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `firekirin` and `pandamaster` as registry aliases of the existing `MilkyWayBackend` (search-row-Balance variant of the 3.0.303 ASP.NET cashier family), and patch the shared `_aspnet_cashier` parser+client to tolerate Pandamaster's missing `__VIEWSTATEGENERATOR`.

**Architecture:** Pure registry + tolerance work, no new backend module. `parse_viewstate()` promotes `__VIEWSTATEGENERATOR` from required → optional; client POST builders skip it when absent ("send exactly the hidden fields each GET returned" per findings §3). Two new named driver frozensets in the registry: `_ORIONSTARS_FAMILY_DRIVERS = {"orionstars"}` and `_MILKYWAY_FAMILY_DRIVERS = {"milkyway", "firekirin", "pandamaster"}`. Both new drivers added to `NON_IDEMPOTENT_DRIVERS`.

**Tech Stack:** Python 3.12, no new dependencies. Uses existing `_aspnet_cashier` shared package + `MilkyWayBackend`.

**Findings doc:** `/Applications/development/orionstars-standalone/api_findings.md` — updated 2026-06-12 with the 4-portal family table (§top), the Balance-column variant note (§4.1), and per-portal quirks (§top callouts: Pandamaster omits `__VIEWSTATEGENERATOR`; Firekirin on `:8888`; Pandamaster on default 443).

**Branch:** `feat/phase7-firekirin-pandamaster` (already created)

---

## Conventions

- Every task ends with `make lint && make type && make test` and a commit on `feat/phase7-firekirin-pandamaster`.
- All unit tests live in `tests/unit/` flat.
- HTTP mocking uses `respx`; Redis tests use the existing `fake_redis` fixture in `tests/conftest.py`.

---

## Task 1: Parser tolerance for missing `__VIEWSTATEGENERATOR`

Per findings doc, Pandamaster's `default.aspx` and `AccountsList.aspx` GETs do NOT include the `__VIEWSTATEGENERATOR` hidden field. The `ctl16` search postback still works without it. Promote the field from required to optional, matching how `event_validation` already works.

**Files:**
- Modify: `/Applications/development/python/casino-app-automation/app/backends/_aspnet_cashier/parsers.py:5-36`
- Modify: `/Applications/development/python/casino-app-automation/tests/unit/test_aspnet_parsers.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_aspnet_parsers.py`:

```python
def test_viewstate_generator_is_none_when_absent():
    """Pandamaster's default.aspx and AccountsList.aspx omit __VIEWSTATEGENERATOR.
    The parser must tolerate this and return None rather than raising.
    Findings doc top-section callout: Pandamaster runs on 443 and omits VSG."""
    html = """
    <form id="form1">
      <input type="hidden" name="__VIEWSTATE" value="dDwxNDc=" />
      <input type="hidden" name="__SCROLLPOSITIONX" value="0" />
    </form>
    """
    vs = parse_viewstate(html)
    assert vs.viewstate == "dDwxNDc="
    assert vs.viewstate_generator is None
    assert vs.event_validation is None


def test_viewstate_still_raises_when_viewstate_itself_missing():
    """__VIEWSTATE remains mandatory — only the generator becomes optional."""
    with pytest.raises(ValueError, match="__VIEWSTATE"):
        parse_viewstate("<form><input type='hidden' name='__VIEWSTATEGENERATOR' value='G' /></form>")
```

- [ ] **Step 2: Run; expect failures**

Run: `.venv/bin/pytest tests/unit/test_aspnet_parsers.py::test_viewstate_generator_is_none_when_absent tests/unit/test_aspnet_parsers.py::test_viewstate_still_raises_when_viewstate_itself_missing -v`
Expected: 2 failures (current code raises on missing `__VIEWSTATEGENERATOR`).

- [ ] **Step 3: Update the dataclass + parser**

Edit `app/backends/_aspnet_cashier/parsers.py`. Replace the `ViewState` dataclass and `parse_viewstate` function (lines 5-36) with:

```python
@dataclass(frozen=True)
class ViewState:
    viewstate: str
    viewstate_generator: str | None  # Pandamaster omits this on default.aspx + AccountsList.aspx
    event_validation: str | None     # None on pages with EnableEventValidation="false" (AccountsList.aspx)


_HIDDEN_RE = re.compile(
    r'<input[^>]*name=["\'](?P<name>__[A-Z]+)["\'][^>]*value=["\'](?P<value>[^"\']*)["\']',
    re.IGNORECASE,
)


def parse_viewstate(html: str) -> ViewState:
    """Scrape ASP.NET hidden fields from a rendered form.

    __VIEWSTATE is required (no portal ships without it). __VIEWSTATEGENERATOR is optional —
    Pandamaster omits it on default.aspx and AccountsList.aspx; per findings §3 we must "send
    exactly the hidden fields the GET actually contained", and the ctl16 search postback works
    without it. __EVENTVALIDATION is page-specific: dialog pages (GrantTreasure, ChangeTreasure,
    ResetPassWord, CreateAccount) include it; AccountsList.aspx does not.
    """
    fields: dict[str, str] = {}
    for m in _HIDDEN_RE.finditer(html):
        fields[m.group("name").upper()] = m.group("value")
    if "__VIEWSTATE" not in fields:
        raise ValueError("__VIEWSTATE not found in form HTML")
    return ViewState(
        viewstate=fields["__VIEWSTATE"],
        viewstate_generator=fields.get("__VIEWSTATEGENERATOR"),
        event_validation=fields.get("__EVENTVALIDATION"),
    )
```

- [ ] **Step 4: Run new tests; expect pass**

Run: `.venv/bin/pytest tests/unit/test_aspnet_parsers.py -v`
Expected: all parser tests pass (the previously-passing test `test_viewstate_scrapes_all_three_hidden_fields` still passes because it has `__VIEWSTATEGENERATOR` present, and `test_viewstate_eventvalidation_is_none_when_absent` still passes because the new dataclass field is `str | None`).

- [ ] **Step 5: Confirm an existing test that asserts the old "raises" behaviour was removed by this change**

The old test `test_viewstate_raises_when_required_field_missing` asserted `parse_viewstate("<form></form>")` raises `ValueError` matching `__VIEWSTATE` — that still holds (we kept `__VIEWSTATE` as mandatory). Verify:

Run: `.venv/bin/pytest tests/unit/test_aspnet_parsers.py::test_viewstate_raises_when_required_field_missing -v`
Expected: pass.

- [ ] **Step 6: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green. Test count: +2 (the two new tests added in Step 1). mypy may flag the 4 call sites in `client.py` + `orionstars/backend.py` that build form bodies with `vs.viewstate_generator` as `str | None` going into a `dict[str, str]` — those will be fixed in Task 2, but for this commit, run mypy to capture the current state. If mypy DOES flag these as type errors (which it should), proceed to Step 7 — Task 2 lands the fix.

If mypy fails on this commit only because of the `str | None` → `dict[str, str]` mismatch, that's expected and Task 2 will resolve it. To keep the commit chain bisectable, optionally add `# type: ignore[dict-item]` at the 4 sites in this commit and remove them in Task 2. But the simpler path is to land Tasks 1+2 as a single commit if mypy doesn't tolerate a temporary state.

**Recommended:** Roll Tasks 1+2 into a single commit by deferring Step 7 (the commit) to the end of Task 2. Mark this step as deferred and proceed to Task 2.

- [ ] **Step 7 (DEFERRED):** Commit is rolled into Task 2's final commit.

---

## Task 2: Client tolerance — skip `__VIEWSTATEGENERATOR` when absent

Update the 4 sites that include `__VIEWSTATEGENERATOR` in POST bodies. Each must skip the field when `vs.viewstate_generator is None`, mirroring how `event_validation` is already handled in `submit_dialog`.

**Sites to fix:**
- `app/backends/_aspnet_cashier/client.py:175-183` (`search_account` form body)
- `app/backends/_aspnet_cashier/client.py:206-213` (`submit_dialog` form body)
- `app/backends/_aspnet_cashier/client.py:240-249` (`milkyway_read_balance` form body)
- `app/backends/orionstars/backend.py:110-120` (`create_account` form body)

**Files:**
- Modify: `/Applications/development/python/casino-app-automation/app/backends/_aspnet_cashier/client.py`
- Modify: `/Applications/development/python/casino-app-automation/app/backends/orionstars/backend.py`
- Modify: `/Applications/development/python/casino-app-automation/tests/unit/test_aspnet_client.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_aspnet_client.py`:

```python
# --- Pandamaster quirk: missing __VIEWSTATEGENERATOR ---

_PANDAMASTER_LIST_HTML = """
<form id="form1">
  <input type="hidden" name="__VIEWSTATE" value="PVS" />
  <input type="hidden" name="__SCROLLPOSITIONX" value="0" />
  <input type="hidden" name="__SCROLLPOSITIONY" value="0" />
  <div class="nav">Balance:42</div>
  <table>
    <tr><td><a onclick="updateSelect( '11,22')">Update</a></td></tr>
  </table>
</form>
"""


@respx.mock
async def test_search_account_omits_viewstategenerator_when_absent():
    """Pandamaster's AccountsList GET has no __VIEWSTATEGENERATOR. The search POST must
    send exactly the hidden fields that were present — i.e. NOT include VSG with empty value."""
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_PANDAMASTER_LIST_HTML)
    )
    route = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_PANDAMASTER_LIST_HTML)
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        pairs = await c.search_account("Saud_Doe892")
    assert pairs == [("11", "22")]
    body = route.calls.last.request.content.decode()
    assert "__EVENTTARGET=ctl16" in body
    assert "__VIEWSTATE=PVS" in body
    # Critical: VSG must NOT appear in the body when absent from the GET
    assert "__VIEWSTATEGENERATOR" not in body


@respx.mock
async def test_milkyway_read_balance_omits_viewstategenerator_when_absent():
    """Same Pandamaster quirk for the milkyway-style balance read."""
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    # GET returns the AccountsList page WITHOUT __VIEWSTATEGENERATOR
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_PANDAMASTER_LIST_HTML)
    )
    # Search POST returns a milkyway-style results row with Balance column
    search_result_html = """
    <table>
      <tr>
        <td><a onclick="updateSelect( '11,22')">U</a></td>
        <td>22</td>
        <td>Saud_Doe892</td>
        <td>Saud</td>
        <td>7.50</td>
        <td>2026-05-30</td>
        <td>2026-06-01</td>
        <td>TestPM159</td>
        <td>Active</td>
      </tr>
    </table>
    """
    route = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=search_result_html)
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        credit_str = await c.milkyway_read_balance(query="Saud_Doe892")
    assert credit_str == "7.50"
    body = route.calls.last.request.content.decode()
    assert "__VIEWSTATEGENERATOR" not in body
    assert "txtSearch=Saud_Doe892" in body


@respx.mock
async def test_submit_dialog_omits_viewstategenerator_when_absent():
    """Dialog pages normally render VSG, but verify the conditional skipping works there too."""
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    # Dialog HTML missing VSG (hypothetical — defensive guard)
    respx.get(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOK").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="VS" />
                    <input type="hidden" name="__EVENTVALIDATION" value="EV" /></form>""",
        )
    )
    route = respx.post(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOK").mock(
        return_value=httpx.Response(
            200, text='<script>showAlter("Confirmed successful","Balance:30");</script>',
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        text = await c.submit_dialog(
            dialog_url="Module/AccountManager/GrantTreasure.aspx?param=TOK",
            extra_fields={"txtAddGold": "1"},
        )
    assert "Confirmed successful" in text
    body = route.calls.last.request.content.decode()
    assert "__VIEWSTATEGENERATOR" not in body
    assert "__VIEWSTATE=VS" in body
    assert "__EVENTVALIDATION=EV" in body
```

- [ ] **Step 2: Run; expect failures**

Run: `.venv/bin/pytest tests/unit/test_aspnet_client.py -v -k "viewstategenerator"`
Expected: 3 failures.

- [ ] **Step 3: Fix `search_account` (`client.py:163-185`)**

Replace the `search_account` method body (lines 163-185 currently) with:

```python
    async def search_account(self, query: str) -> list[tuple[str, str]]:
        """Run the ctl16 search and return all (uid, gid) pairs from the result HTML.

        `query` matches against both GameID and Account (server-side `LIKE 'x%'` per §4.8 SQL).
        Pandamaster omits __VIEWSTATEGENERATOR; we skip the field rather than echoing an empty
        value (findings §3: "send exactly the hidden fields the GET actually contained").
        """
        # First GET the page to scrape viewstate (AccountsList has no __EVENTVALIDATION).
        html = await self.fetch_accounts_list_html()
        vs = parse_viewstate(html)
        form: dict[str, str] = {
            "__EVENTTARGET": "ctl16",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": vs.viewstate,
            "__SCROLLPOSITIONX": "0",
            "__SCROLLPOSITIONY": "0",
            "txtSearch": query,
            "ShowHideAccount": "1",
        }
        if vs.viewstate_generator is not None:
            form["__VIEWSTATEGENERATOR"] = vs.viewstate_generator
        body = await self.request_text(
            "POST", "/Module/AccountManager/AccountsList.aspx", form=form,
        )
        return parse_update_select(body)
```

- [ ] **Step 4: Fix `submit_dialog` (`client.py:195-215`)**

Replace the `submit_dialog` method body with:

```python
    async def submit_dialog(
        self, *, dialog_url: str, extra_fields: dict[str, str],
    ) -> str:
        """GET the dialog page (scraping viewstate + __EVENTVALIDATION), then POST the action.

        `extra_fields` is the op-specific payload (txtAddGold/txtReason for money ops,
        txtConfirmPass/txtSureConfirmPass for reset). Returns the POST response text.
        """
        path = dialog_url if dialog_url.startswith("/") else "/" + dialog_url
        get_body = await self.request_text("GET", path)
        vs = parse_viewstate(get_body)
        form: dict[str, str] = {
            "__EVENTTARGET": "Button1",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": vs.viewstate,
        }
        if vs.viewstate_generator is not None:
            form["__VIEWSTATEGENERATOR"] = vs.viewstate_generator
        if vs.event_validation is not None:
            form["__EVENTVALIDATION"] = vs.event_validation
        form.update(extra_fields)
        return await self.request_text("POST", path, form=form)
```

- [ ] **Step 5: Fix `milkyway_read_balance` (`client.py:230-251`)**

Replace the method body with:

```python
    async def milkyway_read_balance(self, *, query: str) -> str:
        """MilkyWay-family (MilkyWay/Firekirin/Pandamaster): search and parse the Balance column
        from the matching row. Pandamaster omits __VIEWSTATEGENERATOR — we skip it when absent.

        `query` is the account name or the GameID — either matches the LIKE clause; for cached
        callers GameID is preferred (more selective).
        """
        html = await self.fetch_accounts_list_html()
        vs = parse_viewstate(html)
        form: dict[str, str] = {
            "__EVENTTARGET": "ctl16",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": vs.viewstate,
            "__SCROLLPOSITIONX": "0",
            "__SCROLLPOSITIONY": "0",
            "txtSearch": query,
            "ShowHideAccount": "1",
        }
        if vs.viewstate_generator is not None:
            form["__VIEWSTATEGENERATOR"] = vs.viewstate_generator
        body = await self.request_text(
            "POST", "/Module/AccountManager/AccountsList.aspx", form=form,
        )
        return parse_milkyway_balance_row(body, account=query)
```

- [ ] **Step 6: Fix `create_account` in orionstars/backend.py (lines 110-120)**

Replace the `form = {...}` block in `OrionStarsBackend.create_account` (after the `vs = parse_viewstate(get_body)` line) with:

```python
        form: dict[str, str] = {
            "__EVENTTARGET": "ctl07",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": vs.viewstate,
            "__EVENTVALIDATION": vs.event_validation or "",
            "txtAccount": username,
            "txtNickName": username,
            "txtLogonPass": pwd,
            "txtLogonPass2": pwd,
        }
        if vs.viewstate_generator is not None:
            form["__VIEWSTATEGENERATOR"] = vs.viewstate_generator
```

(Note: `CreateAccount.aspx` does render VSG in normal operation; the conditional is defensive consistency with the rest of the codebase. Since `OrionStarsBackend` is only resolved for the `"orionstars"` driver — Pandamaster/Firekirin go through `MilkyWayBackend` — this site shouldn't ever hit the None branch in production, but mypy demands consistent typing now that `vs.viewstate_generator` is `str | None`.)

- [ ] **Step 7: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_aspnet_client.py tests/unit/test_orionstars_backend.py tests/unit/test_milkyway_backend.py -v`
Expected: all pass (3 new tests + all previously-passing tests).

- [ ] **Step 8: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green. Test count: 369 → 374 (+5 from Tasks 1 and 2 combined).

- [ ] **Step 9: Commit (covers Tasks 1+2 together for bisectability)**

```bash
git add app/backends/_aspnet_cashier/parsers.py \
        app/backends/_aspnet_cashier/client.py \
        app/backends/orionstars/backend.py \
        tests/unit/test_aspnet_parsers.py \
        tests/unit/test_aspnet_client.py
git commit -m "fix(aspnet): tolerate missing __VIEWSTATEGENERATOR (Pandamaster)

Pandamaster (the 4th 3.0.303 portal) omits __VIEWSTATEGENERATOR on
default.aspx and AccountsList.aspx; per findings doc §3 we must \"send
exactly the hidden fields the GET actually contained\". ViewState
dataclass + parse_viewstate make the field optional, and the 4 POST-body
builders (search_account, submit_dialog, milkyway_read_balance,
OrionStarsBackend.create_account) skip it when absent — same pattern
already used for __EVENTVALIDATION.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Registry wiring — `firekirin` + `pandamaster` aliases

**Files:**
- Modify: `/Applications/development/python/casino-app-automation/app/backends/registry.py`
- Modify: `/Applications/development/python/casino-app-automation/tests/unit/test_registry.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_registry.py`:

```python
def test_firekirin_and_pandamaster_in_non_idempotent_drivers():
    from app.backends.registry import NON_IDEMPOTENT_DRIVERS
    assert "firekirin" in NON_IDEMPOTENT_DRIVERS
    assert "pandamaster" in NON_IDEMPOTENT_DRIVERS


async def test_resolve_firekirin_returns_milkyway_backend_with_firekirin_prefix(fake_redis):
    """Firekirin is a registry alias of MilkyWay: same class, different driver_prefix."""
    import httpx
    from app.backends.context import GameCredentials
    from app.backends.milkyway.backend import MilkyWayBackend
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=200, name="FK",
        backend_url="https://firekirin.xyz:8888", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="firekirin",
    )
    settings = Settings(anticaptcha_api_key="testkey")
    async with httpx.AsyncClient() as http:
        b = resolve_backend(
            "firekirin", credentials=creds, http_client=http,
            settings=settings, redis=fake_redis,
        )
    assert isinstance(b, MilkyWayBackend)
    assert b._client._driver == "firekirin"


async def test_resolve_pandamaster_returns_milkyway_backend_with_pandamaster_prefix(fake_redis):
    """Pandamaster is a registry alias of MilkyWay (runs on default 443, no port)."""
    import httpx
    from app.backends.context import GameCredentials
    from app.backends.milkyway.backend import MilkyWayBackend
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=201, name="PM",
        backend_url="https://pandamaster.vip", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="pandamaster",
    )
    settings = Settings(anticaptcha_api_key="testkey")
    async with httpx.AsyncClient() as http:
        b = resolve_backend(
            "pandamaster", credentials=creds, http_client=http,
            settings=settings, redis=fake_redis,
        )
    assert isinstance(b, MilkyWayBackend)
    assert b._client._driver == "pandamaster"


async def test_resolve_firekirin_requires_credentials(fake_redis):
    import httpx
    import pytest
    from app.backends.base import BackendError
    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=200, name="FK",
        backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="firekirin",
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="missing_firekirin_credentials"):
            resolve_backend(
                "firekirin", credentials=creds, http_client=http,
                settings=Settings(anticaptcha_api_key="testkey"), redis=fake_redis,
            )
```

- [ ] **Step 2: Run; expect 4 failures**

Run: `.venv/bin/pytest tests/unit/test_registry.py -v -k "firekirin or pandamaster"`
Expected: 4 failures.

- [ ] **Step 3: Update registry**

Edit `app/backends/registry.py`. Locate the existing block `if key in {"orionstars", "milkyway"}:` (around line 101) — refactor it to use named frozensets and extend `_MILKYWAY_FAMILY_DRIVERS` to include the two new aliases.

Step 3a — Add two named frozensets near the top of the file, after the existing `_VPOWER_PROVIDER_DRIVERS` line (currently line 27):

```python
# Drivers that share the OrionStars cashier wire (ASP.NET 3.0.303 family). The OrionStars
# variant reads player balance via the `getscoreuserid` postback; the MilkyWay variant reads
# it from the search-results row's `Balance` column. Per findings doc §4.1 and the family
# table at the top, the only behavioural divergence within this family is the balance-read
# style. Pandamaster also omits __VIEWSTATEGENERATOR — handled in the shared client.
_ORIONSTARS_FAMILY_DRIVERS = frozenset({"orionstars"})
_MILKYWAY_FAMILY_DRIVERS = frozenset({"milkyway", "firekirin", "pandamaster"})
_ASPNET_CASHIER_DRIVERS = _ORIONSTARS_FAMILY_DRIVERS | _MILKYWAY_FAMILY_DRIVERS
```

Step 3b — Extend `NON_IDEMPOTENT_DRIVERS` (currently includes orionstars, milkyway, ultrapanda, vblink, gameroom, goldentreasure):

```python
NON_IDEMPOTENT_DRIVERS: frozenset[str] = frozenset({
    "gameroom", "goldentreasure",
    "orionstars", "milkyway", "firekirin", "pandamaster",
    "ultrapanda", "vblink",
})
```

Step 3c — Replace the existing block `if key in {"orionstars", "milkyway"}:` with the frozenset reference + the family-based class selection. The block currently ends with `return OrionStarsBackend(client) if key == "orionstars" else MilkyWayBackend(client)`. Change it to:

```python
    if key in _ASPNET_CASHIER_DRIVERS:
        if not (credentials.backend_url and credentials.backend_username and credentials.backend_password):
            raise BackendError(f"missing_{key}_credentials")
        if redis is None:
            raise BackendError("missing_redis_client")
        if not settings.anticaptcha_api_key:
            raise BackendError("missing_anticaptcha_api_key")
        client = AspnetCashierClient(
            base_url=credentials.backend_url,
            username=credentials.backend_username,
            password=credentials.backend_password,
            http_client=http_client,
            session_store=CookieSessionStore(redis),
            captcha_solver=AntiCaptchaSolver(api_key=settings.anticaptcha_api_key),
            game_id=credentials.game_id,
            session_ttl_seconds=settings.aspnet_session_ttl_seconds,
            lock_ttl_seconds=settings.aspnet_lock_ttl_seconds,
            lock_acquire_timeout_seconds=settings.aspnet_lock_acquire_timeout_seconds,
            captcha_login_max_attempts=settings.captcha_login_max_attempts,
            driver_prefix=key,
        )
        if key in _ORIONSTARS_FAMILY_DRIVERS:
            return OrionStarsBackend(client)
        return MilkyWayBackend(client)
```

(The full block content is the same as today — only the outer condition and the class-selection step changed. Re-use the exact existing client-construction lines.)

Step 3d — Update the `resolve_backend` docstring to mention the two new aliases. Find the docstring at the top of `resolve_backend` and add a brief line about the MilkyWay-family aliases (firekirin, pandamaster).

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_registry.py -v`
Expected: 4 new tests pass; all previously-passing tests still pass (including the existing orionstars + milkyway resolution tests).

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green. Test count 374 → 378.

- [ ] **Step 6: Commit**

```bash
git add app/backends/registry.py tests/unit/test_registry.py
git commit -m "feat(registry): wire firekirin + pandamaster as MilkyWay aliases

Both are branded hosts of the same 3.0.303 ASP.NET cashier build (per
findings doc §top family table) with the search-row Balance read style.
Pandamaster runs on default 443 (no :8781), Firekirin on :8888. Hosts are
driven by credentials.backend_url; no code changes beyond the registry
alias frozenset and NON_IDEMPOTENT_DRIVERS update.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Live-gated integration tests

Two new files mirroring `tests/integration/test_milkyway_integration.py`.

**Files:**
- Create: `/Applications/development/python/casino-app-automation/tests/integration/test_firekirin_integration.py`
- Create: `/Applications/development/python/casino-app-automation/tests/integration/test_pandamaster_integration.py`

- [ ] **Step 1: Create Firekirin live test**

Create `tests/integration/test_firekirin_integration.py`:

```python
"""Live-gated end-to-end test against the real Firekirin portal.

Skipped unless all of these are set:
  ANTICAPTCHA_API_KEY
  FIREKIRIN_TEST_BASE_URL    e.g. https://firekirin.xyz:8888
  FIREKIRIN_TEST_AGENT_USER  e.g. TestFK159
  FIREKIRIN_TEST_AGENT_PASS
  FIREKIRIN_TEST_PLAYER      must already exist under the agent

Firekirin is a registry alias of MilkyWay (per findings doc §top family table —
same 3.0.303 build, search-row Balance variant). This test confirms the alias
wiring works end-to-end against the real host.
"""
import os

import httpx
import pytest
import pytest_asyncio

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import InMemoryCookieSessionStore
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.milkyway.backend import MilkyWayBackend
from app.captcha.anticaptcha import AntiCaptchaSolver

_required = [
    "ANTICAPTCHA_API_KEY", "FIREKIRIN_TEST_BASE_URL",
    "FIREKIRIN_TEST_AGENT_USER", "FIREKIRIN_TEST_AGENT_PASS",
    "FIREKIRIN_TEST_PLAYER",
]

pytestmark = pytest.mark.skipif(
    not all(os.getenv(k) for k in _required),
    reason=f"set {', '.join(_required)} to run",
)


@pytest_asyncio.fixture
async def backend():
    base = os.environ["FIREKIRIN_TEST_BASE_URL"]
    user = os.environ["FIREKIRIN_TEST_AGENT_USER"]
    pwd = os.environ["FIREKIRIN_TEST_AGENT_PASS"]
    async with httpx.AsyncClient(timeout=60.0) as http:
        client = AspnetCashierClient(
            base_url=base, username=user, password=pwd,
            http_client=http,
            session_store=InMemoryCookieSessionStore(),
            captcha_solver=AntiCaptchaSolver(api_key=os.environ["ANTICAPTCHA_API_KEY"]),
            game_id=9997, session_ttl_seconds=1800,
            lock_ttl_seconds=20, lock_acquire_timeout_seconds=30.0,
            captcha_login_max_attempts=3, driver_prefix="firekirin",
        )
        yield MilkyWayBackend(client)


def _ctx(*, account=None, username=None) -> BackendContext:
    creds = GameCredentials(
        game_id=9997, name="FK Live",
        backend_url=os.environ["FIREKIRIN_TEST_BASE_URL"],
        login_page_url=None,
        backend_username=os.environ["FIREKIRIN_TEST_AGENT_USER"],
        backend_password=os.environ["FIREKIRIN_TEST_AGENT_PASS"],
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="firekirin",
    )
    return BackendContext(
        credentials=creds, user_id=1, account=account,
        idempotency_key="live-test", account_username=username,
    )


async def test_live_agent_balance(backend):
    result = await backend.agent_balance(_ctx())
    assert result.agent_balance_cents >= 0


async def test_live_read_balance_for_existing_player(backend):
    player = os.environ["FIREKIRIN_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9997,
        username=player, external_user_id=None,
    )
    result = await backend.read_balance(_ctx(account=account))
    assert result.balance_cents >= 0


async def test_live_recharge_one_dollar_then_redeem_one_dollar(backend):
    player = os.environ["FIREKIRIN_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9997,
        username=player, external_user_id=None,
    )
    ctx = _ctx(account=account)
    before = await backend.read_balance(ctx)
    await backend.recharge(ctx, amount_cents=100, bonus_cents=0, total_credit_cents=100)
    after_recharge = await backend.read_balance(ctx)
    assert after_recharge.balance_cents == before.balance_cents + 100
    await backend.redeem(ctx, amount_cents=100)
    after_redeem = await backend.read_balance(ctx)
    assert after_redeem.balance_cents == before.balance_cents
```

- [ ] **Step 2: Create Pandamaster live test**

Create `tests/integration/test_pandamaster_integration.py` (same shape, swap env var prefix and `driver_prefix`, plus a docstring note about Pandamaster's no-port + missing-VSG quirks):

```python
"""Live-gated end-to-end test against the real Pandamaster portal.

Skipped unless all of these are set:
  ANTICAPTCHA_API_KEY
  PANDAMASTER_TEST_BASE_URL    e.g. https://pandamaster.vip   (NOTE: no :8781 — default 443)
  PANDAMASTER_TEST_AGENT_USER  e.g. TestPM159
  PANDAMASTER_TEST_AGENT_PASS
  PANDAMASTER_TEST_PLAYER

Pandamaster is a registry alias of MilkyWay (per findings doc §top family table). Quirks:
  - Runs on default 443 (no explicit port in backend_url).
  - Omits __VIEWSTATEGENERATOR on default.aspx and AccountsList.aspx — tolerated by the
    shared _aspnet_cashier client + parser (Phase 7 fix).
"""
import os

import httpx
import pytest
import pytest_asyncio

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import InMemoryCookieSessionStore
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.milkyway.backend import MilkyWayBackend
from app.captcha.anticaptcha import AntiCaptchaSolver

_required = [
    "ANTICAPTCHA_API_KEY", "PANDAMASTER_TEST_BASE_URL",
    "PANDAMASTER_TEST_AGENT_USER", "PANDAMASTER_TEST_AGENT_PASS",
    "PANDAMASTER_TEST_PLAYER",
]

pytestmark = pytest.mark.skipif(
    not all(os.getenv(k) for k in _required),
    reason=f"set {', '.join(_required)} to run",
)


@pytest_asyncio.fixture
async def backend():
    base = os.environ["PANDAMASTER_TEST_BASE_URL"]
    user = os.environ["PANDAMASTER_TEST_AGENT_USER"]
    pwd = os.environ["PANDAMASTER_TEST_AGENT_PASS"]
    async with httpx.AsyncClient(timeout=60.0) as http:
        client = AspnetCashierClient(
            base_url=base, username=user, password=pwd,
            http_client=http,
            session_store=InMemoryCookieSessionStore(),
            captcha_solver=AntiCaptchaSolver(api_key=os.environ["ANTICAPTCHA_API_KEY"]),
            game_id=9996, session_ttl_seconds=1800,
            lock_ttl_seconds=20, lock_acquire_timeout_seconds=30.0,
            captcha_login_max_attempts=3, driver_prefix="pandamaster",
        )
        yield MilkyWayBackend(client)


def _ctx(*, account=None, username=None) -> BackendContext:
    creds = GameCredentials(
        game_id=9996, name="PM Live",
        backend_url=os.environ["PANDAMASTER_TEST_BASE_URL"],
        login_page_url=None,
        backend_username=os.environ["PANDAMASTER_TEST_AGENT_USER"],
        backend_password=os.environ["PANDAMASTER_TEST_AGENT_PASS"],
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="pandamaster",
    )
    return BackendContext(
        credentials=creds, user_id=1, account=account,
        idempotency_key="live-test", account_username=username,
    )


async def test_live_agent_balance(backend):
    result = await backend.agent_balance(_ctx())
    assert result.agent_balance_cents >= 0


async def test_live_read_balance_for_existing_player(backend):
    player = os.environ["PANDAMASTER_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9996,
        username=player, external_user_id=None,
    )
    result = await backend.read_balance(_ctx(account=account))
    assert result.balance_cents >= 0


async def test_live_recharge_one_dollar_then_redeem_one_dollar(backend):
    player = os.environ["PANDAMASTER_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9996,
        username=player, external_user_id=None,
    )
    ctx = _ctx(account=account)
    before = await backend.read_balance(ctx)
    await backend.recharge(ctx, amount_cents=100, bonus_cents=0, total_credit_cents=100)
    after_recharge = await backend.read_balance(ctx)
    assert after_recharge.balance_cents == before.balance_cents + 100
    await backend.redeem(ctx, amount_cents=100)
    after_redeem = await backend.read_balance(ctx)
    assert after_redeem.balance_cents == before.balance_cents
```

- [ ] **Step 3: Confirm both files skip without env vars**

Run: `.venv/bin/pytest tests/integration/test_firekirin_integration.py tests/integration/test_pandamaster_integration.py -v`
Expected: 6 skipped (3 + 3), 0 failed.

- [ ] **Step 4: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green; 378 passed + 20 skipped (14 from prior phases + 6 from this phase).

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_firekirin_integration.py tests/integration/test_pandamaster_integration.py
git commit -m "test(phase7): live-gated integration scaffolding for firekirin + pandamaster

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Final task: Manual verification and merge

- [ ] **Step 1: Verify the suite is fully green**

Run: `make lint && make type && make test`
Expected: 378 passing, 20 skipped (14 from prior phases + 6 from this phase).

- [ ] **Step 2: Push the branch**

Run: `git push -u origin feat/phase7-firekirin-pandamaster`

- [ ] **Step 3: Hand off to the user**

Tell the user:
> Phase 7 is implemented on `feat/phase7-firekirin-pandamaster`. Suite: 378 passing + 20 skipped. Pandamaster's missing `__VIEWSTATEGENERATOR` is tolerated in the shared parser/client. Both drivers added to NON_IDEMPOTENT_DRIVERS. Ready for manual verification — set `FIREKIRIN_TEST_*` and `PANDAMASTER_TEST_*` env vars to exercise the real portals, or trigger ops from Laravel.

Do **not** merge to main without explicit go-ahead.

---

## Self-review checklist

- [ ] **Spec coverage:** Spec was skipped per user direction — verify against the brainstorm summary I gave the user before this plan:
  - Registry change: introduce family frozensets, alias `firekirin` + `pandamaster` to `MilkyWayBackend` → **Task 3** ✓
  - `NON_IDEMPOTENT_DRIVERS` update → **Task 3** ✓
  - Parser tolerance for missing `__VIEWSTATEGENERATOR` → **Task 1** ✓
  - Client tolerance (3 sites in `client.py` + 1 in `orionstars/backend.py`) → **Task 2** ✓
  - Live integration tests (2 files) → **Task 4** ✓
  - No new module, config knobs, password generator, error mapping, or logging redactions → confirmed by absence ✓
- [ ] **No placeholders.** Every step has concrete code or commands.
- [ ] **Type consistency:** `ViewState.viewstate_generator` becomes `str | None` (Task 1); all 4 call sites (Task 2) skip the field when None to keep `dict[str, str]` form bodies valid.
- [ ] **Driver-prefix propagation works:** Firekirin and Pandamaster tests assert `b._client._driver == "<driver>"` (Task 3).
- [ ] **Pandamaster URL has no port:** Firekirin URL has `:8888`. Tests use these as fixtures (Task 3).
