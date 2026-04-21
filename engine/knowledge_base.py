"""
TestFortge — Knowledge Base

Comprehensive QA knowledge base covering:
- Test design techniques (ISTQB-aligned)
- Testing types per domain/platform
- Testing tools with reasoning
- No-code automation tools
- Focus areas per domain
- Standards & certifications (ISO 25010, OWASP, WCAG, etc.)
- Edge case patterns
"""

# ═══════════════════════════════════════════════════════════════════
# 1. TEST DESIGN TECHNIQUES
# ═══════════════════════════════════════════════════════════════════

TEST_DESIGN_TECHNIQUES = {
    "equivalence_partitioning": {
        "name": "Equivalence Partitioning (EP)",
        "category": "Black-box",
        "description": "Divides input data into equivalence classes — groups of values that the system processes the same way. It is sufficient to test one value from each class.",
        "when_to_use": [
            "There are clear input data ranges (age, price, quantity)",
            "Need to reduce the number of test cases without losing coverage",
            "Form fields with validation",
        ],
        "example": "Field 'Age': valid (18-65), invalid (<18, >65, empty, letters). One test per class.",
        "istqb_level": "Foundation",
    },
    "boundary_value_analysis": {
        "name": "Boundary Value Analysis (BVA)",
        "category": "Black-box",
        "description": "Testing boundary values of ranges. Errors most often occur at boundaries: min, min+1, max-1, max.",
        "when_to_use": [
            "Numeric ranges (price 1-9999, age 0-120)",
            "String length limits (1-255 characters)",
            "Pagination, API limits",
        ],
        "example": "Field 'Item Quantity' (1-99): test 0, 1, 2, 98, 99, 100.",
        "istqb_level": "Foundation",
    },
    "decision_table": {
        "name": "Decision Table Testing",
        "category": "Black-box",
        "description": "A decision table for combinations of conditions and expected results. Each column is a separate test case.",
        "when_to_use": [
            "Business logic depends on multiple conditions (discounts, tariffs)",
            "Complex access rules (roles + status + subscription)",
            "Workflows with branching",
        ],
        "example": "Discount depends on: VIP status (yes/no) x Order amount (>1000/<=1000) x Promo code (exists/none) = 8 combinations.",
        "istqb_level": "Foundation",
    },
    "state_transition": {
        "name": "State Transition Testing",
        "category": "Black-box",
        "description": "Models system behavior as a set of states and transitions between them. Tests valid and invalid transitions.",
        "when_to_use": [
            "Workflows with statuses (order, ticket, document)",
            "Authorization (login → active → locked → reset)",
            "Cart / checkout process",
        ],
        "example": "Order: New → Paid → Shipped → Delivered. Invalid: New → Delivered (skipping payment).",
        "istqb_level": "Foundation",
    },
    "pairwise_testing": {
        "name": "Pairwise / All-Pairs Testing",
        "category": "Black-box",
        "description": "Combinatorial technique: instead of testing all parameter combinations, covers all pairs of values. Drastically reduces the number of tests.",
        "when_to_use": [
            "Many configuration parameters (OS x Browser x Language x Resolution)",
            "API with many query parameters",
            "Forms with many fields",
        ],
        "example": "3 OSes x 4 browsers x 3 languages = 36 combinations → pairwise reduces to ~12 tests.",
        "istqb_level": "Advanced",
    },
    "error_guessing": {
        "name": "Error Guessing",
        "category": "Experience-based",
        "description": "A technique based on the tester's experience. Predicting typical errors: null, empty strings, special characters, large numbers.",
        "when_to_use": [
            "Always — as a complement to formal techniques",
            "New features without detailed specification",
            "Regression testing after hotfixes",
        ],
        "example": "Email field: spaces, Cyrillic characters, SQL injection, XSS, very long string, double @.",
        "istqb_level": "Foundation",
    },
    "exploratory_testing": {
        "name": "Exploratory Testing",
        "category": "Experience-based",
        "description": "Simultaneous learning, test design, and execution. Uses charters (session-based) for structure.",
        "when_to_use": [
            "New feature without documentation",
            "Limited time for testing",
            "Complement to scripted testing",
            "Searching for edge cases not covered in test cases",
        ],
        "example": "Charter: 'Explore checkout with different payment methods for 45 min'. Record findings.",
        "istqb_level": "Foundation",
    },
    "use_case_testing": {
        "name": "Use Case Testing",
        "category": "Black-box",
        "description": "Testing based on usage scenarios: main flow (happy path) + alternative flows + error flows.",
        "when_to_use": [
            "End-to-end scenarios (registration → purchase → payment)",
            "Integration testing of business processes",
            "Acceptance testing (UAT)",
        ],
        "example": "Use case 'Place Order': happy path + 'item out of stock' + 'payment declined' + 'session timeout'.",
        "istqb_level": "Foundation",
    },
}

