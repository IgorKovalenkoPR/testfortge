# Identity model spike — input for Sprint 4.1 (Roles)

> ⚠️ **SUPERSEDED (2026-08-04)** by
> [team_platform_architecture.md](team_platform_architecture.md).
> Two conclusions of this spike no longer hold under the owner's
> multi-team requirements:
> * §2 recommended **magic link only** — the new requirement asks for
>   **email + password AND Google sign-in**. Magic link drops out of v1.
> * §8 declared **per-org isolation a non-goal** — org-level tenancy is
>   now in scope (requirements 2 and 3).
>
> What survives and is still worth reading: the `user` / `user_session`
> table core (§3), the `owner_sid` → user migration story (§4), the
> last-admin guard, and the test surface in §7. Do not implement the
> magic-link flow from §5 without re-approval.

**Status:** design draft, not implemented. Approve before starting S4.1
coding (≈22 h). Sprint 4 plan
([sprint_4_polish.md](sprint_4_polish.md#L4-L13)) flags this as a
12-month decision that touches audit, SSO, GDPR, and billing —
worth a day of design instead of a week of rework.

**Authored:** 2026-05-20.

---

## 1. Why this needs a decision before code

Sprint 4.1 introduces three roles (admin / tester / viewer) on top of
projects. Roles need a stable *identifier per human* — not per browser
cookie — so:

- Invites survive cookie loss.
- Audit-trail entries name the same person across sessions.
- Removing access is one delete, not "find every cookie."
- An SSO bolt-on later does not require schema surgery.

Today the codebase only knows ``project.owner_sid`` (a Flask-session
id). That's enough for the single-tenant zero-friction UX, but it
cannot answer "who exactly closed BUG-127" once the cookie is gone.

The bulk-bug-ops audit trail shipped in S4.2 already records
``actor = get_session_id(...)[:8]`` ([routes/execution.py:bugs_bulk](../../routes/execution.py)).
That field is a one-line swap to ``actor = current_user().email`` once
this spike lands — so the design risk of *what we already shipped*
is bounded. The risk we are pricing is the **next ~30 h** of code
(roles, members page, magic link, SSO hooks) being thrown away if the
identifier model flips after implementation.

---

## 2. The two real options

### Option A — Email + magic link (recommended)

A new ``user`` table keyed by ``email``. Browser sessions ("sids") are
*bound* to a ``user`` via a one-time magic-link click. Anonymous sids
keep working as today; on first claim, every project ``project_member``
row tagged with that sid gets rewritten to point at the new user id.

Schema sketch is in the original Sprint 4 plan
([sprint_4_polish.md:200-238](sprint_4_polish.md)).
This spike re-validates it below.

**Pros:**
- Email is the human's natural identifier — same one they get the
  invite at, same one SSO emits as the upn / preferred_username claim.
- Magic-link flow is the same well-trodden code path Notion, Linear,
  Vercel use — no password storage, no password-reset UX.
- Existing zero-friction UX is preserved: an anonymous session keeps
  working without a sign-in until the operator wants to share.
- An SSO bolt-on (OIDC / SAML) maps cleanly: the IdP emits an email,
  we look it up in ``user``, no schema change.
- Cookie loss = magic-link request, not "lost work."

**Cons:**
- SMTP plumbing (or a copy-link fallback) — one more piece of infra.
- Edge case: multiple sids claim the same email at different times
  (shared inbox, password-manager confusion). The first claim wins;
  later claims either merge or are rejected. **Open question — see §6.**

### Option B — Keep ``session_id`` as the durable identifier

The current `owner_sid` becomes the canonical identifier. Invites are
opaque hex tokens the inviter copies into a chat. The invitee pastes
into a "Claim invite" form.

**Pros:**
- No SMTP. Ship today.
- One fewer table to maintain.
- Matches the on-prem / closed-team threat model the README pitches.

**Cons:**
- Opaque hex is hostile UX. Operators *will* email it anyway,
  defeating the security argument.
- Cookie loss = audit trail orphaned. Every "who did X" lookup needs
  manual sid-to-human reconciliation.
- An SSO bolt-on later means a *second* identifier — every row will
  carry both ``user_id`` and ``sid``, and someone has to write the
  back-fill.
- GDPR DSAR ("show me everything tied to user@x.com") needs an
  out-of-band mapping table the admin maintains by hand.

**Net:** Option B is cheaper *this sprint*. Option A is cheaper across
the next 12 months.

### Recommendation

**Option A.** The SMTP-plumbing concern is real but bounded: dev
falls back to logging the magic link, prod gates the feature on
``SMTP_HOST``, and the admin has an "always-visible one-time URL"
button on the members page as the manual-share fallback if SMTP fails
or isn't configured.

---

## 3. Schema (locked-in if Option A is approved)

```sql
CREATE TABLE "user" (
  id            VARCHAR(32) PRIMARY KEY,           -- uuid hex
  email         VARCHAR(255) UNIQUE NOT NULL,
  display_name  VARCHAR(120),
  created_at    TIMESTAMP WITH TIME ZONE NOT NULL,
  last_login_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE user_session (                         -- sid → user binding
  sid       VARCHAR(80) PRIMARY KEY,
  user_id   VARCHAR(32) NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  bound_at  TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE TABLE magic_link (
  token       VARCHAR(64) PRIMARY KEY,
  email       VARCHAR(255) NOT NULL,
  sid         VARCHAR(80)  NOT NULL,                -- which browser asked
  expires_at  TIMESTAMP WITH TIME ZONE NOT NULL,    -- 15 min default
  used_at     TIMESTAMP WITH TIME ZONE
);
CREATE INDEX magic_link_email_idx ON magic_link(email);

CREATE TABLE project_member (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id    VARCHAR(32) NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  user_id       VARCHAR(32) REFERENCES "user"(id) ON DELETE CASCADE,
  invited_email VARCHAR(255),                       -- set until claim lands
  sid           VARCHAR(80),                        -- legacy anonymous owner
  role          VARCHAR(20) NOT NULL,               -- admin|tester|viewer
  added_at      TIMESTAMP WITH TIME ZONE NOT NULL,
  added_by_user_id VARCHAR(32),
  UNIQUE (project_id, user_id),
  UNIQUE (project_id, invited_email)
);
```

Notes on the schema:

- ``user.email`` is the natural unique key. The hex ``id`` exists only
  so foreign keys are short and email changes don't cascade. Email
  changes are out of scope for the spike; we'll handle them by hand
  if they happen in the next 6 months.
- ``user_session`` is a one-to-many (one user, many sids — phone +
  laptop). ``ON DELETE CASCADE`` so deleting a user invalidates every
  bound session.
- ``project_member`` carries **three** alternative principal columns
  on purpose:
  * ``user_id`` — the post-claim canonical row.
  * ``invited_email`` — pre-claim placeholder; gets nulled and
    ``user_id`` populated on first sign-in by that email.
  * ``sid`` — legacy migration target; covers every project that
    currently has ``project.owner_sid`` set but no claimed user yet.
- The two ``UNIQUE`` constraints prevent "invite the same email
  twice" and "promote the same user to two roles." They allow
  multiple ``sid``-only rows (legacy / unclaimed).

---

## 4. Migration story

On ``init_db()``:

```sql
-- For every legacy project with a non-null owner_sid, seed a
-- project_member row at admin level.
INSERT INTO project_member (project_id, sid, role, added_at, added_by_user_id)
SELECT id, owner_sid, 'admin', created_at, NULL
FROM project
WHERE owner_sid IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM project_member m
    WHERE m.project_id = project.id AND m.sid = project.owner_sid
  );
```

On every magic-link claim by ``sid = S`` and ``email = E``:

```sql
-- 1. Upsert the user row.
-- 2. Bind this sid to it (user_session).
-- 3. Rewrite every project_member(sid=S) into
--    project_member(user_id=user.id), preserving role.
-- 4. Promote any project_member(invited_email=E) into
--    project_member(user_id=user.id, invited_email=NULL).
```

Step 3 is the "carry your work forward when you finally claim an
identity" path. Step 4 is the "I was invited last week, now I claim"
path. Both can land in one transaction.

---

## 5. UX touch points

| Surface | Anonymous today | After 4.1 (Option A) |
|---|---|---|
| First visit | New sid + auto-project. | Same. |
| ``/projects/<id>/members`` | (does not exist) | Admin sees members + invite form. |
| Invite | (out of band) | Admin types email + picks role → ``project_member(invited_email, role)`` row created. If SMTP configured: send link. Always: show one-time URL the admin can copy/paste. |
| Sign-in | (n/a) | Email form → magic link → click → user_session bound, sid carries projects across. |
| Sign-out | (n/a) | Drop ``user_session``; sid keeps working as anonymous. |
| Audit field | ``sid[:8]`` | ``user.email`` if claimed, else ``sid[:8]`` (legacy rows stay legible). |
| ``/healthz`` and ``/metrics`` | open | unchanged (S4.5 gate still applies). |

---

## 6. Open questions that BLOCK 4.1

These need an answer before 4.1 coding starts. Each is a single yes/no
or a forced choice — should not need more than one stand-up.

1. **SMTP infra.** Do we run our own (sendgrid / ses / postmark)? If
   the answer is "later," confirm the copy-link fallback is acceptable
   for the first 4.1 release. Recommendation: copy-link fallback is
   enough for shipping 4.1; SMTP can land in S6.
2. **Anon-user retention.** When a user with claimed identity removes
   their last session_id and never signs in again — do their projects
   stay forever, or expire after N days? Recommendation: keep forever,
   match the on-prem pitch. Revisit if storage becomes an issue.
3. **Email claim conflict.** Two different sids claim the same email
   (one is a stale session from a teammate, one is the real owner).
   Options: (a) first-wins, later-rejected with "this email is
   already bound to a session — sign out there first";
   (b) auto-merge (last claim wins, prior session_user_session row
   replaced). Recommendation: **(a) first-wins** — auto-merge is the
   path to "did someone steal my projects?" support tickets.
4. **Role hierarchy enforcement.** ``ROLE_RANK = {viewer:1, tester:2,
   admin:3}`` per the original plan — confirm no fourth role
   ("billing", "support") is on the 6-month roadmap. If yes, the
   ``role`` column should probably become a foreign key into a
   role-definition table instead of a literal string.
5. **Last-admin guard wording.** "You cannot remove the last admin
   from a project" — is that a hard 400 with a flash, or a
   confirmation-required UX? Recommendation: hard 400 + flash
   ("Promote another member to admin first"). Cheap, clear.

If any of these stays unresolved at the start of 4.1, the spike has
failed its job — code shouldn't begin until each has an owner-signed
answer.

---

## 7. Test surface (what S4.1 must add)

These are the regression tests S4.1's PR must include, derived from
the schema above. Sized so each is unambiguously pass/fail in CI:

- Migration: existing project with ``owner_sid='S'`` produces exactly
  one ``project_member(sid='S', role='admin')`` row on ``init_db()``.
- Magic-link claim: ``sid=S`` + ``email=E`` rewrites every
  ``project_member(sid=S)`` into ``project_member(user_id=u)`` in one
  transaction; legacy sid column nulled.
- Invite + claim: ``project_member(invited_email='x@y')`` is promoted
  to ``user_id`` on the first claim, ``invited_email`` nulled.
- Last-admin guard: removing the only admin returns 400 +
  ``"last admin"`` in the flash. Demoting the only admin to viewer
  returns 400 too.
- Role-gate matrix: each route in the Sprint-4 plan's per-route
  policy table ([sprint_4_polish.md:272-280](sprint_4_polish.md))
  returns 403 for under-privileged roles and 2xx for the minimum.
- Bulk-bug audit (S4.2 follow-up): once roles land, ``actor`` field
  in the bug ``comment`` audit line is the user's email, not the
  sid prefix. Backfill is **not** required — legacy rows keep their
  sid[:8] string.

---

## 8. Out of scope (explicit non-goals)

- **Email change UX.** If a user wants to change their email,
  current answer is "admin edits the DB row by hand." Revisit in S7+.
- **2FA / TOTP.** Magic link is the second factor (email account).
- **Per-org isolation.** A user can be in many projects across many
  customers; the threat model is single-tenant.
- **Audit-trail upgrade to a real table.** S4.2's "append to comment"
  approach stays. Promote to a real ``audit_log`` table only when an
  operator asks for filterable audit views (S5+).

---

## 9. Decision-meeting agenda (≤30 min)

1. Confirm Option A or Option B (2 min).
2. Walk Open Questions 1–5 from §6, mark each ✓ before leaving (15 min).
3. Confirm migration story §4 — anyone has a project the legacy
   migration would break? (5 min)
4. Confirm out-of-scope list §8 — anyone has a hard requirement that
   would push something back in? (5 min)
5. Estimate review: still 22 h, or has anything in this spike grown
   the work? Recommendation: stays 22 h; this spike only formalises
   what was already in the Sprint 4 plan.

When all five items have decisions, S4.1 coding can begin.
