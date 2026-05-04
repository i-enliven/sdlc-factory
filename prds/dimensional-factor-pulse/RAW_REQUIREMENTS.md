# PRD: Dimensional Factor Pulse (DFP)

## 1. Business Objective
The **Dimensional Factor Pulse (DFP)** is a containerized financial dashboard designed for Dimensional Fund Advisors (DFA) management. The goal is to demonstrate the **SDLC-Factory's** ability to generate a fully orchestrated, interactive multi-container environment that handles dynamic user inputs with professional-grade financial accuracy.

## 2. Functional Boundaries

### 2.1 Tier 1: UI (Web Interface)
- **Technology**: React/Vite (pnpm-based) served via **Nginx**.
- **Styling & Visualization**: You MUST utilize **Tailwind CSS** for all component styling to ensure a modern, professional interface. The dashboard MUST feature a performance heatmap of factor premiums. CRITICAL: Do not use Recharts or standard line graphs. You MUST use @nivo/heatmap or apexcharts to render a true grid-based heatmap (X-axis: date, Y-axis: funds, Color: positive/negative premium).
- **Page title**: "Dimensional Factor Pulse"
- **Core View**: A "Management View" dashboard featuring a styled performance heatmap of factor premiums.
- **Dynamic Controls**:
    - **Fund Selector**: A dropdown to switch between the following proxy pairs:
        | Region | DFA Proxy Ticker | Benchmark Ticker |
        | :--- | :--- | :--- |
        | **US Core Equity** | `DFAC` | `^GSPC` (S&P 500) |
        | **Intl. Vector** | `DXIV` | `URTH` (MSCI World ex US) |
        | **Emerging Markets** | `DFAE` | `EEM` (MSCI EM Index) |
    - **Timeframe Toggle**: Buttons to filter data by **1D, 5D, 1M, 6M, 1Y and YTD**.
- **Metadata**: Display "Built by SDLC-Factory" footer with automated build timestamp and git hash.
- **Connectivity**: Automatically discover API endpoint via Docker DNS using the service name `logic-api`.
- **Quality Assurance**: The project setup must include **Playwright** as a dev dependency to allow for downstream E2E browser testing of the UI-to-API integration. 
- **UI/UX**: 
    - **No generic names**: Do not use generic names like "Metric 1", instead use descriptive names that reflect the metric composition.
    - **Heatmap**: The heatmap should be interactive and visually appealing, with clear color differentiation between positive and negative premiums.
    - **Legend**: Include legend with regions and proxy pairs
    - **Columns Text**: At narrow columns, text should be oriented vertically to fit the content
    - **Mouse Over**: When mouse is over a cell, it should display the date, premium value and the region and proxy pair in a tooltip.
- **Authentication**: There should be NO authentication for this application.

### 2.2 Tier 2: API (Logic Engine)
- **Technology**: Python/uv-based(pyproject.toml).
- **Core Logic**:
    - **Premium Calculation**: $Premium = \text{DFA Proxy Return} - \text{Benchmark Return}$.
    - **Precision**: All returns must be calculated and stored to **4 decimal places**.
    - **Filtered Queries**: Endpoint must accept `fund_id` and `timeframe` parameters to return filtered time-series data.
- **Data Fetching**: Startup script using a library like `yfinance` to fetch the tickers defined in Section 2.1.
- **Health Check**: Provide `/health` endpoint for the UI to verify backend and database connectivity.
- **Authentication**: There should be NO authentication for this application.

### 2.3 Tier 3: Data (Persistence Layer)
- **Technology**: Postgres.
- **Responsibility**: Cache daily adjusted market closes for all supported funds and benchmarks.
- **Isolation/Persistence**: Internal network access only; data persists via Docker volumes.

## 3. Key Features & User Stories
* **3.1 Interactive Factor Heatmap**: A dynamic grid showing the performance of Dimensional’s core factors to verify market efficiency in real-time.
* **3.2 Dynamic Fund & Timeframe Controls**: A dropdown and toggle system to instantly re-calculate the dashboard view.
* **3.3 Factory Transparency**: An automated metadata footer injected with `GIT_COMMIT_HASH` and `BUILD_DATE`.
* **3.4 Startup & Resilience**:
    - **Initialization**: Logic-api must seed the last 730 days of data on first boot.
    - **Connectivity Logic**: Docker `depends_on` is insufficient for database readiness. The `logic-api` startup script MUST implement an explicit polling loop (e.g., using `tenacity` or a custom while loop) that attempts to connect to Postgres every 2 seconds (up to 10 attempts) before executing the initialization seed or binding the API port.

## 4. Technical Constraints
- **Containerization**: All components Dockerized.
- **Orchestration**: `docker-compose` mandatory.
- **Dependency Management**: `uv` for Python (pyproject.toml), `pnpm` for Node.js (package.json).
- **Networking**: `data-store` must be unreachable from the host machine.
- **Routing & Proxy (NEW)**:
    - **Port Mappings**: 
        - The `ui-gateway` must map host port **8000** to container port **80**.
        - The `logic-api` must map host port **8001** to container port **8001** (and bind to 8001 internally).
    - **Requirement**: The `ui-gateway` must include a custom `nginx.conf` file.
    - **Contract**: All frontend requests directed to `/api/*` must be reverse-proxied internally to `http://logic-api:8001/*`.
    - **Failure Prevention**: The Factory must ensure the Nginx configuration is injected into the `ui-gateway` image and validates the reverse proxy paths correctly to avoid 404 errors during integration.

## 5. Success Metrics for Demo
* **Zero-Touch Provisioning**: The entire stack builds and runs without manual code or YAML edits.
* **Integration Integrity**: Frontend fetches return 200 OK status codes via the internal Nginx proxy.
* **Data Accuracy**: The heatmap displays accurate premium calculations (4 decimal places) and handles weekend/holiday data gracefully.
* **UI/UX**: The heatmap is interactive and visually appealing, with clear color differentiation between positive and negative premiums.
* **Deployment Speed**: Time from "PRD Input" to "Live Dashboard" is less than 30 minutes.