# ═══════════════════════════════════════════════════════════════════
# 2. TESTING TYPES
# ═══════════════════════════════════════════════════════════════════

TESTING_TYPES = {
    "functional": {
        "name": "Functional Testing",
        "description": "Verifying that functionality conforms to requirements.",
        "when_critical": ["All projects — baseline level of testing"],
        "subtypes": ["Smoke", "Sanity", "Regression", "Integration", "System", "UAT"],
    },
    "ui_ux": {
        "name": "UI/UX Testing",
        "description": "Verifying the interface: layout, colors, fonts, navigation, responsiveness.",
        "when_critical": ["B2C products", "E-commerce", "SaaS with a large user base", "Mobile apps"],
        "subtypes": ["Visual Regression", "Cross-browser", "Responsive", "Accessibility"],
    },
    "api_testing": {
        "name": "API Testing",
        "description": "Testing REST/GraphQL/SOAP endpoints: status codes, response body, validation, authorization.",
        "when_critical": ["Microservice architecture", "Backend-heavy products", "Third-party integrations"],
        "subtypes": ["Contract Testing", "Integration", "Performance", "Security"],
    },
    "security": {
        "name": "Security Testing",
        "description": "Verifying security: OWASP Top 10, authorization, XSS, SQL Injection, CSRF.",
        "when_critical": ["FinTech", "Healthcare", "E-commerce", "Products with personal data", "SaaS"],
        "subtypes": ["Penetration", "Vulnerability Scanning", "Auth Testing", "Data Protection"],
    },
    "performance": {
        "name": "Performance Testing",
        "description": "Load testing: response time, throughput, stability under load.",
        "when_critical": ["High-traffic applications", "E-commerce (sales events)", "Real-time systems", "API with SLA"],
        "subtypes": ["Load", "Stress", "Spike", "Endurance", "Scalability"],
    },
    "compatibility": {
        "name": "Compatibility / Cross-platform Testing",
        "description": "Testing across different browsers, OSes, devices, and screen resolutions.",
        "when_critical": ["Web applications", "Mobile apps", "Cross-platform products"],
        "subtypes": ["Cross-browser", "Cross-device", "OS Compatibility", "Screen Resolution"],
    },
    "accessibility": {
        "name": "Accessibility Testing (a11y)",
        "description": "Verifying accessibility for people with disabilities: WCAG 2.1/2.2, screen readers, keyboard navigation.",
        "when_critical": ["Government services", "Educational platforms", "B2C with a broad audience", "EU/US markets"],
        "subtypes": ["Screen Reader", "Keyboard Navigation", "Color Contrast", "ARIA Labels"],
    },
    "localization": {
        "name": "Localization / i18n Testing",
        "description": "Testing translations, date/currency formats, RTL, character encoding.",
        "when_critical": ["Multi-language products", "International markets", "E-commerce with multiple currencies"],
        "subtypes": ["Translation", "Date/Time Formats", "Currency", "RTL Support", "Unicode"],
    },
    "mobile_specific": {
        "name": "Mobile-Specific Testing",
        "description": "Gestures, screen orientation, push notifications, offline mode, interruptions (calls, SMS).",
        "when_critical": ["Native mobile apps", "PWA", "Hybrid apps"],
        "subtypes": ["Gestures", "Orientation", "Interruptions", "Offline", "Push Notifications", "Deep Links"],
    },
    "database": {
        "name": "Database Testing",
        "description": "Verifying data integrity, migrations, CRUD operations, indexes.",
        "when_critical": ["Data-intensive applications", "FinTech", "Healthcare", "Migrations between systems"],
        "subtypes": ["Data Integrity", "Migration", "CRUD", "Stored Procedures", "Backup/Restore"],
    },
    "usability": {
        "name": "Usability Testing",
        "description": "Evaluating ease of use: navigation, clarity, time to complete tasks.",
        "when_critical": ["B2C products", "New products without analogues", "Redesign"],
        "subtypes": ["Task Completion", "Navigation", "Learnability", "User Satisfaction"],
    },
    "regression": {
        "name": "Regression Testing",
        "description": "Verifying that new changes have not broken existing functionality.",
        "when_critical": ["All projects with active development", "Frequent releases", "CI/CD"],
        "subtypes": ["Full Regression", "Partial Regression", "Smoke Suite"],
    },
}

# ═══════════════════════════════════════════════════════════════════
# 3. TESTING TOOLS
# ═══════════════════════════════════════════════════════════════════

