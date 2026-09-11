# Гілки, закриті 2026-09-11 — і чим саме кожна перекрита

Шість гілок видалено з `origin` за рішенням власника. Кожна відставала
від `main` на 190–365 комітів і жодна не мала PR, окрім двох названих
нижче — ті закрилися разом із видаленням head-гілки.

**Навіщо цей файл.** Видалення гілки прибирає єдиний вказівник на її
коміти: у reflog віддаленого репозиторію SHA поживуть іще трохи, а
потім ні. Щоб рішення можна було переглянути, запис має пережити самі
коміти — тому тут SHA кожної, дата, обсяг і **перевірка**, чому робота
не втрачається. Відновити будь-яку можна з її SHA, доки вона ще в
reflog: `git fetch origin <sha>` і гілка від нього.

| Гілка | SHA на момент видалення | Остання дата | Комітів | Про що |
|---|---|---|---|---|
| `claude/ci-fix-deflake` | `0c1caffc10fcc5734a90b1b89a54e2971625861b` | 2026-05-20 | 1 | stub site_crawler.crawl_site in conftest to deflake URL-input tests |
| `claude/free-tier-generation-fixes` | `03b80d91eb7407d80138c6f29ab3076fa2a6fde8` | 2026-07-30 | 3 | OOM-kill of the generation worker on the free tier |
| `claude/recorder-guide-and-team-docs` | `3e3cf00bc7df028ac122e1d9663ba72cf048cd08` | 2026-07-14 | 1 | in-app Guide for the recorder + an active-driver team doc (PR #43) |
| `claude/testforge-test-cases-model-6055d7` | `149dda4f48b59cc730d0cc3ea052301be37e1b91` | 2026-07-30 | 7 | test-case house-style model in qa_knowledge/style/*.yaml |
| `cloudflare/workers-autoconfig` | `3fe11d7807049f39e369ef06fc60757a81f0f449` | 2026-04-27 | 1 | wrangler.jsonc for Cloudflare Workers (PR #1) |
| `pr-c-guide-updates` | `e5741fce2e3e85546f4ca9377bce7a7335bd6c85` | 2026-05-28 | 1 | Guide refresh for recorder / assertion / multi-locator |

## Чим кожна перекрита — перевірено, а не припущено

**`claude/free-tier-generation-fixes`** — OOM-гвард у `main` новіший:
`4d2604c fix(live): E5.2 — the OOM guard was watching the one process that
never grows`. Тобто гілка лікувала симптом, який у `main` полагоджено в
корені: гвард дивився на власний процес, а не на дочірній, який і
виділяв пам'ять.

**`claude/testforge-test-cases-model-6055d7`** — `engine/qa_knowledge/style/`
у `main` існує і **відрізняється** від гілки: house style виміряно з
корпусу Odoo на 4 808 кейсів (PR #56). Версія в `main` новіша, а не
відсутня; злиття відкотило б її.

**`claude/recorder-guide-and-team-docs`** (PR #43) і **`pr-c-guide-updates`** —
обидві правлять `templates/guide.html` у тому вигляді, якого більше немає:
гід розібрано на оболонку + `templates/guide/_sections_{en,ua}.html` із
ґейтом структурної парності. Rebase означав би переписати правку з нуля, а
не перенести її.

**`cloudflare/workers-autoconfig`** (PR #1) — `wrangler.jsonc` від квітня,
365 комітів позаду. Продукт — Flask на Render; Cloudflare тут обслуговує
лише маркетинговий сайт, і та перевірка в CI зелена без цього файлу.

**`claude/ci-fix-deflake`** — єдина, чий зміст у `main` відсутній: стаба
`crawl_site` у `tests/conftest.py` немає. Але з травня набір зелений у
трьох версіях Python і в десятикратному e2e, тож флейку, заради якої стаб
писали, ніхто не спостерігав. Закрито як таке, що не відтворюється; якщо
повернеться — SHA вище.

## Що НЕ закрито

`claude/testfortge-qa-regression-0232e3` лишається жити. П'ять із шести її
комітів перенесено в `main`; шостий — `cb995a7 fix(csp): thirty-one inline
handlers, and four confirmations that failed open` — **не перенесений**, і
гілку не можна видаляти, доки він не зроблений заново. Подробиці в тому ж
коміті, що переносить решту.