TESTING_TOOLS = {
    "test_management": {
        "category": "Test Management",
        "tools": [
            {"name": "TestRail", "type": "Commercial", "best_for": ["Structured test case management", "Reporting"], "reasoning": "The gold standard for test case management. Integration with Jira, CI/CD. Detailed reports."},
            {"name": "Zephyr Scale (TM4J)", "type": "Commercial", "best_for": ["Jira-native workflow", "Agile teams"], "reasoning": "Embeds directly into Jira. Ideal for Agile teams already using Jira."},
            {"name": "qase.io", "type": "Freemium", "best_for": ["Modern UI", "Small/medium teams"], "reasoning": "Modern interface, free plan for small teams. API-first approach."},
            {"name": "TestLink", "type": "Open Source", "best_for": ["Budget-conscious teams", "Self-hosted"], "reasoning": "Free, self-hosted. Suitable when budget is limited or there are NDA/on-premise requirements."},
            {"name": "Xray for Jira", "type": "Commercial", "best_for": ["BDD support", "Jira integration"], "reasoning": "BDD (Gherkin) support, native Jira integration, traceability requirements→tests."},
        ],
    },
    "bug_tracking": {
        "category": "Bug Tracking",
        "tools": [
            {"name": "Jira", "type": "Commercial", "best_for": ["Enterprise", "Agile teams", "Customizable workflows"], "reasoning": "The most popular tracker. Custom workflows, integrations, reports. Industry standard."},
            {"name": "Linear", "type": "Commercial", "best_for": ["Startups", "Fast-paced teams", "Developer-friendly"], "reasoning": "Fast, minimalist. Keyboard-first UX. Ideal for startups."},
            {"name": "YouTrack", "type": "Freemium", "best_for": ["JetBrains ecosystem", "Agile boards"], "reasoning": "Free for up to 10 users. Powerful Smart queries, Agile boards."},
            {"name": "Mantis BT", "type": "Open Source", "best_for": ["Simple bug tracking", "Self-hosted"], "reasoning": "Simple, lightweight, self-hosted. Suitable for small teams with limited budget."},
        ],
    },
    "api_testing": {
        "category": "API Testing",
        "tools": [
            {"name": "Postman", "type": "Freemium", "best_for": ["REST API testing", "Team collaboration", "Collections"], "reasoning": "The standard for manual API testing. Collections, environments, mock servers. Easy to get started."},
            {"name": "Insomnia", "type": "Freemium", "best_for": ["GraphQL", "Lightweight API testing"], "reasoning": "Lighter than Postman. Excellent GraphQL support. Minimalist interface."},
            {"name": "Swagger/OpenAPI", "type": "Open Source", "best_for": ["API documentation", "Contract testing"], "reasoning": "Automatic documentation + playground for testing directly from the UI."},
            {"name": "SoapUI", "type": "Freemium", "best_for": ["SOAP APIs", "Enterprise integrations"], "reasoning": "The standard for SOAP. Also supports REST. For enterprise integrations."},
        ],
    },
    "browser_testing": {
        "category": "Cross-browser / UI Testing",
        "tools": [
            {"name": "BrowserStack", "type": "Commercial", "best_for": ["Real device testing", "Cross-browser", "Screenshots"], "reasoning": "Real devices and browsers in the cloud. Responsive testing. Percy for visual regression."},
            {"name": "LambdaTest", "type": "Freemium", "best_for": ["Cross-browser", "Affordable cloud testing"], "reasoning": "An alternative to BrowserStack at a lower price. Smart testing, screenshot comparison."},
            {"name": "Chrome DevTools", "type": "Free", "best_for": ["Quick debugging", "Network analysis", "Performance"], "reasoning": "Built into Chrome. Network tab, Console, Performance profiling. An essential tool."},
            {"name": "Responsively App", "type": "Open Source", "best_for": ["Responsive design testing", "Multiple viewports"], "reasoning": "Displays the site simultaneously across different viewports. Free."},
        ],
    },
    "performance_testing": {
        "category": "Performance Testing",
        "tools": [
            {"name": "JMeter", "type": "Open Source", "best_for": ["Load testing", "API performance", "Scriptable"], "reasoning": "The standard for load testing. GUI + CLI. Extensible with plugins."},
            {"name": "k6", "type": "Open Source", "best_for": ["Developer-friendly load testing", "CI/CD integration"], "reasoning": "JavaScript-based. Easily integrates with CI/CD. A modern approach to performance testing."},
            {"name": "Lighthouse", "type": "Free", "best_for": ["Web performance audit", "SEO", "Accessibility"], "reasoning": "Built into Chrome. Automatic audit of performance, a11y, SEO, best practices."},
            {"name": "PageSpeed Insights", "type": "Free", "best_for": ["Quick performance check", "Core Web Vitals"], "reasoning": "Quick Core Web Vitals check. Optimization recommendations from Google."},
        ],
    },
    "security_testing": {
        "category": "Security Testing",
        "tools": [
            {"name": "OWASP ZAP", "type": "Open Source", "best_for": ["Web app security scanning", "OWASP Top 10"], "reasoning": "Free vulnerability scanner from OWASP. Automated scanning + manual testing."},
            {"name": "Burp Suite Community", "type": "Free", "best_for": ["Manual security testing", "Proxy interception"], "reasoning": "Proxy for traffic interception. Manual security testing. Community version is free."},
            {"name": "Snyk", "type": "Freemium", "best_for": ["Dependency scanning", "Container security"], "reasoning": "Scans dependencies for vulnerabilities. Integration with GitHub/GitLab."},
        ],
    },
    "accessibility_testing": {
        "category": "Accessibility Testing",
        "tools": [
            {"name": "axe DevTools", "type": "Free", "best_for": ["WCAG compliance", "Browser extension"], "reasoning": "Chrome extension. Finds a11y issues right in DevTools. WCAG 2.1 compliance check."},
            {"name": "WAVE", "type": "Free", "best_for": ["Visual accessibility evaluation", "Quick audit"], "reasoning": "Visual display of a11y errors directly on the page. Easy to get started."},
            {"name": "Lighthouse (a11y)", "type": "Free", "best_for": ["Automated a11y audit", "CI integration"], "reasoning": "A11y audit as part of Lighthouse. Integrates with CI/CD."},
        ],
    },
    "mobile_testing": {
        "category": "Mobile Testing",
        "tools": [
            {"name": "Android Studio Emulator", "type": "Free", "best_for": ["Android testing", "Debug"], "reasoning": "Official Android emulator. Various API versions and screen sizes."},
            {"name": "Xcode Simulator", "type": "Free", "best_for": ["iOS testing", "Debug"], "reasoning": "Official iOS/iPadOS simulator. macOS only."},
            {"name": "BrowserStack / Sauce Labs", "type": "Commercial", "best_for": ["Real device cloud", "Cross-device"], "reasoning": "Real devices in the cloud. For when there is no access to physical devices."},
            {"name": "Charles Proxy", "type": "Commercial", "best_for": ["Network debugging", "API mocking"], "reasoning": "Traffic interception for mobile apps. Mock responses for testing edge cases."},
        ],
    },
    "collaboration": {
        "category": "Collaboration & Documentation",
        "tools": [
            {"name": "Confluence", "type": "Commercial", "best_for": ["Test plans", "Knowledge base", "Jira integration"], "reasoning": "The standard for documentation in teams using Jira."},
            {"name": "Notion", "type": "Freemium", "best_for": ["Flexible docs", "Small teams", "Modern UI"], "reasoning": "Flexible, modern. Suitable for startups and small teams."},
            {"name": "Loom", "type": "Freemium", "best_for": ["Bug reproduction videos", "Team communication"], "reasoning": "Quickly record a bug reproduction video. Better than screenshots for complex scenarios."},
        ],
    },
}

# ═══════════════════════════════════════════════════════════════════
# 4. NO-CODE AUTOMATION TOOLS
# ═══════════════════════════════════════════════════════════════════

NOCODE_TOOLS = [
    {
        "name": "Testim",
        "type": "AI-powered",
        "platforms": ["Web"],
        "description": "AI-based testing. Records user actions, creates stable tests with AI locators.",
        "reasoning": "Minimal entry barrier. AI automatically adapts tests when the UI changes. Ideal for QA without coding experience.",
        "best_for": ["Web UI automation", "Teams without coding skills", "Fast-changing UI"],
    },
    {
        "name": "Katalon Studio",
        "type": "Low-code",
        "platforms": ["Web", "Mobile", "API", "Desktop"],
        "description": "Comprehensive tool: record & playback + scripting. Supports Web, Mobile, API.",
        "reasoning": "Broad platform coverage. Can start with record & playback, then transition to scripts. Free version available.",
        "best_for": ["Multi-platform testing", "Gradual automation adoption", "Small teams"],
    },
    {
        "name": "mabl",
        "type": "AI-powered",
        "platforms": ["Web"],
        "description": "SaaS platform with AI for automated testing. Auto-healing tests, visual testing.",
        "reasoning": "Fully cloud-based. Tests automatically adapt to UI changes. Built-in visual regression and performance testing.",
        "best_for": ["SaaS products", "CI/CD integration", "Visual regression"],
    },
    {
        "name": "Leapwork",
        "type": "Visual automation",
        "platforms": ["Web", "Desktop", "Citrix"],
        "description": "Visual automation builder: flowchart-based approach to creating tests.",
        "reasoning": "Drag-and-drop interface. Suitable for enterprises with legacy desktop applications and Citrix.",
        "best_for": ["Enterprise", "Desktop apps", "Citrix environments", "Non-technical teams"],
    },
    {
        "name": "Reflect",
        "type": "Record & Replay",
        "platforms": ["Web"],
        "description": "Records browser actions, creates automated tests without code.",
        "reasoning": "The simplest start: open browser → record → test is ready. Cloud execution.",
        "best_for": ["Quick smoke tests", "Regression suites", "Non-technical QA"],
    },
    {
        "name": "Applitools",
        "type": "Visual AI",
        "platforms": ["Web", "Mobile"],
        "description": "AI-powered visual testing. Screenshot comparison with intelligent ignoring of insignificant changes.",
        "reasoning": "The best for visual regression. AI distinguishes bugs from expected changes. Integrates with any framework.",
        "best_for": ["Visual regression", "Design-heavy products", "Multi-browser screenshots"],
    },
]

# ═══════════════════════════════════════════════════════════════════
# 4b. END-TO-END FLOW PLAYBOOKS
# ═══════════════════════════════════════════════════════════════════
#
# Named business flows that span many screens and require a fixed sequence
# of checks. Unlike `DOMAIN_FOCUS`, each playbook is a concrete, step-
# ordered list every QA Engineer should run when the flow is exercised.
# ═══════════════════════════════════════════════════════════════════

FLOW_PLAYBOOKS = {
    "checkout_flow": {
        "name": "Checkout Flow (end-to-end purchase)",
        "description": (
            "The complete buyer journey on an e-commerce site: discover product → "
            "add to cart → authenticate → enter shipping & contact info → select "
            "delivery and payment methods → review the order → place the order → "
            "verify confirmation (order ID, receipt email, stock/inventory update)."
        ),
        "triggers": [
            "checkout flow", "checkout process", "end-to-end checkout",
            "buy flow", "purchase flow", "order flow", "place order flow",
            "cart to order", "cart-to-order", "full checkout",
            "e2e checkout", "happy path checkout",
            # UA variants
            "оформлення замовлення", "процес оформлення",
            "покупка", "процес покупки", "оформити замовлення",
            "наскрізний чекаут", "чекаут", "чек-аут",
        ],
        "required_phases": [
            {
                "phase": "Discovery / Product",
                "checks": [
                    "Product page loads with title, price, availability and images",
                    "Size / color / variant selectors update price and stock",
                    "Out-of-stock products cannot be added to the cart",
                ],
            },
            {
                "phase": "Cart",
                "checks": [
                    "'Add to cart' increments the cart badge and persists across reloads",
                    "Cart subtotal = sum(unit_price × qty) for every line",
                    "Quantity +/- respects stock limits and minimum 1",
                    "Line item can be removed; empty cart shows a clear empty state",
                    "Promo / coupon code applies the declared discount; invalid code rejected",
                    "Currency / tax / shipping preview reflect the selected region",
                ],
            },
            {
                "phase": "Authentication / Guest",
                "checks": [
                    "Login with valid credentials continues to the checkout",
                    "Invalid credentials show an error and keep the cart intact",
                    "Guest checkout (if supported) captures email and phone",
                    "Existing account on same email is detected — offers login instead",
                ],
            },
            {
                "phase": "Shipping & Contact",
                "checks": [
                    "Required fields (name, phone, email, address, city, ZIP, country) validated",
                    "Phone / ZIP / email formats validated per selected country",
                    "Address autocomplete suggestions insert a valid address",
                    "Shipping method list reflects cart weight/size and destination",
                    "Delivery date / time slot (if offered) is consistent with method",
                ],
            },
            {
                "phase": "Payment",
                "checks": [
                    "All advertised payment methods are selectable (card, PayPal, Apple/Google Pay, bank transfer, cash-on-delivery)",
                    "Card form validates number (Luhn), expiry (not in past), CVC length",
                    "3-D Secure / OTP challenge completes and returns to the shop",
                    "Declined / insufficient funds card shows a clear error; order is NOT created",
                    "Timeout / network drop during authorization does NOT create duplicate orders",
                    "Submit button is disabled after the first click to prevent double-charge",
                ],
            },
            {
                "phase": "Review & Place Order",
                "checks": [
                    "Review screen shows items, prices, shipping, tax, discount and grand total",
                    "Editing any section returns to the correct step with preserved data",
                    "Terms & conditions / age check / marketing opt-in are required where declared",
                    "'Place order' button is clickable only when all prior steps are valid",
                ],
            },
            {
                "phase": "Confirmation & Post-order",
                "checks": [
                    "Order confirmation page shows a unique order ID and full summary",
                    "Confirmation email is delivered to the buyer with the same totals",
                    "Cart is cleared; the product stock is decremented",
                    "Order appears in 'My orders' with status 'New / Paid / Processing'",
                    "Back button after confirmation does NOT re-submit the order",
                ],
            },
            {
                "phase": "Edge / Negative",
                "checks": [
                    "Two buyers buying the last unit in parallel — only one succeeds",
                    "Price or stock changes while the buyer is in checkout — user is warned",
                    "Session expires mid-checkout — cart is restored on re-login",
                    "Currency / language change mid-flow preserves cart and totals",
                    "Very large cart (50+ line items) renders and totals correctly",
                    "Refresh / browser-back / double-submit does not duplicate the order",
                ],
            },
            {
                "phase": "Security & Compliance",
                "checks": [
                    "Checkout is served over HTTPS with a valid certificate",
                    "Card data is not logged in browser console / network responses",
                    "PCI DSS: no PAN stored client-side; only tokenized references",
                    "GDPR: marketing opt-in is opt-in by default, never pre-checked",
                    "Authorization: user A cannot view / edit user B's order by ID",
                ],
            },
        ],
        "test_data_reference": {
            "stripe_success": "4242 4242 4242 4242, any future expiry, any CVC",
            "stripe_declined": "4000 0000 0000 0002",
            "stripe_insufficient": "4000 0000 0000 9995",
            "stripe_3ds": "4000 0027 6000 3184",
        },
        "recommended_techniques": [
            "Use Case Testing (happy path + alternative flows)",
            "State Transition Testing (Cart → Shipping → Payment → Placed → Paid)",
            "Decision Table Testing (payment method × region × promo)",
            "Boundary Value Analysis (qty, amount, promo thresholds)",
            "Error Guessing (double-click, back button, refresh mid-payment)",
        ],
    },
}


# ═══════════════════════════════════════════════════════════════════
# 5. DOMAIN-SPECIFIC FOCUS AREAS
# ═══════════════════════════════════════════════════════════════════

DOMAIN_FOCUS = {
    "e-commerce": {
        "name": "E-Commerce",
        "critical_modules": [
            {"module": "Product Catalog / Search", "why": "Core revenue driver. Search must be accurate, filters must be correct."},
            {"module": "Cart / Checkout", "why": "Direct impact on conversion. Any bug = lost revenue."},
            {"module": "Payment / Payment Gateway", "why": "The most critical module. Errors = financial losses + loss of trust."},
            {"module": "Order Management", "why": "Statuses, cancellations, returns — a complex state machine."},
            {"module": "User Account / Authorization", "why": "Personal data + payment information. Security-critical."},
            {"module": "Promotions / Discounts", "why": "Complex business logic with discount combinations. Decision table testing."},
        ],
        "key_testing_types": ["functional", "security", "performance", "compatibility", "usability"],
        "edge_cases": [
            "Two users simultaneously purchasing the last item",
            "Item price changes during checkout",
            "Huge cart (1000+ items)",
            "Timeout during payment — was the money charged?",
            "Promo code + VIP discount + sale simultaneously",
            "Currency/language changes during checkout",
        ],
        "standards": ["PCI DSS (payments)", "GDPR (personal data)", "WCAG 2.1 (accessibility)"],
    },
    "fintech": {
        "name": "FinTech / Banking",
        "critical_modules": [
            {"module": "Transactions / Transfers", "why": "Accuracy to the penny. Rounding, currency conversion, limits."},
            {"module": "Authorization / 2FA / Biometric", "why": "Financial data — the highest level of protection."},
            {"module": "Balance / Statements", "why": "Correctness of calculations, real-time updates."},
            {"module": "KYC / Verification", "why": "Regulatory requirements. Document uploads, OCR."},
            {"module": "Notifications", "why": "Critical alerts about transactions. Cannot be missed."},
        ],
        "key_testing_types": ["security", "functional", "performance", "api_testing", "database"],
        "edge_cases": [
            "Transfer of 0.01 / maximum amount / negative amount",
            "Simultaneous transfer from two devices",
            "Currency conversion with rounding (0.001)",
            "Timeout during transaction — rollback?",
            "Transfer to yourself",
            "Transfer to a blocked/closed account",
        ],
        "standards": ["PCI DSS", "SOX", "GDPR", "PSD2 (EU)", "ISO 27001"],
    },
    "healthcare": {
        "name": "Healthcare / MedTech",
        "critical_modules": [
            {"module": "Medical Records (EHR/EMR)", "why": "Data accuracy = patient health."},
            {"module": "Prescriptions / Dosage", "why": "A dosage error is a threat to life."},
            {"module": "Authorization / Roles", "why": "Doctor vs nurse vs patient — different data access levels."},
            {"module": "Integration with Medical Equipment", "why": "Real-time data from devices. Accuracy is critical."},
            {"module": "Telemedicine", "why": "Video/audio quality, connection stability."},
        ],
        "key_testing_types": ["security", "functional", "accessibility", "usability", "compatibility"],
        "edge_cases": [
            "Allergy + prescribing medication containing the allergen",
            "Patient with the same name and date of birth",
            "Loss of connection during a teleconsultation",
            "Time zones in records (patient in a different country)",
            "Very long medical history — pagination/search",
        ],
        "standards": ["HIPAA", "HL7 FHIR", "FDA 21 CFR Part 11", "GDPR", "IEC 62304"],
    },
    "saas": {
        "name": "SaaS Platform",
        "critical_modules": [
            {"module": "Registration / Onboarding", "why": "First impression. Trial-to-paid conversion."},
            {"module": "Subscriptions / Billing", "why": "Recurring payments, pricing plans, upgrade/downgrade."},
            {"module": "Multi-tenancy / Data Isolation", "why": "One client's data must not be visible to another."},
            {"module": "API / Integrations", "why": "SaaS thrives on integrations. Webhooks, OAuth, rate limits."},
            {"module": "Dashboard / Analytics", "why": "The main user interface. Data correctness."},
        ],
        "key_testing_types": ["functional", "security", "api_testing", "performance", "compatibility"],
        "edge_cases": [
            "Plan upgrade in the middle of a billing cycle",
            "Two admins simultaneously changing settings",
            "API rate limit exceeded",
            "Webhook not delivered — retry mechanism",
            "Trial expired — what happens to the data?",
        ],
        "standards": ["SOC 2 Type II", "GDPR", "ISO 27001", "CCPA"],
    },
    "education": {
        "name": "EdTech / E-Learning",
        "critical_modules": [
            {"module": "Content / Courses", "why": "Rendering different content types: video, text, quizzes."},
            {"module": "Progress / Grades", "why": "Accuracy of score and progress calculations."},
            {"module": "Video Player / Streaming", "why": "Buffering, quality, subtitles, playback speed."},
            {"module": "Certificates / Diplomas", "why": "Automatic generation upon course completion."},
        ],
        "key_testing_types": ["functional", "usability", "accessibility", "compatibility", "performance"],
        "edge_cases": [
            "Loss of connection during a test",
            "Opening a course in two tabs simultaneously",
            "Changing an answer after submit",
            "Very slow connection — is progress saved?",
        ],
        "standards": ["WCAG 2.1 AA", "SCORM", "xAPI", "GDPR (students)", "COPPA (children)"],
    },
    "social_media": {
        "name": "Social Media / Messaging",
        "critical_modules": [
            {"module": "Feed", "why": "Core experience. Algorithm, loading, infinite scroll."},
            {"module": "Messages / Chat", "why": "Real-time delivery, statuses (delivered/read), media."},
            {"module": "Profile / Privacy Settings", "why": "Control over one's own data and visibility."},
            {"module": "Push Notifications", "why": "Engagement driver. Accuracy, timing, preferences."},
            {"module": "Content Moderation", "why": "Spam filtering, offensive content, reporting."},
        ],
        "key_testing_types": ["functional", "performance", "mobile_specific", "usability", "security"],
        "edge_cases": [
            "Message with 10000+ emojis",
            "Two people simultaneously editing a post",
            "Push notification after a message has been deleted",
            "Blocking a user during an active chat",
        ],
        "standards": ["GDPR", "COPPA (children)", "DSA (EU Digital Services Act)", "CCPA"],
    },
    "iot": {
        "name": "IoT / Smart Devices",
        "critical_modules": [
            {"module": "Pairing / Device Connection", "why": "Bluetooth/WiFi pairing — a frequent source of issues."},
            {"module": "Real-time Sensor Data", "why": "Accuracy, latency, calibration."},
            {"module": "Firmware Update (OTA)", "why": "An update must not 'brick' the device."},
            {"module": "Offline Mode / Sync", "why": "Devices work without internet. Sync upon reconnection."},
        ],
        "key_testing_types": ["functional", "compatibility", "performance", "security", "mobile_specific"],
        "edge_cases": [
            "Loss of connection during an OTA update",
            "100+ devices on a single account",
            "Time zone change — scheduling",
            "Low battery during a critical operation",
        ],
        "standards": ["IEC 62443 (IoT Security)", "GDPR", "FCC/CE (regulatory)", "Matter/Thread protocols"],
    },
    "other": {
        "name": "General / Other",
        "critical_modules": [
            {"module": "Authorization / Access Management", "why": "Present in every product. Security baseline."},
            {"module": "Core Business Process (core flow)", "why": "Happy path must work flawlessly."},
            {"module": "Search / Filtering", "why": "Frequently used functionality with many edge cases."},
            {"module": "Settings / Profile", "why": "CRUD operations + validation + state persistence."},
        ],
        "key_testing_types": ["functional", "regression", "ui_ux", "security"],
        "edge_cases": [
            "Empty fields / maximum length",
            "Special characters / Unicode / emojis in all fields",
            "Concurrent access (two users simultaneously)",
            "Session expiry during an operation",
            "Back button / refresh during submit",
        ],
        "standards": ["OWASP Top 10", "GDPR", "WCAG 2.1"],
    },
}

# ═══════════════════════════════════════════════════════════════════
# 6. PLATFORM-SPECIFIC RECOMMENDATIONS
# ═══════════════════════════════════════════════════════════════════

PLATFORM_SPECIFICS = {
    "web": {
        "name": "Web Application",
        "must_test": [
            "Cross-browser (Chrome, Firefox, Safari, Edge)",
            "Responsive design (mobile, tablet, desktop viewports)",
            "Keyboard navigation and screen reader",
            "Cookies, localStorage, sessionStorage",
            "Deep linking / URL routing",
            "SEO elements (meta tags, structured data)",
        ],
        "recommended_tools": ["Chrome DevTools", "BrowserStack", "Lighthouse", "axe DevTools", "Postman"],
    },
    "mobile_native": {
        "name": "Native Mobile App",
        "must_test": [
            "Different OS versions (iOS 15+, Android 10+)",
            "Different screen sizes (SE, Standard, Plus/Max)",
            "Gestures (swipe, pinch, long press)",
            "Interruptions (calls, SMS, alarm)",
            "Background/foreground transitions",
            "Push notifications (foreground, background, killed)",
            "Offline → Online sync",
            "Permissions (camera, location, notifications)",
            "Install / Update / Uninstall flows",
        ],
        "recommended_tools": ["Android Studio", "Xcode", "BrowserStack", "Charles Proxy", "Appium (no-code via Katalon)"],
    },
    "mobile_hybrid": {
        "name": "Hybrid / PWA",
        "must_test": [
            "WebView performance and compatibility",
            "Install prompt (PWA) / Add to Home Screen",
            "Offline support (Service Worker)",
            "Native feature access (camera, GPS, push)",
            "Deep links / Universal links",
        ],
        "recommended_tools": ["Chrome DevTools (Remote Debugging)", "BrowserStack", "Lighthouse"],
    },
    "desktop": {
        "name": "Desktop Application",
        "must_test": [
            "Different OSes (Windows 10/11, macOS, Linux)",
            "Install / Uninstall / Update processes",
            "File system operations (read/write/permissions)",
            "High DPI / Retina display",
            "Keyboard shortcuts",
            "System tray / Dock integration",
        ],
        "recommended_tools": ["Leapwork", "Katalon", "Applitools"],
    },
    "api_backend": {
        "name": "API / Backend Service",
        "must_test": [
            "All HTTP methods (GET, POST, PUT, PATCH, DELETE)",
            "Status codes (200, 201, 400, 401, 403, 404, 500)",
            "Request/Response validation (schema)",
            "Authentication / Authorization (JWT, OAuth, API keys)",
            "Rate limiting / Throttling",
            "Pagination / Sorting / Filtering",
            "Error handling and messages",
            "CORS configuration",
        ],
        "recommended_tools": ["Postman", "Swagger/OpenAPI", "JMeter", "OWASP ZAP"],
    },
}

# ═══════════════════════════════════════════════════════════════════
# 7. STANDARDS & CERTIFICATIONS REFERENCE
# ═══════════════════════════════════════════════════════════════════

STANDARDS = {
    "ISTQB": {"full_name": "International Software Testing Qualifications Board", "relevance": "Foundational certification for QA. Defines terminology, techniques, and processes.", "url": "https://www.istqb.org"},
    "ISO 25010": {"full_name": "Software Quality Model", "relevance": "8 software quality characteristics: functionality, reliability, usability, efficiency, maintainability, portability, security, compatibility.", "url": "https://iso25000.com/index.php/en/iso-25000-standards/iso-25010"},
    "OWASP Top 10": {"full_name": "Open Web Application Security Project", "relevance": "10 most critical web application vulnerabilities. Mandatory for security testing.", "url": "https://owasp.org/www-project-top-ten/"},
    "WCAG 2.1/2.2": {"full_name": "Web Content Accessibility Guidelines", "relevance": "Accessibility standard. Levels A, AA, AAA. Mandatory for government sector and EU.", "url": "https://www.w3.org/WAI/WCAG21/quickref/"},
    "GDPR": {"full_name": "General Data Protection Regulation", "relevance": "Personal data protection in the EU. Impact on testing: consent, data deletion, export.", "url": "https://gdpr.eu"},
    "PCI DSS": {"full_name": "Payment Card Industry Data Security Standard", "relevance": "Mandatory for payment processing. Requirements for encryption, auditing, and access control.", "url": "https://www.pcisecuritystandards.org"},
    "HIPAA": {"full_name": "Health Insurance Portability and Accountability Act", "relevance": "Protection of medical data in the US. Requirements for encryption, auditing, and access control.", "url": "https://www.hhs.gov/hipaa"},
    "SOC 2": {"full_name": "Service Organization Control 2", "relevance": "Security standard for SaaS. 5 principles: security, availability, processing integrity, confidentiality, privacy.", "url": "https://www.aicpa.org/soc2"},
}
